"""
BME280 environment sensor (temperature / pressure / humidity).

Ported from xcx-g2s bme280.js. The Bosch floating-point compensation formulas
are used (the datasheet reference algorithm), which yields correct physical
values. (akadako.js's bme280.js uses a fixed-point variant that contains an
upstream read-24 bug duplicating the MSB; we deliberately implement the correct
algorithm here.)

Returned units:
- read_temperature() -> degrees Celsius
- read_pressure()    -> Pascals (Pa)
- read_humidity()    -> percent relative humidity (%RH)
"""

import time

BME280_ADDRESS = 0x76

_REG_CHIP_ID = 0xD0
_REG_CONTROL_HUMID = 0xF2
_REG_CONTROL = 0xF4
_REG_CONFIG = 0xF5
_REG_PRESSURE = 0xF7   # 0xF7..0xF9
_REG_TEMP = 0xFA       # 0xFA..0xFC
_REG_HUMID = 0xFD      # 0xFD..0xFE

_CHIP_ID_BME280 = 0x60
_CHIP_ID_BMP280 = 0x58


def _u16(d):
    return d[0] | (d[1] << 8)


def _s16(d):
    v = d[0] | (d[1] << 8)
    return v - 0x10000 if v & 0x8000 else v


def _s8(v):
    return v - 0x100 if v & 0x80 else v


class BME280:
    def __init__(self, bus, address=BME280_ADDRESS):
        self.bus = bus
        self.address = address
        self.t_fine = 0.0
        self._cal = {}

    @staticmethod
    def is_connected(bus, address=BME280_ADDRESS):
        chip = bus.read8(address, _REG_CHIP_ID)
        return chip in (_CHIP_ID_BME280, _CHIP_ID_BMP280)

    def init(self):
        # The chip-id read occasionally returns 0 on a transient I2C miss
        # (the reply is dropped under load). Retry a few times before failing
        # so one glitch doesn't kill the whole program.
        chip = 0
        for _ in range(5):
            chip = self.bus.read8(self.address, _REG_CHIP_ID)
            if chip in (_CHIP_ID_BME280, _CHIP_ID_BMP280):
                break
            time.sleep(0.03)
        if chip not in (_CHIP_ID_BME280, _CHIP_ID_BMP280):
            raise RuntimeError(
                f"0x{self.address:x} is not BME280 (chip id 0x{chip:02x})"
            )
        self._read_calibration()
        # humidity oversampling x1; must be written before ctrl_meas
        self.bus.write(self.address, _REG_CONTROL_HUMID, 0x01)
        # temp x2, pressure x8, normal mode
        self.bus.write(self.address, _REG_CONTROL, 0x4F)
        # standby 125ms
        self.bus.write(self.address, _REG_CONFIG, 0x40)
        return self

    def _read_calibration(self):
        c = self._cal
        c["T1"] = _u16(self.bus.read(self.address, 0x88, 2))
        c["T2"] = _s16(self.bus.read(self.address, 0x8A, 2))
        c["T3"] = _s16(self.bus.read(self.address, 0x8C, 2))
        c["P1"] = _u16(self.bus.read(self.address, 0x8E, 2))
        c["P2"] = _s16(self.bus.read(self.address, 0x90, 2))
        c["P3"] = _s16(self.bus.read(self.address, 0x92, 2))
        c["P4"] = _s16(self.bus.read(self.address, 0x94, 2))
        c["P5"] = _s16(self.bus.read(self.address, 0x96, 2))
        c["P6"] = _s16(self.bus.read(self.address, 0x98, 2))
        c["P7"] = _s16(self.bus.read(self.address, 0x9A, 2))
        c["P8"] = _s16(self.bus.read(self.address, 0x9C, 2))
        c["P9"] = _s16(self.bus.read(self.address, 0x9E, 2))
        c["H1"] = self.bus.read8(self.address, 0xA1)
        c["H2"] = _s16(self.bus.read(self.address, 0xE1, 2))
        c["H3"] = self.bus.read8(self.address, 0xE3)
        e4 = self.bus.read8(self.address, 0xE4)
        e5 = self.bus.read8(self.address, 0xE5)
        e6 = self.bus.read8(self.address, 0xE6)
        c["H4"] = (e4 << 4) | (e5 & 0x0F)
        c["H5"] = (e6 << 4) | (e5 >> 4)
        c["H6"] = _s8(self.bus.read8(self.address, 0xE7))

    def read_temperature(self):
        d = self.bus.read(self.address, _REG_TEMP, 3)
        if len(d) < 3:
            return 0.0
        adc_t = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
        c = self._cal
        var1 = (adc_t / 16384.0 - c["T1"] / 1024.0) * c["T2"]
        var2 = ((adc_t / 131072.0 - c["T1"] / 8192.0) ** 2) * c["T3"]
        self.t_fine = var1 + var2
        return self.t_fine / 5120.0

    def read_pressure(self):
        self.read_temperature()
        d = self.bus.read(self.address, _REG_PRESSURE, 3)
        if len(d) < 3:
            return 0.0
        adc_p = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
        c = self._cal
        var1 = self.t_fine / 2.0 - 64000.0
        var2 = var1 * var1 * c["P6"] / 32768.0
        var2 = var2 + var1 * c["P5"] * 2.0
        var2 = var2 / 4.0 + c["P4"] * 65536.0
        var1 = (c["P3"] * var1 * var1 / 524288.0 + c["P2"] * var1) / 524288.0
        var1 = (1.0 + var1 / 32768.0) * c["P1"]
        if var1 == 0:
            return 0.0
        p = 1048576.0 - adc_p
        p = (p - var2 / 4096.0) * 6250.0 / var1
        var1 = c["P9"] * p * p / 2147483648.0
        var2 = p * c["P8"] / 32768.0
        p = p + (var1 + var2 + c["P7"]) / 16.0
        return p  # Pa

    def read_humidity(self):
        self.read_temperature()
        d = self.bus.read(self.address, _REG_HUMID, 2)
        if len(d) < 2:
            return 0.0
        adc_h = (d[0] << 8) | d[1]
        c = self._cal
        var_h = self.t_fine - 76800.0
        var_h = (adc_h - (c["H4"] * 64.0 + c["H5"] / 16384.0 * var_h)) * (
            c["H2"] / 65536.0 * (
                1.0 + c["H6"] / 67108864.0 * var_h * (1.0 + c["H3"] / 67108864.0 * var_h)
            )
        )
        var_h = var_h * (1.0 - c["H1"] * var_h / 524288.0)
        return max(0.0, min(100.0, var_h))
