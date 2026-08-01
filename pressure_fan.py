# pressure_fan.py - Klipper extra for pressure-based PWM fan control
# Place in: ~/klipper/klippy/extras/pressure_fan.py
#
# Uses TWO BME280 sensors to compute a true live differential pressure:
#   enclosure_sensor  - inside the Voron enclosure
#   ambient_sensor    - outside the enclosure (ambient reference)
#
# differential_pa = (enclosure_hpa - ambient_hpa) * 100
# Target is negative (e.g. -5 Pa) = enclosure below ambient pressure.
#
# Wiring (Raspberry Pi Pico):
#   GP4  = I2C0 SDA  -> enclosure BME280 SDA   (inside enclosure)
#   GP5  = I2C0 SCL  -> enclosure BME280 SCL
#   GP6  = I2C1 SDA  -> ambient   BME280 SDA   (outside enclosure)
#   GP7  = I2C1 SCL  -> ambient   BME280 SCL
#   GP2  = PWM fan signal (25 kHz)
#   3.3V -> both BME280 VCC
#   GND  -> both BME280 GND, fan signal GND
#           (fan motor power from separate 12/24V rail)
#
# Both sensors can share the same I2C address (0x76) because they are on
# separate buses. If you want them on the same bus instead, wire SDO
# differently on each (GND = 0x76, 3V3 = 0x77).

import logging

REPORT_TIME         = 1.0   # seconds between control loop iterations
CALIBRATE_SAMPLES   = 10    # samples to average during calibration (~10 seconds)
FILTER_SIZE         = 5     # default moving average window (5 seconds at 1s loop rate)
PRE_CAL_FAN_OFF     = 10.0  # seconds fan must be off before calibration begins


class MovingAverage:
    """Simple moving average filter over a fixed-size sample window."""
    def __init__(self, size):
        self._size = size
        self._buf  = []

    def update(self, value):
        self._buf.append(value)
        if len(self._buf) > self._size:
            self._buf.pop(0)
        return sum(self._buf) / len(self._buf)

    def ready(self):
        """True once the buffer has filled to its full window size."""
        return len(self._buf) >= self._size

    def reset(self):
        self._buf = []


class PressureFan:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.reactor = self.printer.get_reactor()

        # --- Config parameters ---
        self.target_pa       = config.getfloat('target_pressure', -5.0)
        self.min_speed       = config.getfloat('min_speed', 0.0, minval=0.0, maxval=1.0)
        self.max_speed       = config.getfloat('max_speed', 1.0, minval=0.0, maxval=1.0)
        self.deadband_pa     = config.getfloat('deadband', 0.5, minval=0.0)
        self.pressure_offset = config.getfloat('pressure_offset', 0.0)

        # PID gains (tunable in printer.cfg)
        self.Kp = config.getfloat('pid_Kp', 0.08)
        self.Ki = config.getfloat('pid_Ki', 0.01)
        self.Kd = config.getfloat('pid_Kd', 0.02)

        # Moving average filter size (1 = no filtering)
        filter_size = config.getint('filter_size', FILTER_SIZE, minval=1, maxval=30)
        self._enc_filter = MovingAverage(filter_size)
        self._amb_filter = MovingAverage(filter_size)

        # --- Sensor names from config ---
        self._enclosure_sensor_name = config.get('enclosure_sensor')
        self._ambient_sensor_name   = config.get('ambient_sensor')

        # --- PID state ---
        self._integral   = 0.0
        self._last_error = 0.0
        self._last_time  = None

        # --- Reported values ---
        self._diff_pa       = 0.0
        self._enclosure_hpa = 0.0
        self._ambient_hpa   = 0.0
        self._current_speed = 0.0

        # --- Calibration state ---
        self._calibrated  = False   # fan locked off until first calibration
        self._cal_samples = []
        self._cal_gcmd    = None
        self._cal_waiting = False   # True while waiting for fan-off settle time

        # --- Resolve sensors and start loop after objects are ready ---
        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        self.printer.register_event_handler("klippy:ready",   self._handle_ready)

        # --- Fan pin ---
        ppins = self.printer.lookup_object('pins')
        self._mcu_fan = ppins.setup_pin('pwm', config.get('fan_pin'))
        self._mcu_fan.setup_cycle_time(0.000040)  # 25 kHz
        self._mcu_fan.setup_start_value(0.0, 0.0)

        # --- GCode commands ---
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command(
            "SET_PRESSURE_FAN", "FAN", self.name,
            self.cmd_SET_PRESSURE_FAN,
            desc="Set pressure fan target in Pa (e.g. TARGET=-5.0)")
        gcode.register_mux_command(
            "QUERY_PRESSURE_FAN", "FAN", self.name,
            self.cmd_QUERY_PRESSURE_FAN,
            desc="Report live differential pressure and fan speed")
        gcode.register_mux_command(
            "CALIBRATE_PRESSURE_FAN", "FAN", self.name,
            self.cmd_CALIBRATE_PRESSURE_FAN,
            desc="Turn fan off, wait 10s, sample sensors, compute pressure offset")

    # ------------------------------------------------------------------
    # Startup handlers
    # ------------------------------------------------------------------
    def _handle_connect(self):
        self._enclosure_sensor = self.printer.lookup_object(
            self._enclosure_sensor_name)
        self._ambient_sensor = self.printer.lookup_object(
            self._ambient_sensor_name)
        self._mcu = self.printer.lookup_object('mcu')  # cache MCU object
        logging.info(
            "pressure_fan %s: enclosure='%s'  ambient='%s'",
            self.name, self._enclosure_sensor_name, self._ambient_sensor_name)

    def _handle_ready(self):
        self._last_time = self.reactor.monotonic()
        self.reactor.register_timer(self._control_loop,
                                    self._last_time + REPORT_TIME)
        logging.info(
            "pressure_fan %s: fan locked off — run CALIBRATE_PRESSURE_FAN "
            "before control loop activates", self.name)

    # ------------------------------------------------------------------
    # PID control loop
    #
    # Fan is held off until calibration has been completed at least once.
    # Both raw sensor readings pass through a moving average filter before
    # the differential is computed.
    #
    # differential_pa = (filtered_enc_hpa - filtered_amb_hpa) * 100 + offset
    # Negative when enclosure is below ambient pressure (desired).
    # ------------------------------------------------------------------
    def _control_loop(self, eventtime):
        # Hold fan off until calibrated
        if not self._calibrated:
            self._set_fan_speed(eventtime, 0.0)
            return eventtime + REPORT_TIME

        enc_status = self._enclosure_sensor.get_status(eventtime)
        amb_status = self._ambient_sensor.get_status(eventtime)

        enc_hpa = enc_status.get('pressure', None)
        amb_hpa = amb_status.get('pressure', None)

        if enc_hpa is None or amb_hpa is None:
            self._set_fan_speed(eventtime, self.min_speed)
            return eventtime + REPORT_TIME

        # Apply moving average filter to each sensor independently
        self._enclosure_hpa = self._enc_filter.update(enc_hpa)
        self._ambient_hpa   = self._amb_filter.update(amb_hpa)
        diff_pa = (self._enclosure_hpa - self._ambient_hpa) * 100.0 \
                  + self.pressure_offset
        self._diff_pa = diff_pa

        error = diff_pa - self.target_pa

        dt = eventtime - self._last_time
        self._last_time = eventtime

        # Integral with anti-windup clamp
        self._integral += error * dt
        self._integral = max(-100.0, min(100.0, self._integral))

        # Derivative
        derivative = (error - self._last_error) / dt if dt > 0 else 0.0
        self._last_error = error

        output = (self.Kp * error
                  + self.Ki * self._integral
                  + self.Kd * derivative)

        # Deadband: within ±deadband_pa of target, coast at min_speed
        if abs(error) < self.deadband_pa:
            speed = self.min_speed
        else:
            speed = max(self.min_speed, min(self.max_speed, output))

        self._set_fan_speed(eventtime, speed)
        return eventtime + REPORT_TIME

    def _set_fan_speed(self, eventtime, speed):
        self._current_speed = speed
        curtime = self._mcu.estimated_print_time(eventtime + 0.1)
        self._mcu_fan.set_pwm(curtime, speed)

    # ------------------------------------------------------------------
    # Calibration routine
    #
    # Steps:
    #   1. Command fan off immediately
    #   2. Wait PRE_CAL_FAN_OFF seconds for airflow to settle
    #   3. Sample both sensors CALIBRATE_SAMPLES times
    #   4. Compute and apply offset, reset filters and PID, enable fan
    # ------------------------------------------------------------------
    def cmd_CALIBRATE_PRESSURE_FAN(self, gcmd):
        if self._cal_waiting or len(self._cal_samples) > 0:
            gcmd.respond_info(
                "pressure_fan %s: calibration already in progress" % self.name)
            return

        self._cal_gcmd    = gcmd
        self._cal_waiting = True

        # Force fan off immediately
        eventtime = self.reactor.monotonic()
        self._set_fan_speed(eventtime, 0.0)

        gcmd.respond_info(
            "pressure_fan %s: fan stopped — waiting %.0fs for airflow to settle..."
            % (self.name, PRE_CAL_FAN_OFF))

        self.reactor.register_timer(self._calibrate_start,
                                    eventtime + PRE_CAL_FAN_OFF)

    def _calibrate_start(self, eventtime):
        """Called after the fan-off settle delay — begin sampling."""
        self._cal_waiting = False
        self._cal_samples = []
        self._cal_gcmd.respond_info(
            "pressure_fan %s: airflow settled, sampling for ~%ds..."
            % (self.name, CALIBRATE_SAMPLES))
        self.reactor.register_timer(self._calibrate_sample,
                                    eventtime + REPORT_TIME)
        return self.reactor.NEVER

    def _calibrate_sample(self, eventtime):
        enc_status = self._enclosure_sensor.get_status(eventtime)
        amb_status = self._ambient_sensor.get_status(eventtime)
        enc_hpa = enc_status.get('pressure', None)
        amb_hpa = amb_status.get('pressure', None)

        if enc_hpa is not None and amb_hpa is not None:
            self._cal_samples.append((enc_hpa - amb_hpa) * 100.0)

        if len(self._cal_samples) < CALIBRATE_SAMPLES:
            return eventtime + REPORT_TIME

        # Enough samples — compute and apply offset
        raw_offset = sum(self._cal_samples) / len(self._cal_samples)
        self.pressure_offset = -raw_offset

        # Reset PID and filters so stale data doesn't bias first readings
        self._integral = 0.0
        self._last_error = 0.0
        self._enc_filter.reset()
        self._amb_filter.reset()

        # Unlock the fan
        self._calibrated = True

        self._cal_gcmd.respond_info(
            "pressure_fan %s: calibration complete — fan control active\n"
            "  samples         : %d\n"
            "  raw differential: %.3f Pa\n"
            "  applied offset  : %.3f Pa\n"
            "  To make permanent, add to [pressure_fan %s]:\n"
            "    pressure_offset: %.3f"
            % (self.name, len(self._cal_samples),
               raw_offset, self.pressure_offset,
               self.name, self.pressure_offset))

        self._cal_samples = []
        self._cal_gcmd    = None
        return self.reactor.NEVER

    # ------------------------------------------------------------------
    # GCode commands
    # ------------------------------------------------------------------
    def cmd_SET_PRESSURE_FAN(self, gcmd):
        self.target_pa = gcmd.get_float('TARGET', self.target_pa)
        self._integral = 0.0
        gcmd.respond_info(
            "pressure_fan %s: target set to %.1f Pa" % (self.name, self.target_pa))

    def cmd_QUERY_PRESSURE_FAN(self, gcmd):
        if not self._calibrated:
            status = "NOT CALIBRATED — run CALIBRATE_PRESSURE_FAN"
        elif self._cal_waiting:
            status = "waiting for fan-off settle"
        elif self._cal_gcmd is not None:
            status = "sampling..."
        else:
            status = "active"
        gcmd.respond_info(
            "pressure_fan %s:\n"
            "  status          : %s\n"
            "  enclosure       : %.4f hPa\n"
            "  ambient         : %.4f hPa\n"
            "  diff (corrected): %.2f Pa  (target: %.1f Pa)\n"
            "  pressure offset : %.3f Pa\n"
            "  fan speed       : %.1f%%"
            % (self.name, status,
               self._enclosure_hpa,
               self._ambient_hpa,
               self._diff_pa,
               self.target_pa,
               self.pressure_offset,
               self._current_speed * 100.0))

    # ------------------------------------------------------------------
    # Status dict — available in Mainsail/Fluidd and macros as:
    #   printer["pressure_fan exhaust"].differential_pa
    #   printer["pressure_fan exhaust"].fan_speed  etc.
    # ------------------------------------------------------------------
    def get_status(self, eventtime):
        return {
            'differential_pa' : round(self._diff_pa, 2),
            'enclosure_hpa'   : round(self._enclosure_hpa, 4),
            'ambient_hpa'     : round(self._ambient_hpa, 4),
            'target_pa'       : self.target_pa,
            'pressure_offset' : round(self.pressure_offset, 3),
            'fan_speed'       : round(self._current_speed, 3),
            'calibrated'      : self._calibrated,
        }

def load_config_prefix(config):
    return PressureFan(config)
