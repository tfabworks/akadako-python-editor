"""VL53L0X time-of-flight distance sensor driver.

This is a faithful Python port of ``vl53l0x.js`` from the xcx-g2s project
(``external/xcx-g2s/src/vm/extensions/block/vl53l0x.js``), itself derived from
the Pololu Arduino VL53L0X library (https://github.com/pololu/vl53l0x-arduino),
which in turn is based on ST's VL53L0X API.

It is intended for use with the AkaDako board. The full ST default tuning
register table, SPAD info routine, measurement-timing-budget / VCSEL
macro-period math, and reference calibration are all ported verbatim so that
the sensor produces valid readings (a hand-trimmed minimal init does NOT work
for the VL53L0X).

I2C address note:
    The VL53L0X default 7-bit address is ``0x29``. AkaDako boards may expose
    the sensor at I2C address ``0x08`` instead; in that case pass
    ``address=0x08`` to the constructor.

I2C bus interface:
    The ``bus`` object passed to the constructor must provide exactly these two
    methods, which are the only I2C primitives used here:

        ``bus.read(addr, reg, length) -> list[int]``
            Read ``length`` bytes starting at register ``reg``; returns a list
            of byte values.
        ``bus.write(addr, reg, data)``
            Write to register ``reg``. ``data`` is either an int (a single
            byte) or a ``list[int]`` of bytes.

All multi-byte register values are BIG-ENDIAN, matching the JavaScript source
(``(d[0] << 8) | d[1]`` etc.).
"""

import time

# region register addresses from API vl53l0x_device.h (ordered as listed there)
# enum regAddr
SYSRANGE_START = 0x00

SYSTEM_THRESH_HIGH = 0x0C
SYSTEM_THRESH_LOW = 0x0E

SYSTEM_SEQUENCE_CONFIG = 0x01
SYSTEM_RANGE_CONFIG = 0x09
SYSTEM_INTERMEASUREMENT_PERIOD = 0x04

SYSTEM_INTERRUPT_CONFIG_GPIO = 0x0A

GPIO_HV_MUX_ACTIVE_HIGH = 0x84

SYSTEM_INTERRUPT_CLEAR = 0x0B

RESULT_INTERRUPT_STATUS = 0x13
RESULT_RANGE_STATUS = 0x14

RESULT_CORE_AMBIENT_WINDOW_EVENTS_RTN = 0xBC
RESULT_CORE_RANGING_TOTAL_EVENTS_RTN = 0xC0
RESULT_CORE_AMBIENT_WINDOW_EVENTS_REF = 0xD0
RESULT_CORE_RANGING_TOTAL_EVENTS_REF = 0xD4
RESULT_PEAK_SIGNAL_RATE_REF = 0xB6

ALGO_PART_TO_PART_RANGE_OFFSET_MM = 0x28

I2C_SLAVE_DEVICE_ADDRESS = 0x8A

MSRC_CONFIG_CONTROL = 0x60

PRE_RANGE_CONFIG_MIN_SNR = 0x27
PRE_RANGE_CONFIG_VALID_PHASE_LOW = 0x56
PRE_RANGE_CONFIG_VALID_PHASE_HIGH = 0x57
PRE_RANGE_MIN_COUNT_RATE_RTN_LIMIT = 0x64

FINAL_RANGE_CONFIG_MIN_SNR = 0x67
FINAL_RANGE_CONFIG_VALID_PHASE_LOW = 0x47
FINAL_RANGE_CONFIG_VALID_PHASE_HIGH = 0x48
FINAL_RANGE_CONFIG_MIN_COUNT_RATE_RTN_LIMIT = 0x44

PRE_RANGE_CONFIG_SIGMA_THRESH_HI = 0x61
PRE_RANGE_CONFIG_SIGMA_THRESH_LO = 0x62

PRE_RANGE_CONFIG_VCSEL_PERIOD = 0x50
PRE_RANGE_CONFIG_TIMEOUT_MACROP_HI = 0x51
PRE_RANGE_CONFIG_TIMEOUT_MACROP_LO = 0x52

SYSTEM_HISTOGRAM_BIN = 0x81
HISTOGRAM_CONFIG_INITIAL_PHASE_SELECT = 0x33
HISTOGRAM_CONFIG_READOUT_CTRL = 0x55

FINAL_RANGE_CONFIG_VCSEL_PERIOD = 0x70
FINAL_RANGE_CONFIG_TIMEOUT_MACROP_HI = 0x71
FINAL_RANGE_CONFIG_TIMEOUT_MACROP_LO = 0x72
CROSSTALK_COMPENSATION_PEAK_RATE_MCPS = 0x20

MSRC_CONFIG_TIMEOUT_MACROP = 0x46

SOFT_RESET_GO2_SOFT_RESET_N = 0xBF
IDENTIFICATION_MODEL_ID = 0xC0
IDENTIFICATION_REVISION_ID = 0xC2

OSC_CALIBRATE_VAL = 0xF8

GLOBAL_CONFIG_VCSEL_WIDTH = 0x32
GLOBAL_CONFIG_SPAD_ENABLES_REF_0 = 0xB0
GLOBAL_CONFIG_SPAD_ENABLES_REF_1 = 0xB1
GLOBAL_CONFIG_SPAD_ENABLES_REF_2 = 0xB2
GLOBAL_CONFIG_SPAD_ENABLES_REF_3 = 0xB3
GLOBAL_CONFIG_SPAD_ENABLES_REF_4 = 0xB4
GLOBAL_CONFIG_SPAD_ENABLES_REF_5 = 0xB5

GLOBAL_CONFIG_REF_EN_START_SELECT = 0xB6
DYNAMIC_SPAD_NUM_REQUESTED_REF_SPAD = 0x4E
DYNAMIC_SPAD_REF_EN_START_OFFSET = 0x4F
POWER_MANAGEMENT_GO1_POWER_FORCE = 0x80

VHV_CONFIG_PAD_SCL_SDA__EXTSUP_HV = 0x89

ALGO_PHASECAL_LIM = 0x30
ALGO_PHASECAL_CONFIG_TIMEOUT = 0x30
# endregion

# enum vcselPeriodType
VcselPeriodPreRange = 0
VcselPeriodFinalRange = 1


def decode_vcsel_period(reg_val):
    """Decode VCSEL pulse period in PCLKs from register value.

    Based on VL53L0X_decode_vcsel_period().
    """
    return ((reg_val) + 1) << 1


def encode_vcsel_period(period_pclks):
    """Encode VCSEL pulse period register value from period in PCLKs.

    Based on VL53L0X_encode_vcsel_period().
    """
    return ((period_pclks) >> 1) - 1


def calc_macro_period(vcsel_period_pclks):
    """Calculate macro period in *nanoseconds* from VCSEL period in PCLKs.

    Based on VL53L0X_calc_macro_period_ps().
    PLL_period_ps = 1655; macro_period_vclks = 2304.
    """
    return ((2304 * (vcsel_period_pclks) * 1655) + 500) / 1000


class VL53L0X:
    """A VL53L0X distance sensor."""

    def __init__(self, bus, address=0x29):
        """Create a VL53L0X instance.

        :param bus: I2C adapter exposing ``read``/``write`` methods.
        :param address: 7-bit I2C address (default 0x29; AkaDako may use 0x08).
        """
        # I2C bus adapter
        self.bus = bus

        # I2C address for this module
        self.address = address

        # read by init and used when starting measurement;
        # is StopVariable field of VL53L0X_DevData_t structure in API
        self.stop_variable = 0

        # Timeout for IO in milliseconds.
        self.io_timeout = 500

        # Did a timeout occur in a sequence.
        self.did_timeout = False

        self.measurement_timing_budget_us = 0

        # Starting time (seconds, monotonic) to count timeout for IO.
        self.timeout_start_ms = 0.0

    # region I2C register helpers (built on bus.read / bus.write)

    def write_reg(self, register, value):
        """Write an 8-bit value at the register."""
        self.bus.write(self.address, register, value & 0xFF)

    def write_reg16(self, register, value):
        """Write a 16-bit value at the register (big-endian)."""
        data = [
            (value >> 8) & 0xFF,
            value & 0xFF,
        ]
        self.bus.write(self.address, register, data)

    def write_reg32(self, register, value):
        """Write a 32-bit value at the register (big-endian)."""
        data = [
            (value >> 24) & 0xFF,
            (value >> 16) & 0xFF,
            (value >> 8) & 0xFF,
            value & 0xFF,
        ]
        self.bus.write(self.address, register, data)

    def read_reg(self, register):
        """Read an 8-bit value from the register."""
        data = self.bus.read(self.address, register, 1)
        return data[0]

    def read_reg16(self, register):
        """Read a 16-bit value from the register (big-endian)."""
        data = self.bus.read(self.address, register, 2)
        return (data[0] << 8) | data[1]

    def read_reg32(self, register):
        """Read a 32-bit value from the register (big-endian)."""
        data = self.bus.read(self.address, register, 4)
        return (
            (data[0] << 24)
            | (data[1] << 16)
            | (data[2] << 8)
            | data[3]
        )

    def write_multi(self, register, data):
        """Write these bytes starting at the register."""
        self.bus.write(self.address, register, list(data))

    def read_multi(self, register, bytes_to_read):
        """Read ``bytes_to_read`` bytes from the register."""
        return self.bus.read(self.address, register, bytes_to_read)

    # endregion

    def set_address(self, new_addr):
        """Change I2C address for this module.

        Based on the JS setAddress().
        """
        self.write_reg(I2C_SLAVE_DEVICE_ADDRESS, new_addr)
        self.address = new_addr

    def init(self, io_2v8=True):
        """Initialize the sensor.

        Sequence based on VL53L0X_DataInit(), VL53L0X_StaticInit(), and
        VL53L0X_PerformRefCalibration().

        :param io_2v8: set 2V8 mode if True.
        :returns: True if initialization succeeded, otherwise False.
        """
        # check model ID register (value specified in datasheet)
        sensor_id = self.read_reg(IDENTIFICATION_MODEL_ID)
        if sensor_id != 0xEE:
            return False

        # VL53L0X_DataInit() begin

        # sensor uses 1V8 mode for I/O by default; switch to 2V8 if necessary
        if io_2v8:
            self.write_reg(
                VHV_CONFIG_PAD_SCL_SDA__EXTSUP_HV,
                self.read_reg(VHV_CONFIG_PAD_SCL_SDA__EXTSUP_HV) | 0x01,
            )  # set bit 0

        # "Set I2C standard mode"
        self.write_reg(0x88, 0x00)

        self.write_reg(0x80, 0x01)
        self.write_reg(0xFF, 0x01)
        self.write_reg(0x00, 0x00)
        self.stop_variable = self.read_reg(0x91)
        self.write_reg(0x00, 0x01)
        self.write_reg(0xFF, 0x00)
        self.write_reg(0x80, 0x00)

        # disable SIGNAL_RATE_MSRC (bit 1) and SIGNAL_RATE_PRE_RANGE (bit 4)
        # limit checks
        self.write_reg(
            MSRC_CONFIG_CONTROL,
            self.read_reg(MSRC_CONFIG_CONTROL) | 0x12,
        )

        # set final range signal rate limit to 0.25 MCPS
        self.set_signal_rate_limit(0.25)

        self.write_reg(SYSTEM_SEQUENCE_CONFIG, 0xFF)

        # VL53L0X_DataInit() end

        # VL53L0X_StaticInit() begin

        info = {"count": 0, "isAperture": False}
        if not self.get_spad_info(info):
            return False

        # The SPAD map (RefGoodSpadMap) is read by VL53L0X_get_info_from_device()
        # in the API, but the same data seems to be more easily readable from
        # GLOBAL_CONFIG_SPAD_ENABLES_REF_0 through _6, so read it from there.
        ref_spad_map = self.read_multi(GLOBAL_CONFIG_SPAD_ENABLES_REF_0, 6)

        # -- VL53L0X_set_reference_spads() begin (assume NVM values are valid)

        self.write_reg(0xFF, 0x01)
        self.write_reg(DYNAMIC_SPAD_REF_EN_START_OFFSET, 0x00)
        self.write_reg(DYNAMIC_SPAD_NUM_REQUESTED_REF_SPAD, 0x2C)
        self.write_reg(0xFF, 0x00)
        self.write_reg(GLOBAL_CONFIG_REF_EN_START_SELECT, 0xB4)

        # 12 is the first aperture spad
        first_spad_to_enable = 12 if info["isAperture"] else 0
        spads_enabled = 0

        for i in range(48):
            # NOTE: upstream JS uses ``refSpadMap[i / 8]`` (float index), an
            # integer-division bug. Use ``i // 8`` here (``i % 8`` stays).
            if i < first_spad_to_enable or spads_enabled == info["count"]:
                # This bit is lower than the first one that should be enabled,
                # or (reference_spad_count) bits already enabled, so zero it.
                ref_spad_map[i // 8] &= ~(1 << (i % 8))
            elif (ref_spad_map[i // 8] >> (i % 8)) & 0x1:
                spads_enabled += 1

        self.write_multi(GLOBAL_CONFIG_SPAD_ENABLES_REF_0, ref_spad_map)

        # -- VL53L0X_set_reference_spads() end

        # -- VL53L0X_load_tuning_settings() begin
        # DefaultTuningSettings from vl53l0x_tuning.h

        self.write_reg(0xFF, 0x01)
        self.write_reg(0x00, 0x00)

        self.write_reg(0xFF, 0x00)
        self.write_reg(0x09, 0x00)
        self.write_reg(0x10, 0x00)
        self.write_reg(0x11, 0x00)

        self.write_reg(0x24, 0x01)
        self.write_reg(0x25, 0xFF)
        self.write_reg(0x75, 0x00)

        self.write_reg(0xFF, 0x01)
        self.write_reg(0x4E, 0x2C)
        self.write_reg(0x48, 0x00)
        self.write_reg(0x30, 0x20)

        self.write_reg(0xFF, 0x00)
        self.write_reg(0x30, 0x09)
        self.write_reg(0x54, 0x00)
        self.write_reg(0x31, 0x04)
        self.write_reg(0x32, 0x03)
        self.write_reg(0x40, 0x83)
        self.write_reg(0x46, 0x25)
        self.write_reg(0x60, 0x00)
        self.write_reg(0x27, 0x00)
        self.write_reg(0x50, 0x06)
        self.write_reg(0x51, 0x00)
        self.write_reg(0x52, 0x96)
        self.write_reg(0x56, 0x08)
        self.write_reg(0x57, 0x30)
        self.write_reg(0x61, 0x00)
        self.write_reg(0x62, 0x00)
        self.write_reg(0x64, 0x00)
        self.write_reg(0x65, 0x00)
        self.write_reg(0x66, 0xA0)

        self.write_reg(0xFF, 0x01)
        self.write_reg(0x22, 0x32)
        self.write_reg(0x47, 0x14)
        self.write_reg(0x49, 0xFF)
        self.write_reg(0x4A, 0x00)

        self.write_reg(0xFF, 0x00)
        self.write_reg(0x7A, 0x0A)
        self.write_reg(0x7B, 0x00)
        self.write_reg(0x78, 0x21)

        self.write_reg(0xFF, 0x01)
        self.write_reg(0x23, 0x34)
        self.write_reg(0x42, 0x00)
        self.write_reg(0x44, 0xFF)
        self.write_reg(0x45, 0x26)
        self.write_reg(0x46, 0x05)
        self.write_reg(0x40, 0x40)
        self.write_reg(0x0E, 0x06)
        self.write_reg(0x20, 0x1A)
        self.write_reg(0x43, 0x40)

        self.write_reg(0xFF, 0x00)
        self.write_reg(0x34, 0x03)
        self.write_reg(0x35, 0x44)

        self.write_reg(0xFF, 0x01)
        self.write_reg(0x31, 0x04)
        self.write_reg(0x4B, 0x09)
        self.write_reg(0x4C, 0x05)
        self.write_reg(0x4D, 0x04)

        self.write_reg(0xFF, 0x00)
        self.write_reg(0x44, 0x00)
        self.write_reg(0x45, 0x20)
        self.write_reg(0x47, 0x08)
        self.write_reg(0x48, 0x28)
        self.write_reg(0x67, 0x00)
        self.write_reg(0x70, 0x04)
        self.write_reg(0x71, 0x01)
        self.write_reg(0x72, 0xFE)
        self.write_reg(0x76, 0x00)
        self.write_reg(0x77, 0x00)

        self.write_reg(0xFF, 0x01)
        self.write_reg(0x0D, 0x01)

        self.write_reg(0xFF, 0x00)
        self.write_reg(0x80, 0x01)
        self.write_reg(0x01, 0xF8)

        self.write_reg(0xFF, 0x01)
        self.write_reg(0x8E, 0x01)
        self.write_reg(0x00, 0x01)
        self.write_reg(0xFF, 0x00)
        self.write_reg(0x80, 0x00)

        # -- VL53L0X_load_tuning_settings() end

        # "Set interrupt config to new sample ready"
        # -- VL53L0X_SetGpioConfig() begin

        self.write_reg(SYSTEM_INTERRUPT_CONFIG_GPIO, 0x04)
        self.write_reg(
            GPIO_HV_MUX_ACTIVE_HIGH,
            (self.read_reg(GPIO_HV_MUX_ACTIVE_HIGH)) & ~0x10,
        )  # active low
        self.write_reg(SYSTEM_INTERRUPT_CLEAR, 0x01)

        # -- VL53L0X_SetGpioConfig() end

        self.measurement_timing_budget_us = self.get_measurement_timing_budget()

        # "Disable MSRC and TCC by default"
        # MSRC = Minimum Signal Rate Check; TCC = Target CentreCheck
        # -- VL53L0X_SetSequenceStepEnable() begin

        self.write_reg(SYSTEM_SEQUENCE_CONFIG, 0xE8)

        # -- VL53L0X_SetSequenceStepEnable() end

        # "Recalculate timing budget"
        self.set_measurement_timing_budget(self.measurement_timing_budget_us)

        # VL53L0X_StaticInit() end

        # VL53L0X_PerformRefCalibration() begin

        # -- VL53L0X_perform_vhv_calibration() begin

        self.write_reg(SYSTEM_SEQUENCE_CONFIG, 0x01)
        if not self.perform_single_ref_calibration(0x40):
            return False

        # -- VL53L0X_perform_vhv_calibration() end

        # -- VL53L0X_perform_phase_calibration() begin

        self.write_reg(SYSTEM_SEQUENCE_CONFIG, 0x02)
        if not self.perform_single_ref_calibration(0x00):
            return False

        # -- VL53L0X_perform_phase_calibration() end

        # "restore the previous Sequence Config"
        self.write_reg(SYSTEM_SEQUENCE_CONFIG, 0xE8)

        # VL53L0X_PerformRefCalibration() end

        return True

    def start_timeout(self):
        """Record the current time to check an upcoming timeout against."""
        self.timeout_start_ms = time.monotonic()

    def check_timeout_expired(self):
        """Return True when the timeout is enabled and has expired."""
        if self.io_timeout <= 0:
            return False
        elapsed_ms = (time.monotonic() - self.timeout_start_ms) * 1000.0
        return elapsed_ms > self.io_timeout

    def set_signal_rate_limit(self, limit_mcps):
        """Set the return signal rate limit check value in units of MCPS.

        Defaults to 0.25 MCPS as initialized by the ST API and this library.
        """
        if limit_mcps < 0 or limit_mcps > 511.99:
            return False

        # Q9.7 fixed point format (9 integer bits, 7 fractional bits)
        self.write_reg16(
            FINAL_RANGE_CONFIG_MIN_COUNT_RATE_RTN_LIMIT,
            int(limit_mcps * (1 << 7)),
        )
        return True

    def get_signal_rate_limit(self):
        """Get the return signal rate limit check value in MCPS."""
        return (self.read_reg16(FINAL_RANGE_CONFIG_MIN_COUNT_RATE_RTN_LIMIT)) / (1 << 7)

    def set_measurement_timing_budget(self, budget_us):
        """Set the measurement timing budget in microseconds.

        Based on VL53L0X_set_measurement_timing_budget_micro_seconds().
        """
        enables = {"tcc": False, "msrc": False, "dss": False,
                   "pre_range": False, "final_range": False}
        timeouts = {"pre_range_vcsel_period_pclks": 0,
                    "final_range_vcsel_period_pclks": 0,
                    "msrc_dss_tcc_mclks": 0,
                    "pre_range_mclks": 0,
                    "final_range_mclks": 0,
                    "msrc_dss_tcc_us": 0,
                    "pre_range_us": 0,
                    "final_range_us": 0}

        StartOverhead = 1910
        EndOverhead = 960
        MsrcOverhead = 660
        TccOverhead = 590
        DssOverhead = 690
        PreRangeOverhead = 660
        FinalRangeOverhead = 550

        MinTimingBudget = 20000

        if budget_us < MinTimingBudget:
            return False

        used_budget_us = StartOverhead + EndOverhead

        self.get_sequence_step_enables(enables)
        self.get_sequence_step_timeouts(enables, timeouts)

        if enables["tcc"]:
            used_budget_us += (timeouts["msrc_dss_tcc_us"] + TccOverhead)

        if enables["dss"]:
            used_budget_us += 2 * (timeouts["msrc_dss_tcc_us"] + DssOverhead)
        elif enables["msrc"]:
            used_budget_us += (timeouts["msrc_dss_tcc_us"] + MsrcOverhead)

        if enables["pre_range"]:
            used_budget_us += (timeouts["pre_range_us"] + PreRangeOverhead)

        if enables["final_range"]:
            used_budget_us += FinalRangeOverhead

            # "Note that the final range timeout is determined by the timing
            # budget and the sum of all other timeouts within the sequence.
            # If there is no room for the final range timeout, then an error
            # will be set. Otherwise the remaining time will be applied to
            # the final range."

            if used_budget_us > budget_us:
                # "Requested timeout too big."
                return False

            final_range_timeout_us = budget_us - used_budget_us

            # set_sequence_step_timeout() begin
            # (SequenceStepId == VL53L0X_SEQUENCESTEP_FINAL_RANGE)

            # "For the final range timeout, the pre-range timeout
            #  must be added. To do this both final and pre-range
            #  timeouts must be expressed in macro periods MClks
            #  because they have different vcsel periods."

            final_range_timeout_mclks = self.timeout_microseconds_to_mclks(
                final_range_timeout_us,
                timeouts["final_range_vcsel_period_pclks"],
            )

            if enables["pre_range"]:
                final_range_timeout_mclks += timeouts["pre_range_mclks"]

            self.write_reg16(
                FINAL_RANGE_CONFIG_TIMEOUT_MACROP_HI,
                self.encode_timeout(final_range_timeout_mclks),
            )

            # set_sequence_step_timeout() end

            self.measurement_timing_budget_us = budget_us  # store for internal reuse
        return True

    def get_measurement_timing_budget(self):
        """Get the measurement timing budget in microseconds.

        Based on VL53L0X_get_measurement_timing_budget_micro_seconds().
        """
        enables = {"tcc": False, "msrc": False, "dss": False,
                   "pre_range": False, "final_range": False}
        timeouts = {"pre_range_vcsel_period_pclks": 0,
                    "final_range_vcsel_period_pclks": 0,
                    "msrc_dss_tcc_mclks": 0,
                    "pre_range_mclks": 0,
                    "final_range_mclks": 0,
                    "msrc_dss_tcc_us": 0,
                    "pre_range_us": 0,
                    "final_range_us": 0}

        StartOverhead = 1910
        EndOverhead = 960
        MsrcOverhead = 660
        TccOverhead = 590
        DssOverhead = 690
        PreRangeOverhead = 660
        FinalRangeOverhead = 550

        # "Start and end overhead times always present"
        budget_us = StartOverhead + EndOverhead

        self.get_sequence_step_enables(enables)
        self.get_sequence_step_timeouts(enables, timeouts)

        if enables["tcc"]:
            budget_us += (timeouts["msrc_dss_tcc_us"] + TccOverhead)

        if enables["dss"]:
            budget_us += 2 * (timeouts["msrc_dss_tcc_us"] + DssOverhead)
        elif enables["msrc"]:
            budget_us += (timeouts["msrc_dss_tcc_us"] + MsrcOverhead)

        if enables["pre_range"]:
            budget_us += (timeouts["pre_range_us"] + PreRangeOverhead)

        if enables["final_range"]:
            budget_us += (timeouts["final_range_us"] + FinalRangeOverhead)

        self.measurement_timing_budget_us = budget_us  # store for internal reuse
        return budget_us

    def set_vcsel_pulse_period(self, period_type, period_pclks):
        """Set the VCSEL pulse period for the given period type.

        Valid values (even numbers only):
          pre:   12 to 18 (initialized default: 14)
          final: 8 to 14  (initialized default: 10)

        Based on VL53L0X_set_vcsel_pulse_period().
        """
        vcsel_period_reg = encode_vcsel_period(period_pclks)

        enables = {"tcc": False, "msrc": False, "dss": False,
                   "pre_range": False, "final_range": False}
        timeouts = {"pre_range_vcsel_period_pclks": 0,
                    "final_range_vcsel_period_pclks": 0,
                    "msrc_dss_tcc_mclks": 0,
                    "pre_range_mclks": 0,
                    "final_range_mclks": 0,
                    "msrc_dss_tcc_us": 0,
                    "pre_range_us": 0,
                    "final_range_us": 0}

        self.get_sequence_step_enables(enables)
        self.get_sequence_step_timeouts(enables, timeouts)

        # "Apply specific settings for the requested clock period"
        # "Re-calculate and apply timeouts, in macro periods"
        #
        # "When the VCSEL period for the pre or final range is changed, the
        # corresponding timeout must be read from the device using the current
        # VCSEL period, then the new VCSEL period can be applied. The timeout
        # then must be written back to the device using the new VCSEL period.
        # For the MSRC timeout, the same applies - this timeout being dependant
        # on the pre-range vcsel period."

        if period_type == VcselPeriodPreRange:
            # "Set phase check limits"
            if period_pclks == 12:
                self.write_reg(PRE_RANGE_CONFIG_VALID_PHASE_HIGH, 0x18)
            elif period_pclks == 14:
                self.write_reg(PRE_RANGE_CONFIG_VALID_PHASE_HIGH, 0x30)
            elif period_pclks == 16:
                self.write_reg(PRE_RANGE_CONFIG_VALID_PHASE_HIGH, 0x40)
            elif period_pclks == 18:
                self.write_reg(PRE_RANGE_CONFIG_VALID_PHASE_HIGH, 0x50)
            else:
                # invalid period
                return False
            self.write_reg(PRE_RANGE_CONFIG_VALID_PHASE_LOW, 0x08)

            # apply new VCSEL period
            self.write_reg(PRE_RANGE_CONFIG_VCSEL_PERIOD, vcsel_period_reg)

            # set_sequence_step_timeout() begin (PRE_RANGE)
            new_pre_range_timeout_mclks = self.timeout_microseconds_to_mclks(
                timeouts["pre_range_us"], period_pclks)

            self.write_reg16(
                PRE_RANGE_CONFIG_TIMEOUT_MACROP_HI,
                self.encode_timeout(new_pre_range_timeout_mclks),
            )
            # set_sequence_step_timeout() end

            # set_sequence_step_timeout() begin (MSRC)
            new_msrc_timeout_mclks = self.timeout_microseconds_to_mclks(
                timeouts["msrc_dss_tcc_us"], period_pclks)

            self.write_reg(
                MSRC_CONFIG_TIMEOUT_MACROP,
                255 if (new_msrc_timeout_mclks > 256) else (new_msrc_timeout_mclks - 1),
            )
            # set_sequence_step_timeout() end
        elif period_type == VcselPeriodFinalRange:
            if period_pclks == 8:
                self.write_reg(FINAL_RANGE_CONFIG_VALID_PHASE_HIGH, 0x10)
                self.write_reg(FINAL_RANGE_CONFIG_VALID_PHASE_LOW, 0x08)
                self.write_reg(GLOBAL_CONFIG_VCSEL_WIDTH, 0x02)
                self.write_reg(ALGO_PHASECAL_CONFIG_TIMEOUT, 0x0C)
                self.write_reg(0xFF, 0x01)
                self.write_reg(ALGO_PHASECAL_LIM, 0x30)
                self.write_reg(0xFF, 0x00)
            elif period_pclks == 10:
                self.write_reg(FINAL_RANGE_CONFIG_VALID_PHASE_HIGH, 0x28)
                self.write_reg(FINAL_RANGE_CONFIG_VALID_PHASE_LOW, 0x08)
                self.write_reg(GLOBAL_CONFIG_VCSEL_WIDTH, 0x03)
                self.write_reg(ALGO_PHASECAL_CONFIG_TIMEOUT, 0x09)
                self.write_reg(0xFF, 0x01)
                self.write_reg(ALGO_PHASECAL_LIM, 0x20)
                self.write_reg(0xFF, 0x00)
            elif period_pclks == 12:
                self.write_reg(FINAL_RANGE_CONFIG_VALID_PHASE_HIGH, 0x38)
                self.write_reg(FINAL_RANGE_CONFIG_VALID_PHASE_LOW, 0x08)
                self.write_reg(GLOBAL_CONFIG_VCSEL_WIDTH, 0x03)
                self.write_reg(ALGO_PHASECAL_CONFIG_TIMEOUT, 0x08)
                self.write_reg(0xFF, 0x01)
                self.write_reg(ALGO_PHASECAL_LIM, 0x20)
                self.write_reg(0xFF, 0x00)
            elif period_pclks == 14:
                self.write_reg(FINAL_RANGE_CONFIG_VALID_PHASE_HIGH, 0x48)
                self.write_reg(FINAL_RANGE_CONFIG_VALID_PHASE_LOW, 0x08)
                self.write_reg(GLOBAL_CONFIG_VCSEL_WIDTH, 0x03)
                self.write_reg(ALGO_PHASECAL_CONFIG_TIMEOUT, 0x07)
                self.write_reg(0xFF, 0x01)
                self.write_reg(ALGO_PHASECAL_LIM, 0x20)
                self.write_reg(0xFF, 0x00)
            else:
                # invalid period
                return False

            # apply new VCSEL period
            self.write_reg(FINAL_RANGE_CONFIG_VCSEL_PERIOD, vcsel_period_reg)

            # set_sequence_step_timeout() begin (FINAL_RANGE)
            # "For the final range timeout, the pre-range timeout
            #  must be added. To do this both final and pre-range
            #  timeouts must be expressed in macro periods MClks
            #  because they have different vcsel periods."
            new_final_range_timeout_mclks = self.timeout_microseconds_to_mclks(
                timeouts["final_range_us"], period_pclks)

            if enables["pre_range"]:
                new_final_range_timeout_mclks += timeouts["pre_range_mclks"]

            self.write_reg16(
                FINAL_RANGE_CONFIG_TIMEOUT_MACROP_HI,
                self.encode_timeout(new_final_range_timeout_mclks),
            )
            # set_sequence_step_timeout end
        else:
            # invalid type
            return False

        # "Finally, the timing budget must be re-applied"
        self.set_measurement_timing_budget(self.measurement_timing_budget_us)

        # "Perform the phase calibration. This is needed after changing on
        # vcsel period." VL53L0X_perform_phase_calibration() begin
        sequence_config = self.read_reg(SYSTEM_SEQUENCE_CONFIG)
        self.write_reg(SYSTEM_SEQUENCE_CONFIG, 0x02)
        self.perform_single_ref_calibration(0x0)
        self.write_reg(SYSTEM_SEQUENCE_CONFIG, sequence_config)
        # VL53L0X_perform_phase_calibration() end

        return True

    def get_vcsel_pulse_period(self, period_type):
        """Get the VCSEL pulse period in PCLKs for the given period type.

        Based on VL53L0X_get_vcsel_pulse_period().
        """
        if period_type == VcselPeriodPreRange:
            return decode_vcsel_period(self.read_reg(PRE_RANGE_CONFIG_VCSEL_PERIOD))
        elif period_type == VcselPeriodFinalRange:
            return decode_vcsel_period(self.read_reg(FINAL_RANGE_CONFIG_VCSEL_PERIOD))
        return 255

    def start_continuous(self, period_ms=0):
        """Start continuous ranging measurements.

        If ``period_ms`` is 0 or not given, continuous back-to-back mode is used
        (the sensor takes measurements as often as possible); otherwise,
        continuous timed mode is used.

        Based on VL53L0X_StartMeasurement().
        """
        self.write_reg(0x80, 0x01)
        self.write_reg(0xFF, 0x01)
        self.write_reg(0x00, 0x00)
        self.write_reg(0x91, self.stop_variable)
        self.write_reg(0x00, 0x01)
        self.write_reg(0xFF, 0x00)
        self.write_reg(0x80, 0x00)

        if period_ms:
            # continuous timed mode
            # VL53L0X_SetInterMeasurementPeriodMilliSeconds() begin
            osc_calibrate_val = self.read_reg16(OSC_CALIBRATE_VAL)

            if osc_calibrate_val != 0:
                period_ms *= osc_calibrate_val

            self.write_reg32(SYSTEM_INTERMEASUREMENT_PERIOD, period_ms)
            # VL53L0X_SetInterMeasurementPeriodMilliSeconds() end

            self.write_reg(SYSRANGE_START, 0x04)  # VL53L0X_REG_SYSRANGE_MODE_TIMED
        else:
            # continuous back-to-back mode
            self.write_reg(SYSRANGE_START, 0x02)  # VL53L0X_REG_SYSRANGE_MODE_BACKTOBACK

    def stop_continuous(self):
        """Stop continuous measurements.

        Based on VL53L0X_StopMeasurement().
        """
        self.write_reg(SYSRANGE_START, 0x01)  # VL53L0X_REG_SYSRANGE_MODE_SINGLESHOT

        self.write_reg(0xFF, 0x01)
        self.write_reg(0x00, 0x00)
        self.write_reg(0x91, 0x00)
        self.write_reg(0x00, 0x01)
        self.write_reg(0xFF, 0x00)

    def read_range_continuous_millimeters(self):
        """Return a range reading in millimeters when continuous mode is active.

        :raises TimeoutError: if the result interrupt status does not become
            ready within ``io_timeout`` milliseconds.
        """
        self.start_timeout()
        while (self.read_reg(RESULT_INTERRUPT_STATUS) & 0x07) == 0:
            if self.check_timeout_expired():
                self.did_timeout = True
                raise TimeoutError(
                    "timeout read RESULT_INTERRUPT_STATUS: {}ms".format(self.io_timeout)
                )

        # assumptions: Linearity Corrective Gain is 1000 (default);
        # fractional ranging is not enabled
        range_mm = self.read_reg16(RESULT_RANGE_STATUS + 10)

        self.write_reg(SYSTEM_INTERRUPT_CLEAR, 0x01)

        return range_mm

    def read_range_single_millimeters(self):
        """Perform a single-shot range measurement, return reading in mm.

        Based on VL53L0X_PerformSingleRangingMeasurement().

        :raises TimeoutError: if the start bit does not clear within
            ``io_timeout`` milliseconds.
        """
        self.write_reg(0x80, 0x01)
        self.write_reg(0xFF, 0x01)
        self.write_reg(0x00, 0x00)
        self.write_reg(0x91, self.stop_variable)
        self.write_reg(0x00, 0x01)
        self.write_reg(0xFF, 0x00)
        self.write_reg(0x80, 0x00)

        self.write_reg(SYSRANGE_START, 0x01)

        # "Wait until start bit has been cleared"
        self.start_timeout()
        while self.read_reg(SYSRANGE_START) & 0x01:
            if self.check_timeout_expired():
                self.did_timeout = True
                raise TimeoutError(
                    "timeout read SYSRANGE_START: {}ms".format(self.io_timeout)
                )

        return self.read_range_continuous_millimeters()

    def timeout_occurred(self):
        """Return whether a timeout did occur and clear the timeout flag."""
        tmp = self.did_timeout
        self.did_timeout = False
        return tmp

    def get_spad_info(self, info):
        """Get reference SPAD count and type.

        Based on VL53L0X_get_info_from_device(), but only gets reference SPAD
        count and type. ``info`` is a dict with keys ``"count"`` and
        ``"isAperture"`` which are filled in place.

        :returns: True if the info was obtained, otherwise False.
        """
        self.write_reg(0x80, 0x01)
        self.write_reg(0xFF, 0x01)
        self.write_reg(0x00, 0x00)

        self.write_reg(0xFF, 0x06)
        self.write_reg(0x83, self.read_reg(0x83) | 0x04)
        self.write_reg(0xFF, 0x07)
        self.write_reg(0x81, 0x01)

        self.write_reg(0x80, 0x01)

        self.write_reg(0x94, 0x6B)
        self.write_reg(0x83, 0x00)
        self.start_timeout()
        while self.read_reg(0x83) == 0x00:
            if self.check_timeout_expired():
                return False
        self.write_reg(0x83, 0x01)
        tmp = self.read_reg(0x92)

        info["count"] = tmp & 0x7F
        info["isAperture"] = ((tmp >> 7) & 0x01) == 0x01

        self.write_reg(0x81, 0x00)
        self.write_reg(0xFF, 0x06)
        self.write_reg(0x83, self.read_reg(0x83) & ~0x04)
        self.write_reg(0xFF, 0x01)
        self.write_reg(0x00, 0x01)

        self.write_reg(0xFF, 0x00)
        self.write_reg(0x80, 0x00)

        return True

    def get_sequence_step_enables(self, enables):
        """Get sequence step enables.

        Based on VL53L0X_GetSequenceStepEnables(). ``enables`` filled in place.
        """
        sequence_config = self.read_reg(SYSTEM_SEQUENCE_CONFIG)

        enables["tcc"] = (sequence_config >> 4) & 0x1
        enables["dss"] = (sequence_config >> 3) & 0x1
        enables["msrc"] = (sequence_config >> 2) & 0x1
        enables["pre_range"] = (sequence_config >> 6) & 0x1
        enables["final_range"] = (sequence_config >> 7) & 0x1

    def get_sequence_step_timeouts(self, enables, timeouts):
        """Get sequence step timeouts.

        Based on get_sequence_step_timeout(), but gets all timeouts instead of
        just the requested one, and also stores intermediate values.
        ``timeouts`` is filled in place.
        """
        timeouts["pre_range_vcsel_period_pclks"] = self.get_vcsel_pulse_period(
            VcselPeriodPreRange)

        timeouts["msrc_dss_tcc_mclks"] = self.read_reg(MSRC_CONFIG_TIMEOUT_MACROP) + 1
        timeouts["msrc_dss_tcc_us"] = self.timeout_mclks_to_microseconds(
            timeouts["msrc_dss_tcc_mclks"],
            timeouts["pre_range_vcsel_period_pclks"])

        timeouts["pre_range_mclks"] = self.decode_timeout(
            self.read_reg16(PRE_RANGE_CONFIG_TIMEOUT_MACROP_HI))
        timeouts["pre_range_us"] = self.timeout_mclks_to_microseconds(
            timeouts["pre_range_mclks"],
            timeouts["pre_range_vcsel_period_pclks"])

        timeouts["final_range_vcsel_period_pclks"] = self.get_vcsel_pulse_period(
            VcselPeriodFinalRange)

        timeouts["final_range_mclks"] = self.decode_timeout(
            self.read_reg16(FINAL_RANGE_CONFIG_TIMEOUT_MACROP_HI))

        if enables["pre_range"]:
            timeouts["final_range_mclks"] -= timeouts["pre_range_mclks"]

        timeouts["final_range_us"] = self.timeout_mclks_to_microseconds(
            timeouts["final_range_mclks"],
            timeouts["final_range_vcsel_period_pclks"])

    def decode_timeout(self, reg_val):
        """Decode sequence step timeout in MCLKs from register value.

        Based on VL53L0X_decode_timeout().
        """
        # format: "(LSByte * 2^MSByte) + 1"
        return ((reg_val & 0x00FF) << ((reg_val & 0xFF00) >> 8)) + 1

    def encode_timeout(self, timeout_mclks):
        """Encode sequence step timeout register value from timeout in MCLKs.

        Based on VL53L0X_encode_timeout().
        """
        # format: "(LSByte * 2^MSByte) + 1"
        ls_byte = 0
        ms_byte = 0

        if timeout_mclks > 0:
            ls_byte = int(timeout_mclks) - 1

            while (ls_byte & 0xFFFFFF00) > 0:
                ls_byte >>= 1
                ms_byte += 1

            return (ms_byte << 8) | (ls_byte & 0xFF)
        return 0

    def timeout_mclks_to_microseconds(self, timeout_period_mclks, vcsel_period_pclks):
        """Convert sequence step timeout from MCLKs to microseconds.

        Based on VL53L0X_calc_timeout_us().
        """
        macro_period_ns = calc_macro_period(vcsel_period_pclks)

        return ((timeout_period_mclks * macro_period_ns) + 500) / 1000

    def timeout_microseconds_to_mclks(self, timeout_period_us, vcsel_period_pclks):
        """Convert sequence step timeout from microseconds to MCLKs.

        Based on VL53L0X_calc_timeout_mclks().
        """
        macro_period_ns = calc_macro_period(vcsel_period_pclks)

        return (((timeout_period_us * 1000) + (macro_period_ns / 2)) / macro_period_ns)

    def perform_single_ref_calibration(self, vhv_init_byte):
        """Perform a single reference calibration.

        Based on VL53L0X_perform_single_ref_calibration().

        :returns: True on success, False on timeout.
        """
        # VL53L0X_REG_SYSRANGE_MODE_START_STOP
        self.write_reg(SYSRANGE_START, 0x01 | vhv_init_byte)

        self.start_timeout()
        while (self.read_reg(RESULT_INTERRUPT_STATUS) & 0x07) == 0:
            if self.check_timeout_expired():
                return False

        self.write_reg(SYSTEM_INTERRUPT_CLEAR, 0x01)

        self.write_reg(SYSRANGE_START, 0x00)

        return True

    def set_range_profile(self, profile):
        """Set the range profile.

        :param profile: one of ``'LONG_RANGE'``, ``'HIGH_SPEED'``,
            ``'HIGH_ACCURACY'`` (any other value sets the default profile).
        """
        if profile == "LONG_RANGE":
            # lower the return signal rate limit (default is 0.25 MCPS)
            self.set_signal_rate_limit(0.1)
            # set timing budget to 33 ms (near the default value)
            self.set_measurement_timing_budget(33000)
            # increase laser pulse periods (defaults are 14 and 10 PCLKs)
            self.set_vcsel_pulse_period(VcselPeriodPreRange, 18)
            self.set_vcsel_pulse_period(VcselPeriodFinalRange, 14)
        elif profile == "HIGH_SPEED":
            self.set_signal_rate_limit(0.25)
            # reduce timing budget to 20 ms (default is about 33 ms)
            self.set_measurement_timing_budget(20000)
            self.set_vcsel_pulse_period(VcselPeriodPreRange, 14)
            self.set_vcsel_pulse_period(VcselPeriodFinalRange, 10)
        elif profile == "HIGH_ACCURACY":
            self.set_signal_rate_limit(0.25)
            # increase timing budget to 200 ms
            self.set_measurement_timing_budget(200000)
            self.set_vcsel_pulse_period(VcselPeriodPreRange, 14)
            self.set_vcsel_pulse_period(VcselPeriodFinalRange, 10)
        else:
            # set the default profile
            self.set_signal_rate_limit(0.25)
            self.set_measurement_timing_budget(30000)
            self.set_vcsel_pulse_period(VcselPeriodPreRange, 14)
            self.set_vcsel_pulse_period(VcselPeriodFinalRange, 10)
