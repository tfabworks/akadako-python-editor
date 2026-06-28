"""
Constants and value classes for the akadako library.

These mirror the JavaScript library (akadako.js):
- Connector enums (DigitalRead, DigitalWrite, ServoWrite, PwmWrite, AnalogRead,
  ColorLed, IrRemoteWrite, DigitalReadPin, PinBias)
- Color / Rainbow value classes

IMPORTANT: the numeric values are the *board pin numbers* (or analog channels)
expected by the AkaDako firmware, taken from akadako.js. Do not change them.
"""

import math


# ----------------------------------------------------------------------
# Connector enums  (values = board pin numbers, exactly as in akadako.js)
# ----------------------------------------------------------------------

class DigitalReadPin:
    """Digital input pins."""
    A1 = 10
    A2 = 11
    B1 = 6
    B2 = 9


class DigitalRead:
    """Digital input targets (connectors and built-in devices)."""
    A1 = 10
    A2 = 11
    B1 = 6
    B2 = 9
    BUTTON_A = 6        # Aボタン(内蔵)
    BUTTON_B = 9        # Bボタン(内蔵)
    MOTION_SENSOR = 9   # 人感(内蔵)


class DigitalWrite:
    """Digital output targets."""
    A1 = 10
    A2 = 11
    B1 = 6
    B2 = 9
    RELAY_ON_BOARD = 4  # 制御スイッチ(内蔵)


class ServoWrite:
    """Servo output targets."""
    A1 = 10
    A2 = 11
    B1 = 6
    B2 = 9


class PwmWrite:
    """PWM output targets."""
    A1 = 10
    A2 = 11
    B1 = 6
    B2 = 9
    VIBRATION_MOTOR_ON_BOARD = 3  # 振動モーター(内蔵)


class AnalogRead:
    """Analog input targets (values are analog *channels*, not board pins)."""
    A1 = 0
    A2 = 1
    B1 = 2
    B2 = 3


class ColorLed:
    """Color LED (NeoPixel) connectors."""
    A1 = 10
    A2 = 11
    B1 = 6
    B2 = 9
    ON_BOARD = 7  # 内蔵


class IrRemoteWrite:
    """IR remote output targets."""
    A1 = 10
    ON_BOARD = 999  # 内蔵


class PinBias:
    """Input pin bias (pull-up) configuration. Matches akadako.js (boolean)."""
    NONE = False     # なし
    PULL_UP = True   # プルアップ


# ----------------------------------------------------------------------
# Color
# ----------------------------------------------------------------------

class Color:
    """An RGB color, mirroring AkaDako.Color.

    Use the named class attributes (Color.RED etc.) or construct from RGB:
        Color(255, 0, 0)
    """

    def __init__(self, r, g, b):
        self.r = round(max(0, min(255, r)))
        self.g = round(max(0, min(255, g)))
        self.b = round(max(0, min(255, b)))

    def brightness(self, v):
        """Return a new color scaled by ``v`` percent (100 = unchanged)."""
        return self.map(lambda c: (c * v) / 100)

    def map(self, f):
        return Color(f(self.r), f(self.g), f(self.b))

    def to_rgb_array(self):
        return [self.r, self.g, self.b]

    def __repr__(self):
        return f"Color({self.r}, {self.g}, {self.b})"

    def __eq__(self, other):
        return (
            isinstance(other, Color)
            and self.r == other.r
            and self.g == other.g
            and self.b == other.b
        )


# Named colors (identical RGB values to akadako.js AkaDako.Color).
Color.RED = Color(0xFF, 0, 0)
Color.ORANGE = Color(0xFF, 0xA5, 0)
Color.YELLOW = Color(0xFF, 0xFF, 0)
Color.GREEN = Color(0, 0xFF, 0)
Color.BLUE = Color(0, 0, 0xFF)
Color.INDIGO = Color(0x4B, 0, 0x82)
Color.VIOLET = Color(0xEE, 0x82, 0xEE)
Color.PURPLE = Color(0x80, 0, 0x80)
Color.WHITE = Color(0xFF, 0xFF, 0xFF)
Color.BLACK = Color(0, 0, 0)


def _oklch_to_rgb(lightness, chromacity, hue_deg):
    """Convert an OKLCH color to an sRGB (r, g, b) tuple of 0-255 ints.

    Mirrors chroma.oklch(lightness, chromacity, hue).rgb() used by
    AkaDako.Rainbow in akadako.js. ``lightness`` is 0-1, ``hue_deg`` in degrees.
    Out-of-gamut values are clipped to [0, 255].
    """
    h = math.radians(hue_deg)
    a = chromacity * math.cos(h)
    b = chromacity * math.sin(h)

    # OKLab -> approximate linear sRGB
    l_ = lightness + 0.3963377774 * a + 0.2158037573 * b
    m_ = lightness - 0.1055613458 * a - 0.0638541728 * b
    s_ = lightness - 0.0894841775 * a - 1.2914855480 * b

    l = l_ ** 3
    m = m_ ** 3
    s = s_ ** 3

    lr = 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    lg = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    lb = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s

    def gamma(c):
        c = max(0.0, min(1.0, c))
        if c <= 0.0031308:
            c = 12.92 * c
        else:
            c = 1.055 * (c ** (1 / 2.4)) - 0.055
        return round(max(0.0, min(1.0, c)) * 255)

    return (gamma(lr), gamma(lg), gamma(lb))


class Rainbow:
    """A rainbow fill for color LEDs, mirroring AkaDako.Rainbow.

    The color of each LED depends on its index within the strip.
    """

    def __init__(self, brightness=100):
        self.brightness = brightness

    def to_rgb_array(self, idx, length):
        lightness = max(0, min(100, self.brightness)) / 100
        chromacity = 0.15
        hue = (idx * 360) / length if length else 0
        return list(_oklch_to_rgb(lightness, chromacity, hue))

    def __repr__(self):
        return f"Rainbow({self.brightness})"
