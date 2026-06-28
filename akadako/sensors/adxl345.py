"""
ADXL345 accelerometer.

Ported from xcx-g2s adxl345.js. get_acceleration() returns a dict
{"x", "y", "z"} in m/s^2 (full-resolution, +/-16g range).
"""

ADXL345_ADDRESS = 0x53

_ADXL345_ID = 0xE5
_REG_DEVID = 0x00
_REG_DATA_FORMAT = 0x31
_REG_POWER_CTL = 0x2D
_REG_DATA_X0 = 0x32

_FULL_RES_16G = 0x0B   # FULL_RES | +/-16g
_MEASURE = 0x08        # measure mode

# 4 mg/LSB (full resolution) * 9.80665 m/s^2 per g
_SCALE = 0.0392266


def _s16le(lo, hi):
    v = lo | (hi << 8)
    return v - 0x10000 if v & 0x8000 else v


class ADXL345:
    def __init__(self, bus, address=ADXL345_ADDRESS):
        self.bus = bus
        self.address = address

    def read_id(self):
        d = self.bus.read(self.address, _REG_DEVID, 1)
        return d[0] if d else 0

    @staticmethod
    def is_connected(bus, address=ADXL345_ADDRESS):
        # adxl345.js returns true on any successful read of register 0x00
        d = bus.read(address, _REG_DEVID, 1)
        return bool(d)

    def init(self):
        if (self.read_id()) != _ADXL345_ID:
            raise RuntimeError(f"0x{self.address:x} is not ADXL345")
        self.bus.write(self.address, _REG_DATA_FORMAT, _FULL_RES_16G)
        self.bus.write(self.address, _REG_POWER_CTL, _MEASURE)
        return self

    def get_acceleration(self):
        d = self.bus.read(self.address, _REG_DATA_X0, 6)
        if len(d) < 6:
            return {"x": 0.0, "y": 0.0, "z": 0.0}
        return {
            "x": _s16le(d[0], d[1]) * _SCALE,
            "y": _s16le(d[2], d[3]) * _SCALE,
            "z": _s16le(d[4], d[5]) * _SCALE,
        }
