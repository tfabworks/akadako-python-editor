"""
KXTJ3 accelerometer.

Ported from xcx-g2s kxtj3.js. get_acceleration() returns a dict
{"x", "y", "z"} in m/s^2. Default range 8g, low-power mode, 50 Hz ODR.
"""

KXTJ3_ADDRESS_0E = 0x0E
KXTJ3_ADDRESS_0F = 0x0F

_WHO_AM_I = 0x0F
_WAI_VAL = 0x35
_CNTL1 = 0x1B
_DATA_CNTL = 0x21
_XOUT_L = 0x06
_READ_DATA_SIZE = 6

_DATA_CNTL_OSA_50HZ = 0x02

_CNTL1_PC1 = 0x80          # operating mode bit
_CNTL1_RES_LOWPOWER = 0x00
_CNTL1_GSEL_2G = 0 << 2    # 0x00
_CNTL1_GSEL_4G = 2 << 2    # 0x08
_CNTL1_GSEL_8G = 4 << 2    # 0x10
_CNTL1_GSEL_16G = 1 << 2   # 0x04
_CNTL1_GSELMASK = 0x18
_CNTL1_EN16GMASK = 0x04
_DIVIDE_SHIFT = 15         # 1 << 15 == 32768


def _s16le(lo, hi):
    v = lo | (hi << 8)
    return v - 0x10000 if v & 0x8000 else v


class KXTJ3:
    def __init__(self, bus, address=KXTJ3_ADDRESS_0E):
        self.bus = bus
        self.address = address
        self.reg_ctrl1 = 0
        self.g_divide = (1 << _DIVIDE_SHIFT) / 8  # default 8g

    @staticmethod
    def is_connected(bus, address=KXTJ3_ADDRESS_0E):
        d = bus.read(address, _WHO_AM_I, 1)
        return bool(d) and d[0] == _WAI_VAL

    def init(self, range_g=8):
        ctrl1 = self._init_device(range_g)
        self._setup_parameters(ctrl1)
        self._start()
        return self

    def _init_device(self, range_g):
        d = self.bus.read(self.address, _WHO_AM_I, 1)
        if not d or d[0] != _WAI_VAL:
            raise RuntimeError(f"0x{self.address:x} is not KXTJ3")
        if range_g == 2:
            gsel = _CNTL1_GSEL_2G
        elif range_g == 4:
            gsel = _CNTL1_GSEL_4G
        elif range_g == 16:
            gsel = _CNTL1_GSEL_16G
        else:
            gsel = _CNTL1_GSEL_8G
        ctrl1_value = _CNTL1_RES_LOWPOWER | gsel
        self.bus.write(self.address, _CNTL1, ctrl1_value)
        self.bus.write(self.address, _DATA_CNTL, _DATA_CNTL_OSA_50HZ)
        d = self.bus.read(self.address, _CNTL1, 1)
        return d[0] if d else ctrl1_value

    def _setup_parameters(self, ctrl1):
        self.reg_ctrl1 = ctrl1
        g_selection = ctrl1 & _CNTL1_GSELMASK
        en16g = ctrl1 & _CNTL1_EN16GMASK
        if en16g == _CNTL1_GSEL_16G:
            g_sense = 16
        elif g_selection == _CNTL1_GSEL_2G:
            g_sense = 2
        elif g_selection == _CNTL1_GSEL_4G:
            g_sense = 4
        else:
            g_sense = 8
        self.g_divide = (1 << _DIVIDE_SHIFT) / g_sense

    def _start(self):
        self.bus.write(self.address, _CNTL1, self.reg_ctrl1 | _CNTL1_PC1)

    def stop(self):
        self.bus.write(self.address, _CNTL1, self.reg_ctrl1 & ~_CNTL1_PC1)

    def get_acceleration(self):
        d = self.bus.read(self.address, _XOUT_L, _READ_DATA_SIZE)
        if len(d) < 6:
            return {"x": 0.0, "y": 0.0, "z": 0.0}
        return {
            "x": 9.8 * _s16le(d[0], d[1]) / self.g_divide,
            "y": 9.8 * _s16le(d[2], d[3]) / self.g_divide,
            "z": 9.8 * _s16le(d[4], d[5]) / self.g_divide,
        }
