"""Constants for Theben DIMAX BLE Mesh integration."""

DOMAIN = "theben_dimax"

# BLE Mesh Proxy Service and Characteristics
MESH_PROXY_SERVICE_UUID = "00001828-0000-1000-8000-00805f9b34fb"
MESH_PROXY_DATA_IN_UUID = "00002add-0000-1000-8000-00805f9b34fb"
MESH_PROXY_DATA_OUT_UUID = "00002ade-0000-1000-8000-00805f9b34fb"

# Mesh Proxy PDU message types
PROXY_MSG_TYPE_NETWORK_PDU = 0x00
PROXY_MSG_TYPE_MESH_BEACON = 0x01
PROXY_MSG_TYPE_PROXY_CONFIG = 0x02

# SAR flags
SAR_COMPLETE = 0x00
SAR_FIRST = 0x01
SAR_CONTINUE = 0x02
SAR_LAST = 0x03

# Config entry keys
CONF_NETKEY = "netkey"
CONF_APPKEY = "appkey"
CONF_PROVISIONER_ADDR = "provisioner_addr"
CONF_DIMMER_ADDR = "dimmer_addr"
CONF_IV_INDEX = "iv_index"
CONF_NETKEY_INDEX = "netkey_index"
CONF_APPKEY_INDEX = "appkey_index"
CONF_SEQ = "seq"
CONF_DEVICE_ADDRESS = "device_address"
CONF_TTL = "ttl"

# Defaults
DEFAULT_TTL = 8
DEFAULT_NETKEY_INDEX = 0
DEFAULT_APPKEY_INDEX = 0
# The old 60s value was calibrated around the per-dimmer, connect-then-
# disconnect design -- each poll paid the full BLE connect overhead
# (~0.5-2s), so polling often was expensive. With the shared MeshGateway
# now holding one persistent connection, a poll is just a few small GATT
# writes over an already-open link, so this can be much shorter without
# meaningfully increasing radio load. This is also what bounds the worst-
# case latency for a manually-triggered change to show up in HA if this
# dimmer's OnOff/Level models aren't configured (in the Theben app) to
# publish status changes to the provisioner -- with no publish set up,
# nothing pushes to us unsolicited, so a manual switch flip is only ever
# noticed on the next poll.
POLL_INTERVAL = 10  # seconds

# Generic Level range for this device -- the full signed 16-bit span,
# using the standard mapping: Level = -32768 + (Percent/100.0) * 65535.0.
#
# The previous narrow values here (-1500..700) were NOT a real hardware
# limit -- they were calibrated while mesh_messages.py packed/unpacked
# Level as big-endian instead of the spec-required little-endian. That
# bug byte-swapped every Level value in both directions (commands sent
# AND status readings received), which made the dimmer's real behavior
# look like it only responded in a tiny window near zero. With the
# endianness fixed, the full range applies and this mapping is now a
# direct match for what the vendor app itself sends.
LEVEL_RAW_MIN = -32768
LEVEL_RAW_MAX = 32767
