"""
Firmata protocol implementation for AkaDako.

Handles encoding/decoding of Firmata messages over the MIDI transport, plus the
AkaDako proprietary SysEx messages (board version / UID / water temperature)
which exist in two variants: "slink" (with the 0x00 0x40 0x08 0x01 prefix) and
"legacy".

Cross-thread note: the MIDI transport delivers incoming bytes on a separate
(rtmidi callback) thread. Request/reply calls block the caller on a
``threading.Event`` that the callback thread sets when the matching reply
arrives; the waiter tables are guarded by a lock.
"""

import threading
import time

# Firmata protocol constants
START_SYSEX = 0xF0
END_SYSEX = 0xF7
SET_PIN_MODE = 0xF4
DIGITAL_MESSAGE = 0x90
ANALOG_MESSAGE = 0xE0
REPORT_ANALOG = 0xC0
REPORT_DIGITAL = 0xD0
REPORT_VERSION = 0xF9
SYSTEM_RESET = 0xFF

# SysEx commands
I2C_REQUEST = 0x76
I2C_REPLY = 0x77
I2C_CONFIG = 0x78
SERVO_CONFIG = 0x70
STRING_DATA = 0x71
QUERY_FIRMWARE = 0x79
EXTENDED_ANALOG = 0x6F
SAMPLING_INTERVAL = 0x7A

# AkaDako proprietary SysEx commands (see xcx-g2s akadako-board.js)
SLINK_ID = [0x00, 0x40, 0x08, 0x01]
ULTRASONIC_DISTANCE_QUERY = 0x01
WATER_TEMPERATURE_QUERY = 0x02
DEVICE_ENABLE = 0x03
FIRMWARE_NAME_QUERY = 0x0D
BOARD_UID_QUERY = 0x0E
BOARD_VERSION_QUERY = 0x0F

# Pin modes
PIN_MODE_INPUT = 0x00
PIN_MODE_OUTPUT = 0x01
PIN_MODE_ANALOG = 0x02
PIN_MODE_PWM = 0x03
PIN_MODE_SERVO = 0x04
PIN_MODE_I2C = 0x06
PIN_MODE_ONEWIRE = 0x07
PIN_MODE_PULLUP = 0x0B

# I2C read/write modes (mode << 3 occupies bits 3-4 of the request byte):
#   0=WRITE, 1=READ(once), 2=CONTINUOUS_READ, 3=STOP_READING
# NOTE (akadako-py-web fix): upstream set I2C_READ_ONCE=0x18, which is
# STOP_READING (mode 3) -- the board then sends no reply, so every I2C read
# times out (BME280 etc. report chip id 0x00). A one-shot read is mode 1 = 0x08.
I2C_WRITE = 0x00
I2C_READ = 0x08
I2C_READ_ONCE = 0x08


class _Waiter:
    """A pending request/reply slot resolved across threads."""

    __slots__ = ("event", "result")

    def __init__(self):
        self.event = threading.Event()
        self.result = None


def _decode_int16_from_two_7bit(b):
    """Decode a signed 16-bit value from two 7-bit bytes.

    Mirrors decodeInt16FromTwo7bitBytes() in akadako-board.js.
    """
    if len(b) < 2:
        return 0
    lsb = (b[0] | (b[1] << 7)) & 0xFF
    msb = (b[1] >> 1) | (0b11000000 if (b[1] >> 6) else 0)
    value = lsb | (msb << 8)
    if value & 0x8000:
        value -= 0x10000
    return value


class FirmataBoard:
    """Low-level Firmata board communication."""

    def __init__(self, transport):
        self._transport = transport
        self._analog_values = {}   # channel -> value (0-1023)
        self._digital_values = {}  # port -> bitmask
        self._version = ""

        # pending request/reply slots (resolved on the MIDI callback thread)
        self._lock = threading.Lock()
        self._i2c_waiters = {}          # (addr, reg) -> _Waiter(list[int])
        self._proprietary_waiters = {}  # key -> _Waiter(list[int])
        self._protocol = None           # 'slink' | 'legacy' | None

        # incoming byte parsing
        self._sysex_buffer = []
        self._parse_buffer = []
        self._parsing_sysex = False

        transport.set_data_callback(self._on_data)

    # ------------------------------------------------------------------
    # Incoming data parsing (runs on the MIDI callback thread)
    # ------------------------------------------------------------------

    def _on_data(self, data):
        for byte in data:
            if byte == START_SYSEX:
                self._parsing_sysex = True
                self._sysex_buffer = []
            elif byte == END_SYSEX:
                self._parsing_sysex = False
                self._process_sysex(self._sysex_buffer)
            elif self._parsing_sysex:
                self._sysex_buffer.append(byte)
            else:
                self._parse_buffer.append(byte)
                self._try_parse_midi()

    def _try_parse_midi(self):
        if len(self._parse_buffer) < 1:
            return
        cmd = self._parse_buffer[0] & 0xF0

        if cmd == ANALOG_MESSAGE and len(self._parse_buffer) >= 3:
            channel = self._parse_buffer[0] & 0x0F
            val = self._parse_buffer[1] | (self._parse_buffer[2] << 7)
            self._analog_values[channel] = val
            self._parse_buffer = []
        elif cmd == DIGITAL_MESSAGE and len(self._parse_buffer) >= 3:
            port = self._parse_buffer[0] & 0x0F
            val = self._parse_buffer[1] | (self._parse_buffer[2] << 7)
            self._digital_values[port] = val
            self._parse_buffer = []
        elif self._parse_buffer[0] == REPORT_VERSION and len(self._parse_buffer) >= 3:
            major = self._parse_buffer[1]
            minor = self._parse_buffer[2]
            self._version = f"{major}.{minor}"
            self._parse_buffer = []
        elif len(self._parse_buffer) > 3:
            self._parse_buffer.pop(0)

    def _process_sysex(self, data):
        if not data:
            return
        cmd = data[0]

        if cmd == I2C_REPLY and len(data) >= 5:
            addr = data[1] | (data[2] << 7)
            reg = data[3] | (data[4] << 7)
            values = []
            for i in range(5, len(data) - 1, 2):
                values.append(data[i] | (data[i + 1] << 7))
            self._resolve(self._i2c_waiters, (addr, reg), values)
            # also resolve a register-agnostic waiter, if any
            self._resolve(self._i2c_waiters, (addr, None), values)
        elif cmd == 0x00 and len(data) >= 5 and list(data[1:4]) == SLINK_ID[1:]:
            # slink proprietary reply: 0x00 0x40 0x08 0x01 <sub> <payload...>
            self._protocol = "slink"
            sub = data[4]
            self._dispatch_proprietary(sub, list(data[5:]))
        elif cmd in (
            FIRMWARE_NAME_QUERY, BOARD_UID_QUERY, BOARD_VERSION_QUERY,
            WATER_TEMPERATURE_QUERY, ULTRASONIC_DISTANCE_QUERY,
        ):
            # legacy proprietary reply: <sub> <payload...>
            self._dispatch_proprietary(cmd, list(data[1:]))

    def _dispatch_proprietary(self, sub, payload):
        if sub in (WATER_TEMPERATURE_QUERY, ULTRASONIC_DISTANCE_QUERY):
            # payload[0] is the pin; the rest is the value
            pin = payload[0] if payload else None
            self._resolve(self._proprietary_waiters, (sub, pin), payload[1:])
            self._resolve(self._proprietary_waiters, (sub, None), payload[1:])
        else:
            self._resolve(self._proprietary_waiters, sub, payload)

    def _resolve(self, table, key, value):
        with self._lock:
            waiter = table.pop(key, None)
        if waiter is None:
            return
        waiter.result = value
        waiter.event.set()

    def _make_waiter(self, table, key):
        waiter = _Waiter()
        with self._lock:
            table[key] = waiter
        return waiter

    # ------------------------------------------------------------------
    # Outgoing primitives
    # ------------------------------------------------------------------

    def set_pin_mode(self, pin, mode):
        self._transport.write([SET_PIN_MODE, pin, mode])

    def digital_write(self, pin, value):
        port = pin >> 3
        bit = pin & 0x07
        self._digital_values.setdefault(port, 0)
        if value:
            self._digital_values[port] |= (1 << bit)
        else:
            self._digital_values[port] &= ~(1 << bit)
        self._transport.write([
            DIGITAL_MESSAGE | port,
            self._digital_values[port] & 0x7F,
            (self._digital_values[port] >> 7) & 0x7F,
        ])

    def digital_read(self, pin):
        port = pin >> 3
        bit = pin & 0x07
        return (self._digital_values.get(port, 0) >> bit) & 0x01

    def analog_read(self, channel):
        """Read the latest value of an analog *channel* (0-5), range 0-1023."""
        return self._analog_values.get(channel, 0)

    def analog_write(self, pin, value):
        """Write a value via the analog message (used for PWM and servo).

        Matches firmata-io: pins <= 15 use ANALOG_MESSAGE.
        """
        if pin > 15 or value > 0x3FFF:
            self._transport.write([
                START_SYSEX, EXTENDED_ANALOG, pin,
                value & 0x7F, (value >> 7) & 0x7F,
                END_SYSEX,
            ])
        else:
            self._transport.write([
                ANALOG_MESSAGE | pin,
                value & 0x7F, (value >> 7) & 0x7F,
            ])

    def report_analog(self, channel, enable=True):
        self._transport.write([REPORT_ANALOG | channel, 1 if enable else 0])

    def report_digital(self, port, enable=True):
        self._transport.write([REPORT_DIGITAL | port, 1 if enable else 0])

    def servo_write(self, pin, angle):
        # AkaDako sends servo position through the analog message (no SERVO_CONFIG).
        self.analog_write(pin, angle)

    def send_sysex(self, data):
        """Send a raw SysEx body (without the 0xF0/0xF7 framing)."""
        self._transport.write([START_SYSEX] + list(data) + [END_SYSEX])

    # ------------------------------------------------------------------
    # I2C
    # ------------------------------------------------------------------

    def i2c_config(self, delay=0):
        self._transport.write([
            START_SYSEX, I2C_CONFIG,
            delay & 0xFF, (delay >> 8) & 0xFF,
            END_SYSEX,
        ])

    def i2c_write(self, address, data):
        """Write bytes to an I2C device. ``data`` already includes the register."""
        msg = [START_SYSEX, I2C_REQUEST, address & 0x7F, I2C_WRITE]
        for byte in data:
            msg.extend([byte & 0x7F, (byte >> 7) & 0x7F])
        msg.append(END_SYSEX)
        self._transport.write(msg)

    def i2c_read_once(self, address, register, length, timeout=2.0):
        """Read ``length`` bytes from ``address``/``register``; returns list[int]."""
        key = (address, register)
        waiter = self._make_waiter(self._i2c_waiters, key)
        self._transport.write([
            START_SYSEX, I2C_REQUEST,
            address & 0x7F, I2C_READ_ONCE,
            register & 0x7F, (register >> 7) & 0x7F,
            length & 0x7F, (length >> 7) & 0x7F,
            END_SYSEX,
        ])
        if waiter.event.wait(timeout):
            return waiter.result
        with self._lock:
            self._i2c_waiters.pop(key, None)
        return []

    # ------------------------------------------------------------------
    # AkaDako proprietary SysEx
    # ------------------------------------------------------------------

    def _sysex_send(self, cmd, payload=None):
        payload = payload or []
        if self._protocol == "legacy":
            self.send_sysex([cmd] + list(payload))
        else:  # default to slink framing
            self.send_sysex(SLINK_ID + [cmd] + list(payload))

    def _query(self, cmd, key, payload=None, timeout=0.4):
        waiter = self._make_waiter(self._proprietary_waiters, key)
        self._sysex_send(cmd, payload)
        if waiter.event.wait(timeout):
            return waiter.result
        with self._lock:
            self._proprietary_waiters.pop(key, None)
        return None

    def detect_protocol(self, timeout=0.4):
        """Detect slink vs legacy by issuing a firmware-name query."""
        if self._protocol:
            return self._protocol
        # try slink first
        self._protocol = "slink"
        reply = self._query(FIRMWARE_NAME_QUERY, FIRMWARE_NAME_QUERY, timeout=timeout)
        if reply is not None:
            self._protocol = "slink"
        else:
            self._protocol = "legacy"
        return self._protocol

    def board_version(self, timeout=0.4):
        """Return the board version dict {type, major, minor} or None."""
        self.detect_protocol()
        reply = self._query(BOARD_VERSION_QUERY, BOARD_VERSION_QUERY, timeout=timeout)
        if not reply:
            return None
        if self._protocol == "slink" and len(reply) >= 4:
            value = (reply[0] | (reply[1] << 7) | (reply[2] << 14) | (reply[3] << 21))
        elif len(reply) >= 2:
            value = reply[0] | (reply[1] << 7)
        else:
            return None
        return {
            "type": (value >> 10) & 0x0F,
            "major": (value >> 6) & 0x0F,
            "minor": value & 0x3F,
        }

    def board_uid(self, timeout=0.4):
        """Return the 12-byte board UID as a list[int], or None."""
        self.detect_protocol()
        reply = self._query(BOARD_UID_QUERY, BOARD_UID_QUERY, timeout=timeout)
        if not reply:
            return None
        return list(_decode7bit(reply))[:12]

    def get_water_temp(self, pin, timeout=2.0):
        """Return the raw signed water-temperature value for a pin, or None."""
        self.detect_protocol()
        reply = self._query(
            WATER_TEMPERATURE_QUERY, (WATER_TEMPERATURE_QUERY, pin),
            payload=[pin], timeout=timeout,
        )
        if reply is None or len(reply) < 2:
            return None
        return _decode_int16_from_two_7bit(reply)

    def enable_device(self, device_id):
        """Send a DEVICE_ENABLE command (used by the on-board IR remote)."""
        self._sysex_send(DEVICE_ENABLE, [device_id])

    def set_sampling_interval(self, interval_ms):
        self._transport.write([
            START_SYSEX, SAMPLING_INTERVAL,
            interval_ms & 0x7F, (interval_ms >> 7) & 0x7F,
            END_SYSEX,
        ])

    def reset(self):
        self._transport.write([SYSTEM_RESET])


def _decode7bit(data):
    """Unpack a dense 7-bit-encoded byte stream (decode7bitBytes in JS)."""
    out = []
    bit_buffer = 0
    bit_count = 0
    for b in data:
        bit_buffer |= (b << bit_count)
        bit_count += 7
        while bit_count >= 8:
            out.append(bit_buffer & 0xFF)
            bit_buffer >>= 8
            bit_count -= 8
    return out


class I2CBus:
    """I2C adapter passed to sensor drivers.

    Provides the minimal pair the drivers rely on:
        bus.read(addr, reg, length) -> list[int]
        bus.write(addr, reg, data)   # data: int | list[int]
    plus a few endian helpers.
    """

    def __init__(self, firmata, pacing=0.01):
        self._f = firmata
        self._pacing = pacing

    def read(self, addr, reg, length):
        data = self._f.i2c_read_once(addr, reg, length)
        return list(data)

    def write(self, addr, reg, data):
        if isinstance(data, int):
            data = [data]
        self._f.i2c_write(addr, [reg] + list(data))
        if self._pacing:
            time.sleep(self._pacing)

    def read8(self, addr, reg):
        d = self.read(addr, reg, 1)
        return d[0] if d else 0

    def read16_le(self, addr, reg):
        d = self.read(addr, reg, 2)
        if len(d) < 2:
            return 0
        return d[0] | (d[1] << 8)

    def read16_be(self, addr, reg):
        d = self.read(addr, reg, 2)
        if len(d) < 2:
            return 0
        return (d[0] << 8) | d[1]
