"""BLE Mesh GATT Proxy transport layer (network layer + GATT link only).


This class used to also own the AppKey and a single fixed dimmer_addr --
i.e. one connection per dimmer, hard-wired to talk to just that one
device. That's gone now: a single GATT Proxy connection is a gateway
into the WHOLE mesh network, not a private channel to one node, and
different dimmers can be bound to different AppKeys. So this class only
knows about what's shared network-wide (the NetKey-derived NID/EncKey/
PrivKey and the GATT link itself); AppKey encryption/decryption and
routing by destination now live one layer up, in mesh_gateway.py.
"""

import asyncio
import logging
from typing import Callable

from bleak import BleakClient, BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection

from .const import (
    MESH_PROXY_SERVICE_UUID,
    MESH_PROXY_DATA_IN_UUID,
    MESH_PROXY_DATA_OUT_UUID,
    PROXY_MSG_TYPE_NETWORK_PDU,
    SAR_COMPLETE,
    SAR_FIRST,
    SAR_CONTINUE,
    SAR_LAST,
)
from .mesh_crypto import (
    k2,
    build_network_pdu,
    build_proxy_pdu,
    decrypt_network_pdu,
)

_LOGGER = logging.getLogger(__name__)


class MeshProxyConnection:
    """One BLE GATT link into the mesh network's transport layer."""

    def __init__(
        self,
        ble_device: BLEDevice,
        netkey: bytes,
        netkey_index: int,
        provisioner_addr: int,
        iv_index: int,
        ttl: int = 8,
    ):
        self.ble_device = ble_device
        self.netkey = netkey
        self.netkey_index = netkey_index
        self.provisioner_addr = provisioner_addr
        self.iv_index = iv_index
        self.ttl = ttl

        # Derive network keys — P = b'\x00'. This is the only crypto this
        # class does -- Access-layer (AppKey) encryption happens in
        # mesh_gateway.py, since it varies per destination dimmer.
        self.nid, self.enc_key, self.priv_key = k2(netkey, b'\x00')
        _LOGGER.info(
            "Mesh crypto derived: NID=0x%02X, EncKey=%s..., PrivKey=%s...",
            self.nid, self.enc_key[:4].hex(), self.priv_key[:4].hex(),
        )

        self._client: BleakClient | None = None
        self._data_in_char: BleakGATTCharacteristic | None = None
        self._proxy_buffer = bytearray()
        self._lock = asyncio.Lock()
        # callback(seq, src, dst, transport_pdu) for every non-control
        # Network PDU received. transport_pdu still has its Lower
        # Transport header attached (AID + encrypted Access payload) --
        # decrypting it requires knowing which AppKey to try, which this
        # class doesn't know. That's mesh_gateway.py's job.
        self._transport_callback: Callable[[int, int, int, bytes], None] | None = None
        self._disconnect_callback: Callable[[], None] | None = None

    def set_transport_callback(self, callback: Callable[[int, int, int, bytes], None]):
        self._transport_callback = callback

    def set_disconnect_callback(self, callback: Callable[[], None]):
        """Fired when the GATT link drops unexpectedly, so the gateway can
        schedule a reconnect instead of only retrying on the next command."""
        self._disconnect_callback = callback

    # ------------------------------------------------------------------ #
    #  BLE CONNECTION (Using bleak-retry-connector)
    # ------------------------------------------------------------------ #

    async def connect(self) -> bool:
        """Connect to the mesh proxy node via BLE GATT using HA's connector."""
        try:
            _LOGGER.info("Establishing connection to %s via bleak-retry-connector...", self.ble_device.address)

            self._client = await establish_connection(
                client_class=BleakClient,
                device=self.ble_device,
                address=self.ble_device.address,
                disconnected_callback=self._on_disconnect,
                max_attempts=3,
                name="Theben DIMAX",
            )
            _LOGGER.info("Connected to mesh proxy %s", self.ble_device.address)

            service = self._client.services.get_service(MESH_PROXY_SERVICE_UUID)
            if not service:
                _LOGGER.error("Mesh Proxy Service (0x1828) not found on device")
                await self.disconnect()
                return False

            for char in service.characteristics:
                if char.uuid == MESH_PROXY_DATA_IN_UUID:
                    self._data_in_char = char
                    _LOGGER.info("Found Data In characteristic: %s", char.uuid)
                elif char.uuid == MESH_PROXY_DATA_OUT_UUID:
                    await self._client.start_notify(char, self._on_notification)
                    _LOGGER.info("Subscribed to Data Out characteristic: %s", char.uuid)

            if not self._data_in_char:
                _LOGGER.error("Mesh Proxy Data In characteristic not found")
                await self.disconnect()
                return False

            return True
        except Exception as err:
            _LOGGER.error("BLE connection failed: %s", err)
            return False

    async def disconnect(self):
        """Disconnect from the proxy node."""
        if self._client and self._client.is_connected:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        self._client = None

    def _on_disconnect(self, client: BleakClient):
        """Handle unexpected disconnection."""
        _LOGGER.warning("BLE disconnected from mesh proxy")
        self._client = None
        if self._disconnect_callback:
            self._disconnect_callback()

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    # ------------------------------------------------------------------ #
    #  GATT NOTIFICATION HANDLER
    # ------------------------------------------------------------------ #

    async def _on_notification(
        self, char: BleakGATTCharacteristic, data: bytearray
    ):
        """Handle incoming GATT notification from Data Out characteristic."""
        if not data:
            return

        sar = (data[0] >> 6) & 0x03
        msg_type = data[0] & 0x3F
        payload = data[1:]

        _LOGGER.debug("Proxy notification: SAR=%d type=%d len=%d data=%s",
                       sar, msg_type, len(payload), payload.hex())

        if sar == SAR_COMPLETE:
            await self._process_proxy_pdu(msg_type, payload)
        elif sar == SAR_FIRST:
            self._proxy_buffer = bytearray(payload)
        elif sar == SAR_CONTINUE:
            self._proxy_buffer.extend(payload)
        elif sar == SAR_LAST:
            self._proxy_buffer.extend(payload)
            await self._process_proxy_pdu(msg_type, bytes(self._proxy_buffer))
            self._proxy_buffer = bytearray()

    async def _process_proxy_pdu(self, msg_type: int, pdu: bytes):
        """Process a reassembled proxy PDU."""
        if msg_type != PROXY_MSG_TYPE_NETWORK_PDU:
            _LOGGER.debug("Ignoring proxy message type %d", msg_type)
            return

        result = decrypt_network_pdu(
            self.enc_key, self.priv_key, self.nid, self.iv_index, pdu
        )
        if result is None:
            _LOGGER.debug("Failed to decrypt network PDU")
            return

        ctl, ttl, seq, src, dst, transport_pdu = result
        _LOGGER.debug(
            "Network PDU: ctl=%d ttl=%d seq=%d src=0x%04X dst=0x%04X transport=%s",
            ctl, ttl, seq, src, dst, transport_pdu.hex(),
        )

        if ctl:
            return  # control messages -- not used by this integration

        if self._transport_callback:
            self._transport_callback(seq, src, dst, transport_pdu)

    # ------------------------------------------------------------------ #
    #  SEND
    # ------------------------------------------------------------------ #

    async def send_transport_pdu(self, seq: int, dst: int, lower_transport_pdu: bytes,
                                  ttl: int | None = None) -> bool:
        """Wrap an already Access-encrypted Lower Transport PDU in Network
        + Proxy framing and write it out.

        seq is supplied by the caller (mesh_gateway.py) rather than
        allocated here, because it has to be the exact same value used
        for the Access-layer encryption nonce one layer up -- both layers'
        nonces must agree or the far end's MIC check fails.

        ttl defaults to this connection's own self.ttl (set from whichever
        device we happened to connect through) if not given, but the
        caller should pass the TTL registered for the actual destination
        dimmer -- different nodes in the export can have different TTLs.
        """
        async with self._lock:
            if not self.is_connected:
                _LOGGER.error("Not connected to mesh proxy")
                return False

            net_pdu = build_network_pdu(
                enc_key=self.enc_key,
                priv_key=self.priv_key,
                nid=self.nid,
                ctl=False,
                ttl=self.ttl if ttl is None else ttl,
                seq=seq,
                src=self.provisioner_addr,
                dst=dst,
                iv_index=self.iv_index,
                transport_pdu=lower_transport_pdu,
                net_mic_len=4,
            )

            proxy_pdu = build_proxy_pdu(net_pdu, PROXY_MSG_TYPE_NETWORK_PDU)

            _LOGGER.info(
                "Sending mesh message: seq=%d src=0x%04X dst=0x%04X proxy=%s",
                seq, self.provisioner_addr, dst, proxy_pdu.hex(),
            )

            try:
                await self._client.write_gatt_char(
                    self._data_in_char,
                    proxy_pdu,
                    response=False,
                )
                return True
            except Exception as err:
                _LOGGER.error("GATT write failed: %s", err)
                self._client = None
                return False
