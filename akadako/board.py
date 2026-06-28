"""
AkaDako board interface.

API design is adapted from the official JavaScript library (akadako.js):
https://akadako.com/javascript/
The JS library is async/await-centric because the browser has no blocking I/O;
this Python port is synchronous (blocking), matching Python physical-computing
conventions (gpiozero, pyfirmata, Adafruit, ...).

Method naming convention:
- fetch_xxx()  -> blocking call that returns a value
- run_xxx()    -> blocking call that performs a side effect
- xxx()        -> returns a cached/last-known value (no I/O round-trip)
- is_xxx       -> property

Pin numbers come from akadako.js (see akadako.constants); they are the board
pin numbers / analog channels expected by the AkaDako firmware.
"""

import math
import time

from akadako import constants as C
from akadako.constants import Color, Rainbow
from akadako.errors import (
    AkaDakoError,
    NotSupportedError,
    DisconnectedError,
    InvalidValueError,
    InvalidConnectorError,
    BusyError,
)
from akadako.firmata import (
    FirmataBoard, I2CBus, PIN_MODE_INPUT, PIN_MODE_ANALOG, PIN_MODE_PULLUP,
)
from akadako.transport import MidiTransport
from akadako.sensors import BME280, LTR303, ADXL345, KXTJ3, VL53L0X
from akadako.share import ShareServer

# PWM resolution on the AkaDako MIDI path (ATmega328 default).
_PWM_RESOLUTION = 255

# Digital input pins that must be put into INPUT mode at connect time, and the
# analog channels put into ANALOG mode. Mirrors onBoardReady() in
# xcx-g2s akadako-board.js. The firmware only reports the state of pins/channels
# that have been set to the matching mode, so without this digital reads stay 0.
_DIGITAL_INPUT_PINS = (6, 9, 10, 11)  # fixed pin config (at least for STEAM Tool)
_ANALOG_INPUT_CHANNELS = (0, 1, 2, 3, 4, 5)

# NeoPixel SysEx command bytes (node-pixel-constants.js)
_PIXEL_COMMAND = 0x51
_PIXEL_CONFIG = 0x01
_PIXEL_SHOW = 0x02
_PIXEL_SET_PIXEL = 0x03
_COLOR_ORDER_GRB = 0x00

_DEFAULT_NEOPIXEL_LENGTH = 3

# gamma correction table (gamma 2.8), as in akadako-board.js
_GAMMA = [math.floor((i / 255) ** 2.8 * 255 + 0.5) for i in range(256)]


class _Servo:
    """Servo helper mirroring xcx-g2s servo.js (zero-centered, speed control)."""

    def __init__(self, board, pin):
        self._board = board
        self._pin = pin
        self.angle = 0
        self.is_busy = False

    def turn(self, angle):
        servo_value = 90 - angle
        servo_value = min(180, max(0, servo_value))
        self._board._firmata.set_pin_mode(self._pin, 0x04)  # MODES.SERVO
        self._board._firmata.servo_write(self._pin, int(round(servo_value)))
        time.sleep(self._board._sending_interval)

    def turn_with_speed(self, angle, speed):
        if speed <= 0:
            return
        speed = min(100, speed)
        angle = min(90, max(-90, angle))
        start_angle = self.angle
        if angle == start_angle or speed == 100:
            self.angle = angle
            self.turn(self.angle)
            return
        step = abs(round(((angle - start_angle) / 180.0) * (100.0 / (speed / 40.0))))
        if step == 0:
            self.angle = angle
            self.turn(self.angle)
            return
        step_angle = (angle - start_angle) / step
        for _ in range(step):
            self.angle += step_angle
            self.turn(self.angle)


class AkaDako:
    """AkaDako board interface (communicates via Firmata over MIDI)."""

    def __init__(self, transport=None, firmata=None):
        self._transport = transport
        self._firmata = firmata
        self._connected = False
        self._on_disconnected = None
        self._sending_interval = 0.01

        self.version = None  # {"type", "major", "minor"} or None
        self._cache = {}
        self._bus = I2CBus(firmata) if firmata else None

        # lazily-initialised sensors
        self._env_sensor = None
        self._brightness_sensor = None
        self._accelerometer = None
        self._vl53l0x = None

        # servos
        self._servos = {}

        # optical distance noise reduction
        self._optical_samples = []
        self._optical_samples_size = 9

        # pwm interval tracking
        self._pwm_last_ts = {}

        # neopixel state: list of {"pin", "length", "colors"}
        self._neopixel = []

        # share server
        self._share = ShareServer(board_is_connected=lambda: self._connected)

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    @classmethod
    def connect(cls, name_filter=None):
        """Connect to an AkaDako board via MIDI and return the board.

        Args:
            name_filter: A substring (or iterable of substrings) to match in
                MIDI port names. Defaults to the known AkaDako port names
                ("STEAM BOX", "MidiDako", "AkaDako").
        """
        transport = MidiTransport.find_and_open(name_filter)
        firmata = FirmataBoard(transport)
        board = cls(transport, firmata)
        board._connected = True

        # Set pin modes and enable reporting, mirroring onBoardReady() in
        # akadako-board.js. The firmware only reports pins/channels that have
        # been put into the matching mode, so the mode must be set first.
        for pin in _DIGITAL_INPUT_PINS:
            firmata.set_pin_mode(pin, PIN_MODE_INPUT)
        for port in range(2):
            firmata.report_digital(port, True)
        for channel in _ANALOG_INPUT_CHANNELS:
            firmata.set_pin_mode(channel, PIN_MODE_ANALOG)
            firmata.report_analog(channel, True)
        firmata.i2c_config(delay=0)

        time.sleep(0.5)
        # best-effort version detection (used for STEAM-tool pin guards)
        try:
            board.version = firmata.board_version()
        except Exception:
            board.version = None
        return board

    def disconnect(self):
        """Disconnect from the board."""
        if self._transport:
            self._transport.close()
        self._connected = False
        if self._on_disconnected:
            self._on_disconnected()

    def on_disconnected(self, handler):
        """Register a callback invoked on disconnection."""
        self._on_disconnected = handler

    @property
    def is_connected(self):
        """Whether the board is connected."""
        return self._connected

    def _with_connect(self, fn):
        if not self._connected:
            raise DisconnectedError()
        return fn()

    def _cached_fetch(self, key, interval, fetcher):
        """Time-based cache used by fetch_* sensors (mirrors #cachedFetch).

        Re-fetches only when the cached value is older than ``interval`` ms.
        """
        if not self._connected:
            raise DisconnectedError()
        entry = self._cache.setdefault(key, {"value": None, "ts": 0.0})
        now = time.monotonic()
        if (now - entry["ts"]) * 1000 > interval:
            try:
                value = fetcher()
            except Exception:
                entry["value"] = None
                raise NotSupportedError()
            entry["value"] = value
            entry["ts"] = time.monotonic()
        return entry["value"]

    def _is_steam_tool(self):
        return bool(self.version) and self.version.get("type") == 2

    # ------------------------------------------------------------------
    # Version / UID
    # ------------------------------------------------------------------

    def fetch_version(self):
        """Fetch the board version as {"type", "major", "minor"} (or None)."""
        return self._with_connect(self._firmata.board_version)

    def fetch_uid(self):
        """Fetch the board UID as a hex string."""
        def _fetch():
            uid = self._firmata.board_uid()
            if uid is None:
                raise NotSupportedError()
            return "".join(f"{b:02x}" for b in uid)
        return self._with_connect(_fetch)

    # ------------------------------------------------------------------
    # I2C sensors
    # ------------------------------------------------------------------

    def _get_env_sensor(self):
        if self._env_sensor is None:
            sensor = BME280(self._bus)
            sensor.init()
            self._env_sensor = sensor
        return self._env_sensor

    def _get_brightness_sensor(self):
        if self._brightness_sensor is None:
            sensor = LTR303(self._bus)
            sensor.init()
            self._brightness_sensor = sensor
        return self._brightness_sensor

    def _get_accelerometer(self):
        if self._accelerometer is None:
            if KXTJ3.is_connected(self._bus):
                sensor = KXTJ3(self._bus)
            elif ADXL345.is_connected(self._bus):
                sensor = ADXL345(self._bus)
            else:
                raise NotSupportedError()
            sensor.init()
            self._accelerometer = sensor
        return self._accelerometer

    def _get_optical_sensor(self):
        if self._vl53l0x is None:
            address = 0x08
            v = self.version
            if v is None or v.get("type", 0) <= 1 or (
                v.get("type") == 2 and v.get("major") == 0 and v.get("minor") == 0
            ):
                address = 0x29
            sensor = VL53L0X(self._bus, address)
            found = sensor.init(True)
            if not found:
                raise NotSupportedError()
            sensor.set_range_profile("LONG_RANGE")
            sensor.start_continuous()
            self._vl53l0x = sensor
        return self._vl53l0x

    def fetch_brightness(self):
        """Light I2C brightness (lx)."""
        def _f():
            sensor = self._get_brightness_sensor()
            return sensor.get_brightness()
        value = self._cached_fetch("fetchBrightness", 100, _f)
        return min(64000, round(value * 10) / 10)

    def fetch_temperature(self):
        """Environment I2C temperature (degrees C)."""
        def _f():
            sensor = self._get_env_sensor()
            return sensor.read_temperature()
        value = self._cached_fetch("fetchTemperature", 100, _f)
        return round(value * 100) / 100

    def fetch_pressure(self):
        """Environment I2C pressure (hPa)."""
        def _f():
            sensor = self._get_env_sensor()
            return sensor.read_pressure()
        value = self._cached_fetch("fetchPressure", 100, _f)  # Pa
        return round(value * 100) / 10000  # -> hPa

    def fetch_humidity(self):
        """Environment I2C humidity (%)."""
        def _f():
            sensor = self._get_env_sensor()
            return sensor.read_humidity()
        value = self._cached_fetch("fetchHumidity", 100, _f)
        return round(value * 100) / 100

    def fetch_water_temperature_a(self):
        """Water temperature on Digital A (degrees C)."""
        def _f():
            if self.version and self.version.get("type") == 0:
                raise NotSupportedError()
            raw = self._firmata.get_water_temp(10)
            if raw is None:
                raise NotSupportedError()
            return raw / 10
        return self._cached_fetch("fetchWaterTemperatureA", 100, _f)

    def _acceleration(self):
        meter = self._get_accelerometer()
        return meter.get_acceleration()

    def fetch_acceleration_x(self):
        """Acceleration X (m/s^2)."""
        acc = self._cached_fetch("fetchAccelerationX", 100, self._acceleration)
        return round(acc["x"] * 100) / 100

    def fetch_acceleration_y(self):
        """Acceleration Y (m/s^2)."""
        acc = self._cached_fetch("fetchAccelerationY", 100, self._acceleration)
        return round(acc["y"] * 100) / 100

    def fetch_acceleration_z(self):
        """Acceleration Z (m/s^2)."""
        acc = self._cached_fetch("fetchAccelerationZ", 100, self._acceleration)
        return round(acc["z"] * 100) / 100

    def fetch_acceleration_magnitude(self):
        """Acceleration magnitude (m/s^2)."""
        acc = self._cached_fetch("fetchAccelerationMagnitude", 100, self._acceleration)
        return round(math.sqrt(acc["x"] ** 2 + acc["y"] ** 2 + acc["z"] ** 2) * 100) / 100

    def fetch_pitch(self):
        """Pitch (degrees)."""
        acc = self._cached_fetch("fetchPitch", 100, self._acceleration)
        angle = math.atan2(acc["x"], math.sqrt(acc["y"] ** 2 + acc["z"] ** 2)) * 180.0 / math.pi
        pitch = angle
        if acc["z"] < 0:
            pitch = (180 if angle > 0 else -180) - angle
        return round(pitch * 100) / 100

    def fetch_roll(self):
        """Roll (degrees)."""
        acc = self._cached_fetch("fetchRoll", 100, self._acceleration)
        return round((math.atan2(acc["y"], acc["z"]) * 180.0 / math.pi) * 100) / 100

    def fetch_optical_distance(self):
        """Laser I2C distance (cm), range ~10..200."""
        def _f():
            sensor = self._get_optical_sensor()
            distance = sensor.read_range_continuous_millimeters()
            distance = distance - 50            # STEAM Tool supplement
            return max(100, min(distance, 2000))  # clamp [mm]
        distance = self._cached_fetch("fetchOpticalDistance", 100, _f)
        distance = distance / 10                # mm -> cm
        return self._reduce_optical_noise(distance)

    def _reduce_optical_noise(self, value):
        if len(self._optical_samples) >= self._optical_samples_size:
            self._optical_samples.pop(0)
        self._optical_samples.append(value)
        s = sorted(self._optical_samples)
        return s[len(s) // 2]

    # ------------------------------------------------------------------
    # Analog input  (target = analog channel; value scaled to 0-100%)
    # ------------------------------------------------------------------

    def analog(self, target):
        if target not in _values(C.AnalogRead):
            raise InvalidConnectorError(target)
        def _read():
            raw = self._firmata.analog_read(target)
            return round((raw / 1023) * 1000) / 10
        return self._with_connect(_read)

    def analog_a1(self):
        return self.analog(C.AnalogRead.A1)

    def analog_a2(self):
        return self.analog(C.AnalogRead.A2)

    def analog_b1(self):
        return self.analog(C.AnalogRead.B1)

    def analog_b2(self):
        return self.analog(C.AnalogRead.B2)

    def analog_brightness(self):
        """Built-in analog light sensor (B2)."""
        return self.analog(C.AnalogRead.B2)

    # ------------------------------------------------------------------
    # Digital input  (target = board pin; returns bool)
    # ------------------------------------------------------------------

    def digital(self, target):
        if target not in _values(C.DigitalRead):
            raise InvalidConnectorError(target)
        return self._with_connect(lambda: self._firmata.digital_read(target) != 0)

    def digital_a1(self):
        return self.digital(C.DigitalRead.A1)

    def digital_a2(self):
        return self.digital(C.DigitalRead.A2)

    def digital_b1(self):
        return self.digital(C.DigitalRead.B1)

    def digital_b2(self):
        return self.digital(C.DigitalRead.B2)

    def motion_sensor(self):
        """Built-in motion (PIR) sensor."""
        return self.digital(C.DigitalRead.MOTION_SENSOR)

    # ------------------------------------------------------------------
    # Digital output
    # ------------------------------------------------------------------

    def run_digital_set(self, target, level):
        if target not in _values(C.DigitalWrite):
            raise InvalidConnectorError(target)
        if not isinstance(level, bool):
            raise InvalidValueError(level)

        def _do():
            if self._is_steam_tool() and target in (6, 9):
                raise NotSupportedError()
            self._firmata.set_pin_mode(target, 0x01)  # OUTPUT
            self._firmata.digital_write(target, 1 if level else 0)
            time.sleep(self._sending_interval)
        return self._with_connect(_do)

    def run_pin_bias_set(self, pin, bias):
        if pin not in _values(C.DigitalReadPin):
            raise InvalidConnectorError(pin)
        if bias not in (C.PinBias.NONE, C.PinBias.PULL_UP):
            raise InvalidValueError(bias)

        def _do():
            mode = PIN_MODE_PULLUP if bias else PIN_MODE_INPUT
            self._firmata.set_pin_mode(pin, mode)
            time.sleep(self._sending_interval)
        return self._with_connect(_do)

    # ------------------------------------------------------------------
    # Servo
    # ------------------------------------------------------------------

    def run_servo_turn(self, target, speed, angle):
        if target not in _values(C.ServoWrite):
            raise InvalidConnectorError(target)
        if not isinstance(speed, (int, float)) or isinstance(speed, bool):
            raise InvalidValueError(speed)
        if not isinstance(angle, (int, float)) or isinstance(angle, bool):
            raise InvalidValueError(angle)
        speed = max(0, min(100, speed))

        def _do():
            if self._is_steam_tool() and target in (6, 9):
                raise NotSupportedError()
            servo = self._servos.get(target)
            if servo is None:
                servo = _Servo(self, target)
                self._servos[target] = servo
            if servo.is_busy:
                raise BusyError("サーボ")
            servo.is_busy = True
            try:
                servo.turn_with_speed(angle, speed)
            finally:
                servo.is_busy = False
        return self._with_connect(_do)

    # ------------------------------------------------------------------
    # PWM
    # ------------------------------------------------------------------

    def run_pwm_set(self, target, level, min_interval=None):
        if target not in _values(C.PwmWrite):
            raise InvalidConnectorError(target)
        if not isinstance(level, (int, float)) or isinstance(level, bool):
            raise InvalidValueError(level)

        def _do():
            pin = target
            if self._is_steam_tool() and pin in (6, 9):
                raise NotSupportedError()
            percent = min(max(level, 0), 100)
            value = round(_PWM_RESOLUTION * (percent / 100))
            if min_interval and min_interval > 0:
                last = self._pwm_last_ts.get(pin)
                if last is not None:
                    elapsed = (time.monotonic() - last) * 1000
                    if elapsed < min_interval:
                        time.sleep((min_interval - elapsed) / 1000)
            self._pwm_last_ts[pin] = time.monotonic()
            self._firmata.set_pin_mode(pin, 0x03)  # PWM
            self._firmata.analog_write(pin, value)
            time.sleep(self._sending_interval)
        return self._with_connect(_do)

    # ------------------------------------------------------------------
    # Color LED (NeoPixel)
    # ------------------------------------------------------------------

    def _check_color_led_pin(self, target):
        if target not in _values(C.ColorLed):
            raise InvalidConnectorError(target)
        if self._is_steam_tool() and target in (6, 9):
            raise NotSupportedError()
        return target

    def _neopixel_color_value(self, rgb):
        r = _GAMMA[max(0, min(255, int(rgb[0])))]
        g = _GAMMA[max(0, min(255, int(rgb[1])))]
        b = _GAMMA[max(0, min(255, int(rgb[2])))]
        return (r << 16) + (g << 8) + b

    def _find_strip(self, pin):
        for strip in self._neopixel:
            if strip["pin"] == pin:
                return strip
        return None

    def _neopixel_config_strip(self, pin, length):
        self._neopixel = [s for s in self._neopixel if s["pin"] != pin]
        self._neopixel.append({"pin": pin, "length": length, "colors": [None] * length})
        msg = [_PIXEL_COMMAND, _PIXEL_CONFIG]
        for strip in self._neopixel:
            msg.append((_COLOR_ORDER_GRB << 5) | strip["pin"])
            msg.append(strip["length"] & 0x7F)
            msg.append((strip["length"] >> 7) & 0x7F)
        self._firmata.send_sysex(msg)
        time.sleep(self._sending_interval)

    def _neopixel_set_color(self, pin, rgb, index=0):
        strip = self._find_strip(pin)
        if strip is None:
            self._neopixel_config_strip(pin, _DEFAULT_NEOPIXEL_LENGTH)
            strip = self._find_strip(pin)
        address = 0
        for s in self._neopixel:
            if s["pin"] == pin:
                address += max(0, index % s["length"])
                break
            address += s["length"]
        if 0 <= index < strip["length"]:
            strip["colors"][index] = rgb
        color_value = self._neopixel_color_value(rgb)
        msg = [
            _PIXEL_COMMAND, _PIXEL_SET_PIXEL,
            address & 0x7F, (address >> 7) & 0x7F,
            color_value & 0x7F,
            (color_value >> 7) & 0x7F,
            (color_value >> 14) & 0x7F,
            (color_value >> 21) & 0x7F,
        ]
        self._firmata.send_sysex(msg)
        time.sleep(self._sending_interval)

    def _neopixel_fill_color(self, pin, color_fn):
        strip = self._find_strip(pin)
        length = strip["length"] if strip else _DEFAULT_NEOPIXEL_LENGTH
        old_colors = list(strip["colors"]) if strip else [None] * length
        if len(old_colors) < length:
            old_colors += [None] * (length - len(old_colors))
        for i in range(length):
            new_color = color_fn(old_colors[i], i, old_colors)
            if new_color is not None:
                self._neopixel_set_color(pin, new_color, i)

    def run_color_led_set_strip(self, target, length):
        pin = self._check_color_led_pin(target)
        self._neopixel_config_strip(pin, max(0, min(60, int(length))))

    def run_color_led_set_color(self, target, position, color):
        pin = self._check_color_led_pin(target)
        index = max(0, int(position) - 1)
        if isinstance(color, Rainbow):
            def fn(_old, i, colors):
                return color.to_rgb_array(i, len(colors)) if i == index else None
            self._neopixel_fill_color(pin, fn)
        else:
            self._neopixel_set_color(pin, _rgb_of(color), index)

    def run_color_led_fill_color(self, target, color):
        pin = self._check_color_led_pin(target)
        if isinstance(color, Rainbow):
            def fn(_old, i, colors):
                return color.to_rgb_array(i, len(colors))
        else:
            rgb = _rgb_of(color)
            def fn(_old, _i, _colors):
                return list(rgb)
        self._neopixel_fill_color(pin, fn)

    def run_color_led_shift_color(self, target, n, loop):
        pin = self._check_color_led_pin(target)
        n = int(n)

        def fn(cur_color, i, colors):
            length = len(colors)
            from_index = i - n
            from_index_loop = ((from_index % length) + length) % length
            new_color = colors[from_index_loop] if loop else (
                colors[from_index] if 0 <= from_index < length else None
            )
            if new_color is None and cur_color is None:
                return None
            if new_color is not None and cur_color is not None:
                if list(new_color) == list(cur_color):
                    return None
            return list(new_color) if new_color is not None else [0, 0, 0]
        self._neopixel_fill_color(pin, fn)

    def run_color_led_show(self):
        def _do():
            self._firmata.send_sysex([_PIXEL_COMMAND, _PIXEL_SHOW])
            time.sleep(self._sending_interval)
        return self._with_connect(_do)

    def run_color_led_clear(self, target):
        pin = self._check_color_led_pin(target)
        self._neopixel_fill_color(pin, lambda *_: [0, 0, 0])
        self.run_color_led_show()

    # ------------------------------------------------------------------
    # IR remote
    # ------------------------------------------------------------------

    def run_ir_remote_send(self, target, command):
        if target not in _values(C.IrRemoteWrite):
            raise InvalidConnectorError(target)
        if not isinstance(command, int) or isinstance(command, bool) or not (0 <= command < 10):
            raise InvalidValueError(command)

        def _do():
            if target == C.IrRemoteWrite.ON_BOARD:
                self._firmata.enable_device(command)
                time.sleep(self._sending_interval)
            else:
                min_interval = 100
                self.run_pwm_set(target, 1, min_interval)
                self.run_pwm_set(target, 10 * command, min_interval)
        return self._with_connect(_do)

    # ------------------------------------------------------------------
    # Raw I2C
    # ------------------------------------------------------------------

    def run_i2c_write(self, address, register, data):
        if not isinstance(address, int):
            raise InvalidValueError(address)
        if not isinstance(register, int):
            raise InvalidValueError(register)
        if isinstance(data, int):
            data = [data]

        def _do():
            self._bus.write(address, register, list(data))
        return self._with_connect(_do)

    def fetch_i2c_read(self, address, register, length):
        if not isinstance(address, int):
            raise InvalidValueError(address)
        if not isinstance(register, int):
            raise InvalidValueError(register)
        if not isinstance(length, int) or length < 1:
            raise InvalidValueError(length)
        return self._with_connect(
            lambda: self._bus.read(address, register, length)
        )

    # ------------------------------------------------------------------
    # Share server
    # ------------------------------------------------------------------

    def run_share_connect(self, group_id):
        if not isinstance(group_id, str) or group_id.strip() == "":
            raise InvalidValueError(group_id)
        return self._with_connect(lambda: self._share.connect(group_id))

    def run_share_send(self, label, data):
        if not isinstance(label, str) or label.strip() == "":
            raise InvalidValueError(label)

        def _do():
            if not self._share.is_connected():
                raise DisconnectedError()
            if self._share._sending:
                raise BusyError("通信")
            return self._share.send(label, data)
        return self._with_connect(_do)

    def shared_data(self, label):
        return self._with_connect(lambda: self._share.shared_data(label))

    @property
    def is_share_server_connected(self):
        return self._share.is_connected()


def _values(enum_cls):
    return [
        v for k, v in vars(enum_cls).items()
        if not k.startswith("_")
    ]


def _rgb_of(color):
    """Accept a Color, an (r, g, b) tuple/list, and return an rgb list."""
    if isinstance(color, Color):
        return color.to_rgb_array()
    if isinstance(color, (tuple, list)) and len(color) == 3:
        return [int(color[0]), int(color[1]), int(color[2])]
    raise InvalidValueError(color)


# ----------------------------------------------------------------------
# Expose the connector enums, value classes and errors as attributes of
# AkaDako, mirroring akadako.js (AkaDako.ServoWrite, AkaDako.Color, ...).
# This makes ``from akadako import AkaDako`` the only import you need:
#     board.run_servo_turn(AkaDako.ServoWrite.A1, 50, 90)
#     board.run_color_led_fill_color(AkaDako.ColorLed.A1, AkaDako.Color.RED)
# The names remain importable from ``akadako`` directly as well.
# ----------------------------------------------------------------------
AkaDako.DigitalRead = C.DigitalRead
AkaDako.DigitalReadPin = C.DigitalReadPin
AkaDako.DigitalWrite = C.DigitalWrite
AkaDako.AnalogRead = C.AnalogRead
AkaDako.ServoWrite = C.ServoWrite
AkaDako.PwmWrite = C.PwmWrite
AkaDako.ColorLed = C.ColorLed
AkaDako.IrRemoteWrite = C.IrRemoteWrite
AkaDako.PinBias = C.PinBias
AkaDako.Color = Color
AkaDako.Rainbow = Rainbow

AkaDako.AkaDakoError = AkaDakoError
AkaDako.NotSupportedError = NotSupportedError
AkaDako.DisconnectedError = DisconnectedError
AkaDako.InvalidValueError = InvalidValueError
AkaDako.InvalidConnectorError = InvalidConnectorError
AkaDako.BusyError = BusyError
