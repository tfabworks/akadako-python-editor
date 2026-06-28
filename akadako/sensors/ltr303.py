"""
LTR303 ambient light sensor.

Ported from xcx-g2s ltr303.js. get_brightness() returns illuminance in lux
(rounded to 1 decimal).
"""

LTR303_ADDRESS = 0x29

_ID_REG = 0x86
_ID = 0xA0           # high nibble of the part-id register
_ALS_CONTR = 0x80    # write 1 -> active mode
_CH1_DATA = 0x88
_CH0_DATA = 0x8A


class LTR303:
    def __init__(self, bus, address=LTR303_ADDRESS):
        self.bus = bus
        self.address = address

    def read_id(self):
        d = self.bus.read(self.address, _ID_REG, 1)
        return (d[0] & 0xF0) if d else 0

    @staticmethod
    def is_connected(bus, address=LTR303_ADDRESS):
        d = bus.read(address, _ID_REG, 1)
        return bool(d) and (d[0] & 0xF0) == _ID

    def init(self):
        if (self.read_id()) != _ID:
            raise RuntimeError(f"0x{self.address:x} is not LTR303")
        return self

    def get_brightness(self):
        self.bus.write(self.address, _ALS_CONTR, 1)
        ch1 = self.bus.read16_le(self.address, _CH1_DATA)
        ch0 = self.bus.read16_le(self.address, _CH0_DATA)
        total = ch0 + ch1
        if total == 0:
            return 0.0
        ratio = ch1 / total
        if ratio < 0.45:
            lux = 1.7743 * ch0 + 1.1059 * ch1
        elif ratio < 0.64:
            lux = 4.2785 * ch0 - 1.9548 * ch1
        elif ratio < 0.85:
            lux = 0.5926 * ch0 + 0.1185 * ch1
        else:
            lux = 0.0
        return round(lux * 10) / 10
