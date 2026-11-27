"""High-level API for Theben DIMAX 540 App B dimmer."""

import asyncio
import base64
import json
import logging

from homeassistant.core import HomeAssistant

from .mesh_gateway import MeshGateway
from .mesh_messages import (
    PARAM_LINE_VOLTAGE, PARAM_OUTPUT_CURRENT, PARAM_TEMPERATURE, PARAM_MIN_DIM_LEVEL,
    OP_ONOFF_STATUS, OP_LEVEL_STATUS, OP_DEVICE_PARAM_STATUS,
    parse_onoff_status, parse_level_status, parse_device_parameter_status,
    generate_tid, level_set_unack, level_get, onoff_set_unack, onoff_get,
    device_parameter_get,
)
from . import seq_store
from .const import (
    CONF_NETKEY, CONF_APPKEY, CONF_PROVISIONER_ADDR, CONF_DIMMER_ADDR,
    CONF_IV_INDEX, CONF_NETKEY_INDEX, CONF_APPKEY_INDEX, CONF_SEQ,
    CONF_DEVICE_ADDRESS, CONF_TTL,
    LEVEL_RAW_MIN, LEVEL_RAW_MAX,
)

_LOGGER = logging.getLogger(__name__)


SEQ_STARTUP_MARGIN = 2000  # guard against replay rejection after an unclean shutdown.
# This margin is applied once, to whichever value the gateway's SEQ counter
# is initialized with on first startup after a restart (see
# ThebenDIMAXAPI.async_initialize_seq below) -- with per-message
# persistence in place, it only ever gets used once per *unclean* shutdown.


class ThebenDIMAXAPI:
    """API for controlling one Theben DIMAX dimmer via the shared BLE Mesh gateway."""

    def __init__(self, hass: HomeAssistant, entry, gateway: MeshGateway):
        self.hass = hass
        self._entry = entry
        self._gateway = gateway
        self._coordinator = None
        config = entry.data
        self._config = config
        self._state = {
            "on": False, "brightness": 0, "level": -32768,
            "power": None, "line_voltage": None, "output_current_ma": None,
            "temperature": None, "min_dim_level_pct": None,
        }

        self._netkey = bytes.fromhex(config[CONF_NETKEY])
        self._appkey = bytes.fromhex(config[CONF_APPKEY])
        self._provisioner_addr = config[CONF_PROVISIONER_ADDR]
        self._dimmer_addr = config[CONF_DIMMER_ADDR]
        self._iv_index = config[CONF_IV_INDEX]
        self._netkey_index = config.get(CONF_NETKEY_INDEX, 0)
        self._appkey_index = config.get(CONF_APPKEY_INDEX, 0)
        self._ttl = config.get(CONF_TTL, 8)
        self._device_address = config.get(CONF_DEVICE_ADDRESS, "").upper()

        # SEQ is now tracked per mesh network (one counter shared by every
        # dimmer through the gateway), not per device -- keyed by the
        # provisioner's own address, since that's the identity every
        # receiving node's replay cache actually keys off of. See
        # async_initialize_seq() for how this reconciles with seq_store.
        self._seq_key = f"provisioner_{self._provisioner_addr:04x}"

    @property
    def dimmer_addr(self) -> int:
        return self._dimmer_addr

    def set_coordinator(self, coordinator):
        """Wired up after construction so mesh state updates -- including
        unsolicited ones from a manual switch press, if this dimmer is
        set up in the app to publish them to us -- can push straight to
        HA instead of waiting for the next poll cycle."""
        self._coordinator = coordinator

    def register_with_gateway(self):
        """Make this dimmer reachable through the shared gateway."""
        self._gateway.register_device(
            dimmer_addr=self._dimmer_addr,
            mac=self._device_address,
            appkey=self._appkey,
            appkey_index=self._appkey_index,
            ttl=self._ttl,
            listener=self._on_mesh_message,
        )

    def unregister_from_gateway(self):
        self._gateway.unregister_device(self._dimmer_addr)

    # ------------------------------------------------------------------ #
    #  SEQ
    # ------------------------------------------------------------------ #

    async def async_initialize_seq(self):
        """Reconcile the gateway's SEQ counter against the persistent
        seq_store registry and this entry's own config data, then persist
        the result. Call this once per entry, right after construction --
        but the actual counter only gets (re)initialized the FIRST time
        it's called for a given gateway; every dimmer after the first one
        just confirms it's not stale, since they all share one counter.

        Config entry data alone isn't enough to guarantee SEQ only moves
        forward -- see seq_store.py's module docstring for why (delete+
        re-add, stale re-import). This takes whichever is higher, the
        config-entry value or the independently-tracked high-water mark.
        """
        config_seq = self._config.get(CONF_SEQ, 0)
        known = await seq_store.get_known_seq(self.hass, self._seq_key)

        if not self._gateway.seq_initialized:
            seq = max(config_seq, known) + SEQ_STARTUP_MARGIN
            self._gateway.set_seq(seq)
            self._gateway.seq_initialized = True
            self._gateway.set_seq_persist_callback(self._on_seq_advanced)
            _LOGGER.info(
                "Gateway SEQ counter initialized to %d (config=%d, "
                "registry=%d, +%d startup margin)",
                seq, config_seq, known, SEQ_STARTUP_MARGIN,
            )
            await seq_store.record_seq(self.hass, self._seq_key, seq)
        elif known > config_seq:
            _LOGGER.info(
                "SEQ registry (%d) is ahead of this entry's config data "
                "(%d) -- gateway is already using the registry value via "
                "an earlier-initialized entry.", known, config_seq,
            )

    def _on_seq_advanced(self, seq: int):
        """Fired by the gateway after every message sent to ANY dimmer on
        this mesh. Persisting on every send (not just periodically) is
        what makes a delete+re-add or a mid-session crash safe -- see
        seq_store.py."""
        self.hass.async_create_task(seq_store.record_seq(self.hass, self._seq_key, seq))

    def get_seq(self) -> int:
        return self._gateway.get_seq()

    # ------------------------------------------------------------------ #
    #  MESH -> HA STATE
    # ------------------------------------------------------------------ #

    def _on_mesh_message(self, opcode: int, params: bytes):
        """Callback registered with the gateway for this dimmer's unicast
        address. Decodes the Access-layer message into the higher-level
        state dict this API/coordinator/entities work with."""
        if opcode == OP_LEVEL_STATUS:
            state = parse_level_status(params)
            _LOGGER.info("Generic Level Status: %s", state)
            # NOTE: do NOT infer on/off from level -- this device keeps
            # the last-set Level even while switched off (it's "brightness
            # to resume to", not "current live brightness"). OnOff Status
            # is the only authoritative source for on/off state.
            self._on_mesh_state({"level": state.get("level", -32768)})

        elif opcode == OP_ONOFF_STATUS:
            state = parse_onoff_status(params)
            _LOGGER.info("OnOff Status: %s", state)
            self._on_mesh_state({"on": state.get("on", False)})

        elif opcode == OP_DEVICE_PARAM_STATUS:
            state = parse_device_parameter_status(params)
            _LOGGER.info("Device Parameter Status: %s", state)
            if state:
                self._on_mesh_state(state)

    def _on_mesh_state(self, state: dict):
        """Merge a partial state update and push it straight to HA.

        Pushing immediately (rather than only updating self._state and
        waiting for the next poll) is what makes a manually-triggered
        change show up in HA promptly instead of up to POLL_INTERVAL
        seconds later -- see coordinator.py.
        """
        _LOGGER.info("Mesh state update: %s", state)
        self._state.update(state)

        if "level" in state:
            level = state["level"]  # raw signed 16-bit as reported by the device
            span = LEVEL_RAW_MAX - LEVEL_RAW_MIN
            pct = (level - LEVEL_RAW_MIN) / span
            self._state["brightness"] = max(0, min(255, int(round(pct * 255))))

        if "line_voltage" in state or "output_current_ma" in state:
            # Power(W) = LineVoltage(V) x OutputCurrent(mA) / 1000 -- the
            # exact formula the Theben app's own EnergyMeter property uses.
            v = self._state.get("line_voltage")
            i = self._state.get("output_current_ma")
            if v is not None and i is not None:
                self._state["power"] = round(v * i / 1000, 1)

        if self._coordinator is not None:
            # coordinator.data / entities expect {channel: state_dict} --
            # same shape _async_update_data() returns. Only channel 0 is
            # currently supported (see the hardcoded channels=[0] in
            # __init__.py), so that's the only key here too.
            self._coordinator.async_set_updated_data({0: dict(self._state)})

    # ------------------------------------------------------------------ #
    #  CONTROL
    # ------------------------------------------------------------------ #

    async def turn_on(self, brightness: int | None = None, channel: int = 0) -> bool:
        """Turn on the dimmer. brightness: 0-255 (HA scale), or None.

        This device only implements Generic OnOff Server (0x1000) and
        Generic Level Server (0x1002) -- there is NO Light Lightness model.

        When brightness is None (the on/off button -- not the slider) we
        send ONLY OnOff Set(True) and deliberately do NOT send a Level
        Set. The device retains its last Level across an off/on cycle on
        its own, so leaving Level alone here is what makes off-then-on
        come back at the same dimming level instead of jumping elsewhere.

        When brightness IS given (a slider drag) we send OnOff Set(True)
        followed by a Generic Level Set for that specific brightness.
        """
        element_addr = self._dimmer_addr + channel

        await self._gateway.send(element_addr, onoff_set_unack(on=True, tid=generate_tid()))

        if brightness is None:
            _LOGGER.info(
                "Turn on (on/off button): OnOff only, device resumes "
                "its own last Level, element=0x%04X", element_addr,
            )
            success = True
            resulting_brightness = self._state.get("brightness") or 255
            resulting_level = self._state.get("level", -32768)
        else:
            span = LEVEL_RAW_MAX - LEVEL_RAW_MIN
            mesh_level = int(round((brightness / 255) * span)) + LEVEL_RAW_MIN
            mesh_level = max(-32768, min(32767, mesh_level))

            _LOGGER.info(
                "Turn on: brightness=%d -> level=%d, element=0x%04X",
                brightness, mesh_level, element_addr,
            )

            success = await self._gateway.send(
                element_addr, level_set_unack(level=mesh_level, tid=generate_tid())
            )
            resulting_brightness = brightness
            resulting_level = mesh_level

        if success:
            # update(), not reassign -- reassigning drops power/voltage/
            # current/temperature/min_dim_level_pct from state until the
            # next poll, which made sensors flicker to unknown on every
            # on/off/brightness call.
            self._on_mesh_state({
                "on": True,
                "brightness": resulting_brightness,
                "level": resulting_level,
            })

        return success

    async def turn_off(self, channel: int = 0) -> bool:
        """Turn off the dimmer."""
        element_addr = self._dimmer_addr + channel
        success = await self._gateway.send(
            element_addr, onoff_set_unack(on=False, tid=generate_tid())
        )

        if success:
            self._on_mesh_state({"on": False, "brightness": 0, "level": -32768})

        return success

    async def set_brightness(self, brightness: int, channel: int = 0) -> bool:
        """Set brightness (0-255 HA scale). Turns off if brightness=0."""
        if brightness == 0:
            return await self.turn_off(channel)
        return await self.turn_on(brightness, channel)

    async def get_state(self, channel: int = 0) -> dict:
        """Get current state (returns cached state)."""
        return dict(self._state)

    async def request_state(self, channel: int = 0) -> bool:
        """Request state from dimmer (sends Get messages)."""
        element_addr = self._dimmer_addr + channel
        await self._gateway.send(element_addr, onoff_get())
        await asyncio.sleep(0.3)
        await self._gateway.send(element_addr, level_get())
        await asyncio.sleep(0.3)
        await self._gateway.send(element_addr, device_parameter_get(PARAM_LINE_VOLTAGE))
        await asyncio.sleep(0.3)
        await self._gateway.send(element_addr, device_parameter_get(PARAM_OUTPUT_CURRENT))
        await asyncio.sleep(0.3)
        await self._gateway.send(element_addr, device_parameter_get(PARAM_TEMPERATURE))
        await asyncio.sleep(0.3)
        await self._gateway.send(element_addr, device_parameter_get(PARAM_MIN_DIM_LEVEL))
        return True

    async def disconnect(self):
        """No-op for a single dimmer -- the gateway connection is shared
        and stays up for as long as ANY registered dimmer needs it. See
        unregister_from_gateway(), used on unload instead."""
        return


def parse_theben_export(export_data: dict) -> list[dict]:
    """Parse the Theben DIMAX app project export JSON."""
    devices = []

    net_keys = json.loads(export_data.get("network_keys", "[]"))
    if not net_keys:
        raise ValueError("No network keys found in export")

    net_key_raw = base64.b64decode(net_keys[0]["Key"])
    netkey_hex = net_key_raw.hex()
    netkey_index = net_keys[0].get("KeyIndex", 0)

    app_keys = json.loads(export_data.get("application_keys", "[]"))
    if not app_keys:
        raise ValueError("No application keys found in export")

    # Index by KeyIndex, not array position -- a project can have several
    # AppKeys (one per "zone"/purpose), and which one is actually bound to
    # a given dimmer is per-node (node["AddedAppKeys"]), not necessarily
    # the first one in this list.
    app_keys_by_index = {k.get("KeyIndex", 0): k for k in app_keys}

    mesh_network = json.loads(export_data.get("mesh_network", "{}"))
    iv_index = mesh_network.get("IvIndex", 0)

    seq_numbers = json.loads(mesh_network.get("SequenceNumbers", "[]"))
    seq_by_addr = {s["address"]: s["value"] for s in seq_numbers}

    nodes = json.loads(export_data.get("provisioned_mesh_nodes", "[]"))

    for node in nodes:
        if node.get("NodeName") == "Provisioner":
            continue

        unicast = node.get("UnicastAddress", 0)
        uuid = node.get("Uuid", "")
        ttl = node.get("Ttl", 8)

        uuid_clean = uuid.replace("-", "").lower()
        if uuid_clean.startswith("00000000000000000000"):
            mac_hex = uuid_clean[-12:]
            mac_address = ":".join(mac_hex[i:i+2] for i in range(0, 12, 2)).upper()
        else:
            mac_address = ""

        provisioners = json.loads(export_data.get("provisioners", "[]"))
        provisioner_addr = provisioners[0].get("ProvisionerAddress", 1) if provisioners else 1

        provisioner_seq = seq_by_addr.get(provisioner_addr, 0)
        node_seq = max(node.get("SequenceNumber", 0), seq_by_addr.get(unicast, 0))
        safe_seq = max(provisioner_seq, node_seq, 787) + 10

        # Which AppKey is actually bound to THIS node.
        added_app_keys = json.loads(node.get("AddedAppKeys", "[]"))
        bound_indexes = [k.get("Index") for k in added_app_keys if "Index" in k]

        if not bound_indexes:
            _LOGGER.warning(
                "Device %s has no AddedAppKeys in the export -- falling "
                "back to AppKey index 0. If this device doesn't respond "
                "to anything, this is the first thing to check.",
                node.get("NodeName"),
            )
            node_appkey_index = 0
        else:
            if len(bound_indexes) > 1:
                _LOGGER.warning(
                    "Device %s has multiple AppKeys bound (%s) -- this "
                    "integration only supports one AppKey per device, "
                    "using index %d.",
                    node.get("NodeName"), bound_indexes, bound_indexes[0],
                )
            node_appkey_index = bound_indexes[0]

        if node_appkey_index not in app_keys_by_index:
            raise ValueError(
                f"Device {node.get('NodeName')} is bound to AppKey index "
                f"{node_appkey_index}, but the export's application_keys "
                f"list doesn't contain that index at all."
            )

        appkey_hex = base64.b64decode(
            app_keys_by_index[node_appkey_index]["Key"]
        ).hex()
        appkey_index = node_appkey_index

        device_config = {
            CONF_DEVICE_ADDRESS: mac_address,
            CONF_NETKEY: netkey_hex,
            CONF_APPKEY: appkey_hex,
            CONF_PROVISIONER_ADDR: provisioner_addr,
            CONF_DIMMER_ADDR: unicast,
            CONF_IV_INDEX: iv_index,
            CONF_NETKEY_INDEX: netkey_index,
            CONF_APPKEY_INDEX: appkey_index,
            CONF_SEQ: safe_seq,
            CONF_TTL: ttl,
            "node_name": node.get("NodeName", f"DIMAX_{unicast}"),
        }
        devices.append(device_config)

        _LOGGER.info(
            "Parsed device: name=%s addr=0x%04X mac=%s ttl=%d seq=%d",
            node.get("NodeName"), unicast, mac_address, ttl, safe_seq,
        )

    return devices
