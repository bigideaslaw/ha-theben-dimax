"""Shared BLE Mesh Proxy gateway -- one persistent connection for every
dimmer in the same mesh network.


A BLE Mesh GATT Proxy connection is a gateway into the WHOLE mesh
network, not a private link to one device: once connected to any one
node's Proxy service, a message with dst set to a DIFFERENT dimmer's
unicast address gets relayed over the air to reach it, and that
dimmer's Status reply gets relayed back over the same GATT link. That's
what TTL/relay in the mesh spec is for.

Previous versions of this integration didn't use that -- every dimmer
opened its OWN BLE connection straight to its OWN radio, addressed only
ever to itself. With N dimmers that's N simultaneous BLE connections,
which is what forced the "disconnect between polls" workaround on
single-connection-slot adapters (see the old coordinator.py). This class
replaces all of that with ONE connection, shared by every registered
dimmer, addressed by dst -- so adding more dimmers to the mesh no longer
costs any more BLE connection slots than the first one did.

Which physical device's radio we're actually plugged into doesn't
matter to the mesh -- only the NetKey (shared network-wide, one per
gateway) and, per message, whichever AppKey is bound to the element
being addressed (which CAN differ per dimmer -- see the AppKey-binding
note in theben_api.py's parse_theben_export). If the currently-connected
dimmer loses power, ensure_connected() falls back to trying every other
registered dimmer's radio instead of failing outright.
"""

import asyncio
import logging
from typing import Callable

from homeassistant.core import HomeAssistant
from homeassistant.components.bluetooth import async_ble_device_from_address, async_get_scanner

from .mesh_proxy import MeshProxyConnection
from .mesh_crypto import k4, encrypt_access, decrypt_access
from .mesh_messages import decode_opcode

_LOGGER = logging.getLogger(__name__)

RECONNECT_DELAY = 5  # seconds between automatic reconnect attempts after a drop


class MeshGateway:
    """Owns the single shared MeshProxyConnection for a whole mesh network."""

    def __init__(self, hass: HomeAssistant, netkey: bytes, netkey_index: int,
                 provisioner_addr: int, iv_index: int):
        self.hass = hass
        self.netkey = netkey
        self.netkey_index = netkey_index
        self.provisioner_addr = provisioner_addr
        self.iv_index = iv_index

        # dimmer_addr -> {"mac", "appkey", "appkey_index", "ttl", "listener"}
        self._devices: dict[int, dict] = {}
        # AID -> AppKey bytes. Usually just one entry, but different
        # dimmers CAN be bound to different AppKeys within one mesh.
        self._appkeys_by_aid: dict[int, bytes] = {}

        self._connection: MeshProxyConnection | None = None
        self._connect_lock = asyncio.Lock()
        self._current_mac: str | None = None
        self._reconnecting = False

        # One SEQ counter for the whole gateway, not per dimmer -- SEQ
        # must be strictly increasing per message from OUR address
        # (provisioner_addr) regardless of which dimmer it's addressed
        # to; each receiving node tracks its own replay cache
        # independently anyway, so interleaving SEQ across multiple
        # destinations from one counter is exactly correct, not just
        # convenient.
        self._seq = 0
        self.seq_initialized = False
        self._seq_persist_cb: Callable[[int], None] | None = None

    # ------------------------------------------------------------------ #
    #  SEQ
    # ------------------------------------------------------------------ #

    def set_seq(self, seq: int):
        self._seq = max(seq, 0)

    def get_seq(self) -> int:
        return self._seq

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFFFFFF
        return self._seq

    def set_seq_persist_callback(self, callback: Callable[[int], None]):
        """callback(seq) fired after every message sent, so the caller can
        persist it (see seq_store.py). Only one caller should set this --
        see async_initialize_seq() in theben_api.py."""
        self._seq_persist_cb = callback

    # ------------------------------------------------------------------ #
    #  DEVICE REGISTRATION
    # ------------------------------------------------------------------ #

    def register_device(self, dimmer_addr: int, mac: str, appkey: bytes,
                         appkey_index: int, ttl: int, listener: Callable[[int, bytes], None]):
        """Register one dimmer as reachable through this gateway.

        mac is only used as a fallback connection target if we end up
        needing to connect via THIS dimmer's own radio -- it is not
        where messages addressed to dimmer_addr necessarily terminate;
        the mesh itself routes that.
        """
        self._devices[dimmer_addr] = {
            "mac": mac, "appkey": appkey, "appkey_index": appkey_index,
            "ttl": ttl, "listener": listener,
        }
        aid = k4(appkey)
        self._appkeys_by_aid[aid] = appkey
        _LOGGER.info(
            "Gateway: registered dimmer 0x%04X (mac=%s, AppKey index=%d, AID=0x%02X)",
            dimmer_addr, mac, appkey_index, aid,
        )

    def unregister_device(self, dimmer_addr: int):
        self._devices.pop(dimmer_addr, None)

    @property
    def device_count(self) -> int:
        return len(self._devices)

    # ------------------------------------------------------------------ #
    #  CONNECTION LIFECYCLE
    # ------------------------------------------------------------------ #

    @property
    def is_connected(self) -> bool:
        return self._connection is not None and self._connection.is_connected

    async def ensure_connected(self) -> bool:
        """Ensure the one shared BLE connection is up, trying every
        registered dimmer's radio (preferring whichever worked last) if
        it isn't."""
        if self.is_connected:
            return True

        async with self._connect_lock:
            if self.is_connected:  # someone else won the race while we waited
                return True

            try:
                scanners = async_get_scanner(self.hass)
                if not scanners:
                    _LOGGER.error(
                        "No Bluetooth scanners found! Ensure the 'Bluetooth' "
                        "integration is added in Settings > Devices & Services."
                    )
                    return False
            except Exception as err:
                _LOGGER.error("Bluetooth integration check failed: %s", err)
                return False

            macs = list({d["mac"] for d in self._devices.values() if d["mac"]})
            if not macs:
                _LOGGER.error("Gateway: no registered devices with a BLE MAC address")
                return False

            if self._current_mac in macs:
                macs.remove(self._current_mac)
                macs.insert(0, self._current_mac)

            for mac in macs:
                ble_device = async_ble_device_from_address(self.hass, mac, connectable=True)
                if not ble_device:
                    _LOGGER.warning(
                        "Gateway: %s not currently visible to the Bluetooth "
                        "scanner, trying the next registered device", mac,
                    )
                    continue

                connection = MeshProxyConnection(
                    ble_device=ble_device,
                    netkey=self.netkey,
                    netkey_index=self.netkey_index,
                    provisioner_addr=self.provisioner_addr,
                    iv_index=self.iv_index,
                )
                connection.set_transport_callback(self._on_transport_pdu)
                connection.set_disconnect_callback(self._on_disconnected)

                if await connection.connect():
                    self._connection = connection
                    self._current_mac = mac
                    _LOGGER.info("Gateway: connected to the mesh via %s", mac)
                    return True

                _LOGGER.warning(
                    "Gateway: failed to connect via %s, trying next "
                    "registered device", mac,
                )

            _LOGGER.error(
                "Gateway: could not connect to the mesh via any of %d "
                "registered device(s)", len(macs),
            )
            return False

    def _on_disconnected(self):
        """Fired from MeshProxyConnection when the GATT link drops
        unexpectedly. Schedules a reconnect in the background so every
        registered dimmer regains service automatically instead of only
        retrying whenever the next command happens to be sent."""
        self._connection = None
        if not self._reconnecting:
            self._reconnecting = True
            self.hass.async_create_task(self._reconnect_loop())

    async def _reconnect_loop(self):
        try:
            while not self.is_connected:
                await asyncio.sleep(RECONNECT_DELAY)
                _LOGGER.info("Gateway: attempting to reconnect to the mesh...")
                if await self.ensure_connected():
                    break
        finally:
            self._reconnecting = False

    async def disconnect(self):
        """Tear down the shared connection entirely -- only call this when
        the LAST dimmer using this gateway is being removed/unloaded."""
        if self._connection:
            await self._connection.disconnect()
            self._connection = None

    # ------------------------------------------------------------------ #
    #  ACCESS LAYER (AppKey encrypt/decrypt + routing)
    # ------------------------------------------------------------------ #

    def _on_transport_pdu(self, seq: int, src: int, dst: int, transport_pdu: bytes):
        """Called synchronously (from MeshProxyConnection's notification
        handler) for every Access-type Network PDU received, regardless
        of which registered dimmer it came from."""
        if len(transport_pdu) < 1:
            return
        lt_header = transport_pdu[0]
        akf = (lt_header >> 6) & 0x01
        aid = lt_header & 0x3F
        enc_and_mic = transport_pdu[1:]

        if not akf:
            return  # DevKey-encrypted -- not used by this integration

        appkey = self._appkeys_by_aid.get(aid)
        if appkey is None:
            _LOGGER.debug(
                "Status message uses AID 0x%02X, no registered AppKey "
                "matches -- ignoring", aid,
            )
            return

        device = self._devices.get(src)
        if device is None:
            _LOGGER.debug(
                "Status message from unregistered address 0x%04X -- "
                "ignoring", src,
            )
            return

        for aszmic in (False, True):
            try:
                decrypted = decrypt_access(
                    appkey,
                    aszmic=aszmic,
                    seq=seq,
                    src=src,
                    dst=dst,
                    iv_index=self.iv_index,
                    encrypted=enc_and_mic,
                )
                opcode, consumed = decode_opcode(decrypted)
                params = decrypted[consumed:]
                _LOGGER.info(
                    "Access message: opcode=0x%04X src=0x%04X params=%s",
                    opcode, src, params.hex(),
                )
                device["listener"](opcode, params)
                return
            except Exception:
                continue

    async def send(self, dst: int, access_payload: bytes) -> bool:
        """Encrypt access_payload with dst's bound AppKey and send it
        through the shared connection, reconnecting first if needed."""
        if not await self.ensure_connected():
            return False

        device = self._devices.get(dst)
        if device is None:
            _LOGGER.error("Gateway: no registered AppKey for dst=0x%04X", dst)
            return False

        appkey = device["appkey"]
        aid = k4(appkey)
        seq = self._next_seq()

        encrypted_access = encrypt_access(
            appkey,
            aszmic=False,
            seq=seq,
            src=self.provisioner_addr,
            dst=dst,
            iv_index=self.iv_index,
            payload=access_payload,
        )

        # Lower Transport header: SEG(1)=0 | AKF(1)=1 (using AppKey) | AID(6).
        lower_transport_header = 0x40 | (aid & 0x3F)
        lower_transport_pdu = bytes([lower_transport_header]) + encrypted_access

        success = await self._connection.send_transport_pdu(
            seq, dst, lower_transport_pdu, ttl=device.get("ttl"),
        )
        if success and self._seq_persist_cb:
            self._seq_persist_cb(seq)
        return success
