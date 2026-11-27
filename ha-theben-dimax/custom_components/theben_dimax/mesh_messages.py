"""BLE Mesh message construction for Generic OnOff and Light Lightness models."""

import struct
import os

# ------------------------------------------------------------------ #
#  OPCODES
# ------------------------------------------------------------------ #

# Generic OnOff
OP_ONOFF_GET = 0x8201
OP_ONOFF_SET = 0x8202
OP_ONOFF_SET_UNACK = 0x8203
OP_ONOFF_STATUS = 0x8204

# Generic Level
OP_LEVEL_GET = 0x8205
OP_LEVEL_SET = 0x8206
OP_LEVEL_SET_UNACK = 0x8207
OP_LEVEL_STATUS = 0x8208

# Light Lightness Actual
OP_LIGHTNESS_GET = 0x824B
OP_LIGHTNESS_SET = 0x824C
OP_LIGHTNESS_SET_UNACK = 0x824D
OP_LIGHTNESS_STATUS = 0x824E

# Theben vendor "Device Parameter" model (company ID 0x076D).
# Get/Set/Status opcodes determined from mesh traffic analysis -- these
# are NOT part of the public Bluetooth Mesh Model spec.
OP_DEVICE_PARAM_GET = 0xC06D07
OP_DEVICE_PARAM_SET = 0xC16D07
OP_DEVICE_PARAM_STATUS = 0xC26D07

# Parameter IDs (from the app's `DeviceParameters` enum) -- this is the
# first payload byte of a Device Parameter Get/Set/Status message.
PARAM_ALL = 15
PARAM_MIN_DIM_LEVEL = 16
PARAM_COUNTDOWN_TIMER = 17
PARAM_LINE_VOLTAGE = 18
PARAM_OUTPUT_CURRENT = 19
PARAM_TEMPERATURE = 253
PARAM_ERROR_FLAG = 254


def _encode_opcode(opcode: int) -> bytes:
    """Encode mesh opcode to bytes."""
    if opcode <= 0x7F:
        return bytes([opcode])
    elif opcode <= 0xBFFF:
        return struct.pack('>H', opcode)
    else:
        return struct.pack('>H', opcode >> 8) + bytes([opcode & 0xFF])


# ------------------------------------------------------------------ #
#  LIGHT LIGHTNESS MESSAGES
# ------------------------------------------------------------------ #

def lightness_set(lightness: int, tid: int, transition_time: int = 0,
                  delay: int = 0) -> bytes:
    """Light Lightness Set."""
    msg = _encode_opcode(OP_LIGHTNESS_SET)
    msg += struct.pack('<H', lightness & 0xFFFF)
    msg += bytes([tid & 0xFF])
    if transition_time or delay:
        msg += bytes([transition_time & 0xFF])
        msg += bytes([delay & 0xFF])
    return msg


def lightness_set_unack(lightness: int, tid: int, transition_time: int = 0,
                        delay: int = 0) -> bytes:
    """Light Lightness Set Unacknowledged."""
    msg = _encode_opcode(OP_LIGHTNESS_SET_UNACK)
    msg += struct.pack('<H', lightness & 0xFFFF)
    msg += bytes([tid & 0xFF])
    if transition_time or delay:
        msg += bytes([transition_time & 0xFF])
        msg += bytes([delay & 0xFF])
    return msg


def lightness_get() -> bytes:
    """Light Lightness Get."""
    return _encode_opcode(OP_LIGHTNESS_GET)


# ------------------------------------------------------------------ #
#  GENERIC ONOFF MESSAGES
# ------------------------------------------------------------------ #

def onoff_set(on: bool, tid: int, transition_time: int = 0,
              delay: int = 0) -> bytes:
    """Generic OnOff Set (acknowledged)."""
    msg = _encode_opcode(OP_ONOFF_SET)
    msg += bytes([0x01 if on else 0x00])
    msg += bytes([tid & 0xFF])
    if transition_time or delay:
        msg += bytes([transition_time & 0xFF])
        msg += bytes([delay & 0xFF])
    return msg


def onoff_set_unack(on: bool, tid: int, transition_time: int = 0,
                    delay: int = 0) -> bytes:
    """Generic OnOff Set Unacknowledged."""
    msg = _encode_opcode(OP_ONOFF_SET_UNACK)
    msg += bytes([0x01 if on else 0x00])
    msg += bytes([tid & 0xFF])
    if transition_time or delay:
        msg += bytes([transition_time & 0xFF])
        msg += bytes([delay & 0xFF])
    return msg


def onoff_get() -> bytes:
    """Generic OnOff Get."""
    return _encode_opcode(OP_ONOFF_GET)


# ------------------------------------------------------------------ #
#  GENERIC LEVEL MESSAGES
# ------------------------------------------------------------------ #

def level_set_unack(level: int, tid: int, transition_time: int = 0,
                    delay: int = 0) -> bytes:
    """Generic Level Set Unacknowledged.

    Level is little-endian signed int16 on the wire, matching the
    Bluetooth Mesh spec (all multi-octet Access-layer parameters are LE; only the opcode itself is
    the documented big-endian exception, which _encode_opcode already
    handles correctly). This was previously packed big-endian ('>h'),
    which silently scrambled every Level value sent -- e.g. an intended
    level of 0x4000 (16384) went out as bytes matching 0x0040 (64) once
    the receiving node parsed them as LE. That's what produced the
    narrow, seemingly-random "working window" documented in
    LEVEL_RAW_MIN/MAX in const.py -- those constants were calibrated
    while this bug was active and are not real hardware limits.
    """
    msg = _encode_opcode(OP_LEVEL_SET_UNACK)
    msg += struct.pack('<h', level)
    msg += bytes([tid & 0xFF])
    if transition_time or delay:
        msg += bytes([transition_time & 0xFF])
        msg += bytes([delay & 0xFF])
    return msg


def level_get() -> bytes:
    """Generic Level Get."""
    return _encode_opcode(OP_LEVEL_GET)


# ------------------------------------------------------------------ #
#  THEBEN VENDOR "DEVICE PARAMETER" MESSAGES (power/voltage/current/temp)
# ------------------------------------------------------------------ #

def device_parameter_get(param_id: int) -> bytes:
    """Request a single device parameter (see PARAM_* constants)."""
    return _encode_opcode(OP_DEVICE_PARAM_GET) + bytes([param_id & 0xFF])


def parse_device_parameter_status(data: bytes) -> dict:
    """Parse a Device Parameter Status message.
    Wire format: [ParameterId (1 byte)][value bytes...].
    Byte widths/scaling determined from mesh traffic:
      - LINE_VOLTAGE / OUTPUT_CURRENT / MIN_DIM_LEVEL / COUNTDOWN_TIMER:
        2-byte value, assumed little-endian (unverified -- if readings
        look wildly wrong, try big-endian instead, see comment below).
      - TEMPERATURE / ERROR_FLAG: 1-byte value.
    LineVoltage is in whole Volts, OutputCurrent in milliamps; the app
    computes Power(W) = LineVoltage * OutputCurrent / 1000.
    """
    if len(data) < 1:
        return {}
    param_id = data[0]
    value_bytes = data[1:]
    result = {"param_id": param_id, "raw": value_bytes.hex()}

    if param_id == PARAM_LINE_VOLTAGE and len(value_bytes) >= 2:
        result["line_voltage"] = struct.unpack('<H', value_bytes[0:2])[0]
    elif param_id == PARAM_OUTPUT_CURRENT and len(value_bytes) >= 2:
        result["output_current_ma"] = struct.unpack('<H', value_bytes[0:2])[0]
    elif param_id == PARAM_TEMPERATURE and len(value_bytes) >= 1:
        result["temperature"] = value_bytes[0]
    elif param_id == PARAM_MIN_DIM_LEVEL and len(value_bytes) >= 2:
        raw = struct.unpack('<H', value_bytes[0:2])[0]
        result["min_dim_level_pct"] = int(round(raw * 100 / 65535))
    elif param_id == PARAM_ERROR_FLAG and len(value_bytes) >= 1:
        result["error_flag"] = value_bytes[0]

    return result


# ------------------------------------------------------------------ #
#  STATUS PARSING
# ------------------------------------------------------------------ #

def decode_opcode(data: bytes) -> tuple[int, int]:
    """Decode opcode from access payload. Returns (opcode, bytes_consumed)."""
    if not data:
        return 0, 0
    if data[0] <= 0x7F:
        return data[0], 1
    elif data[0] <= 0xBF:
        return (data[0] << 8) | data[1], 2
    else:
        return (data[0] << 16) | (data[1] << 8) | data[2], 3


def parse_onoff_status(data: bytes) -> dict:
    """Parse Generic OnOff Status response."""
    if len(data) < 1:
        return {"on": False}
    result = {"on": bool(data[0] & 0x01)}
    if len(data) >= 3:
        result["target"] = bool(data[1] & 0x01)
        result["remaining_time"] = data[2]
    return result


def parse_lightness_status(data: bytes) -> dict:
    """Parse Light Lightness Status response."""
    if len(data) < 2:
        return {"lightness": 0}
    lightness = struct.unpack('<H', data[0:2])[0]
    result = {"lightness": lightness}
    if len(data) >= 5:
        result["target_lightness"] = struct.unpack('<H', data[2:4])[0]
        result["remaining_time"] = data[4]
    return result


def parse_level_status(data: bytes) -> dict:
    """Parse Generic Level Status response.

    Little-endian -- see the note in level_set_unack() above. This was
    also previously '>h', which is why the calibration readings in
    const.py's LEVEL_RAW_MIN/MAX comment (-1414..+642) look like a
    narrow, arbitrary window instead of the real -32768..32767 span:
    those were byte-swapped values coming back from the device.
    """
    if len(data) < 2:
        return {"level": 0}
    level = struct.unpack('<h', data[0:2])[0]
    result = {"level": level}
    if len(data) >= 5:
        result["target_level"] = struct.unpack('<h', data[2:4])[0]
        result["remaining_time"] = data[4]
    return result


def generate_tid() -> int:
    """Generate random Transaction Identifier."""
    return int.from_bytes(os.urandom(1), 'big')
