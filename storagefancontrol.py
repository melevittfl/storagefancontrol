#!/usr/bin/env python3
"""
This program controls the chassis fan speed through PWM based on the temperature
of the hottest hard drive in the chassis. It uses the SMART utility
for reading hard drive temperatures.

Targets TrueNAS SCALE (Linux) on an ASRock Rack E3C236D4U.
"""
import argparse
import atexit
import errno
import os
import shutil
import signal
import sys
import subprocess
import time
import configparser

import fcntl
import logging
import logging.config

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
IPMITOOL_FALLBACK = "/usr/bin/ipmitool"

# Third-party dependencies (only paho-mqtt) may be vendored into ./lib
# rather than a venv. TrueNAS SCALE ships Python without ensurepip, so
# `python3 -m venv` fails, and apt is disabled so the python3-venv package
# it suggests cannot be installed. See the README.
_LIB_DIR = os.path.join(SCRIPT_DIR, "lib")
if os.path.isdir(_LIB_DIR) and _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from log_config import *
from mqtt_handler import setup_mqtt, publish_discovery, publish_readings
from fan_curve import FanCurve
from disk_temps import Smart, SmartReadError, AllDrivesStandby
from cpu_temp import get_cpu_temperature


class PID:
    """
    Discrete PID control
    Source: http://code.activestate.com/recipes/577231-discrete-pid-controller/

    This class calculates the appropriate fan speed based on the difference
    between the current temperature and the desired (target) temperature.
    """

    def __init__(self, P, I, D, Derivator, Integrator, Integrator_max, Integrator_min):
        """
        Generic initialisation of local variables.
        """
        self.Kp = P
        self.Ki = I
        self.Kd = D
        self.Derivator = Derivator
        self.Integrator = Integrator
        self.Integrator_max = Integrator_max
        self.Integrator_min = Integrator_min

        self.set_point = 0.0
        self.error = 0.0
        self.P_value = 0.0
        self.I_value = self.Integrator * I
        self.D_value = 0.0
        self.output_min = 0
        self.output_max = 100

    def update(self, current_value):
        """
        Calculate PID output value for given reference input and feedback
        Current_value = set_point - measured value (difference)
        """
        self.error = current_value - int(self.set_point)

        self.P_value = self.Kp * self.error
        self.D_value = self.Kd * (self.error - self.Derivator)
        self.Derivator = self.error

        output = self.P_value + self.I_value + self.D_value
        saturated_high = output >= self.output_max and self.error > 0
        saturated_low = output <= self.output_min and self.error < 0
        if not saturated_high and not saturated_low:
            self.Integrator = self.Integrator + self.error

        if self.Integrator > self.Integrator_max:
            self.Integrator = self.Integrator_max
        elif self.Integrator < self.Integrator_min:
            self.Integrator = self.Integrator_min

        self.I_value = self.Integrator * self.Ki

        PID = self.P_value + self.I_value + self.D_value

        return max(self.output_min, min(self.output_max, PID))

    def set_target_value(self, set_point):
        """
        Initilize the setpoint of PID
        """
        self.set_point = set_point

    def reload(self, config):
        self.Kp = config.getint("Pid", "P")
        self.Ki = config.getint("Pid", "I")
        self.Kd = config.getint("Pid", "D")
        self.Integrator_max = config.getint("Pid", "I_max")
        self.Integrator_min = config.getint("Pid", "I_min")
        self.set_target_value(config.getint("General", "target_temperature"))

    def log_state(self):
        P = str(self.P_value)
        I = str(self.I_value)
        D = str(self.D_value)
        E = str(self.error)
        return "P={:3} | I={:3} | D={:3} | Err={:3}|".format(P, I, D, E)


class FanControl:
    """
    Controls chassis fan speed via ipmitool PWM commands.
    """

    def __init__(self):
        self.pwm_max = 64
        self.pwm_min = 1
        self.pwm_safety = 32
        self.rear_fan_ratio = 0.9
        self.cpu_temp = 0.0
        self.cpu_temp_min = 40
        self.cpu_temp_max = 80
        self.fan_speed = 50
        self.pwm_value = 0
        self.previous_pwm_value = 0
        self.dry_run = False
        self.ipmitool = shutil.which("ipmitool") or IPMITOOL_FALLBACK

    def get_pwm(self):
        """
        Return the current PWM speed setting.
        """
        return self.pwm_value

    def set_pwm(self, value):
        """
        Sets the fan speed. Only allows values between
        pwm_min and pwm_max. Values outside these ranges
        are set to either pwm_min or pwm_max as a safety
        precaution.

        ipmitool raw 0x3a 0x01 0x64 0x00 0x64 0x00 0x64 0x64 0x00 0x00
                                CPU     REAR       FRNT1 FRNT2

        Setting 0x00 means the BIOS controls the fan speed automatically

        """

        pwm_max = self.pwm_max
        pwm_min = self.pwm_min

        value = pwm_max if value > pwm_max else value

        if value < pwm_min:
            logging.debug(
                "PWM value is less than the minimum. Setting fans to BIOS control"
            )
            value = 0

        if value == 0:
            raw_rear = 0
        else:
            drive_rear = max(self.pwm_min, int(value * self.rear_fan_ratio))
            if self.cpu_temp > self.cpu_temp_min:
                cpu_fraction = min(1.0, (self.cpu_temp - self.cpu_temp_min) / (self.cpu_temp_max - self.cpu_temp_min))
                cpu_rear = max(self.pwm_min, int(cpu_fraction * self.pwm_max))
            else:
                cpu_rear = self.pwm_min
            raw_rear = max(drive_rear, cpu_rear)

        CPU = "0x00"
        REAR = "0x{:02d}".format(raw_rear)
        FRNT1 = "0x{:02d}".format(value)
        FRNT2 = "0x{:02d}".format(value)

        ipmitool_args = "raw 0x3a 0x01 %s 0x00 %s 0x00 %s %s 0x00 0x00" % (
            CPU,
            REAR,
            FRNT1,
            FRNT2,
        )

        logging.debug(ipmitool_args)

        ipmi_cmd = [self.ipmitool] + (ipmitool_args.split())

        self.pwm_value = value

        if self.previous_pwm_value != value:
            if self.dry_run:
                logging.info("DRY RUN, not executing: %s", " ".join(ipmi_cmd))
                self.previous_pwm_value = value
                return

            logging.info("PWM value changed. Updating fan speed")
            # Never raises: this is also reached from the atexit safety
            # handler, where an exception would be swallowed and the
            # failure lost.
            try:
                child = subprocess.run(ipmi_cmd, capture_output=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as e:
                logging.error("Executing ipmitool failed: %s", e)
                return

            if child.returncode != 0:
                logging.error(
                    "ipmitool exited %s: %s",
                    child.returncode,
                    child.stderr.decode("utf-8", "replace").strip(),
                )
                return

            self.previous_pwm_value = value
        else:
            logging.debug("PWM value unchanged")

    def set_fan_speed(self, percent):
        """
        Set fan speed based on a percentage of full speed.
        Values are thus 1-100 instead of raw 1-255
        """
        self.fan_speed = percent
        one_percent = float(self.pwm_max) / 100
        pwm = percent * one_percent
        self.set_pwm(int(pwm))


def log(temperature, chassis, controller):
    """
    Logging to log file.
    """
    TMP = str(temperature)
    PWM = str(chassis.get_pwm())
    PCT = str(chassis.fan_speed)
    state = controller.log_state()

    logging.info(
        "Temp: {:2} | Fan: {:2}% | PWM: {:3} | {}".format(TMP, PCT, PWM, state)
    )


_reload_config = False


def _sighup_handler(sig, frame):
    global _reload_config
    _reload_config = True
    logging.info("SIGHUP received: reloading config on next cycle")


def reload_config_values(config, chassis, controller, temp_source):
    """Update all tunable settings in place. Controller state is preserved."""
    chassis.pwm_min = config.getint("Chassis", "pwm_min")
    chassis.pwm_max = config.getint("Chassis", "pwm_max")
    chassis.pwm_safety = config.getint("Chassis", "pwm_safety")
    chassis.rear_fan_ratio = config.getfloat("Chassis", "rear_fan_ratio")
    chassis.cpu_temp_min = config.getint("Chassis", "cpu_temp_min")
    chassis.cpu_temp_max = config.getint("Chassis", "cpu_temp_max")

    controller.reload(config)

    temp_source.device_filter = config.get("Smart", "device_filter")
    temp_source.boot_device = config.get("Smart", "boot_device")
    temp_source.smart_workers = config.getint("Smart", "smart_workers")
    temp_source.smartctl_timeout = config.getint("Smart", "smartctl_timeout", fallback=30)
    temp_source.source = config.get("Smart", "source", fallback="auto").strip().lower()
    temp_source.get_block_devices()

    logging.info("Config reloaded. Controller mode and MQTT changes require a restart.")


def read_config():
    # Resolved against the script directory, not the CWD: TrueNAS SCALE
    # Post Init scripts run from an unpredictable working directory.
    config_file = os.path.join(SCRIPT_DIR, "storagefancontrol.conf")
    if not os.path.exists(config_file):
        logging.error("Config file not found: %s", config_file)
        sys.exit(1)
    conf = configparser.ConfigParser()
    conf.read(config_file)
    return conf


def get_pid_settings(config):
    """ Get PID settings """
    P = config.getint("Pid", "P")
    I = config.getint("Pid", "I")
    D = config.getint("Pid", "D")
    D_amplification = config.getint("Pid", "D_amplification")
    I_start = config.getint("Pid", "I_start")
    I_max = config.getint("Pid", "I_max")
    I_min = config.getint("Pid", "I_min")

    pid = PID(P, I, D, D_amplification, I_start, I_max, I_min)
    target_temperature = config.getint("General", "target_temperature")
    pid.set_target_value(target_temperature)

    return pid


def get_controller(config):
    """Return a PID or FanCurve controller based on the configured mode."""
    mode = config.get("General", "controller", fallback="pid").strip().lower()
    if mode == "curve":
        points = FanCurve._parse_curve(config.get("FanCurve", "curve"))
        logging.info("Using fan curve controller: %s", points)
        return FanCurve(points)
    logging.info("Using PID controller")
    return get_pid_settings(config)


def get_temp_source(config):
    """ Configure temperature source."""

    temp_source = Smart()
    temp_source.device_filter = config.get("Smart", "device_filter")
    temp_source.boot_device = config.get("Smart", "boot_device")
    temp_source.smart_workers = config.getint("Smart", "smart_workers")
    temp_source.smartctl_timeout = config.getint("Smart", "smartctl_timeout", fallback=30)
    temp_source.source = config.get("Smart", "source", fallback="auto").strip().lower()
    temp_source.get_block_devices()
    logging.info("Temperature source: %s", temp_source.source)
    logging.info(
        "Monitoring %d drive(s): %s",
        len(temp_source.block_devices),
        ", ".join(sorted(temp_source.block_devices)),
    )
    return temp_source


def get_chassis_settings(config):
    """ Initialise chassis fan settings. """

    chassis = FanControl()
    chassis.pwm_min = config.getint("Chassis", "pwm_min")
    chassis.pwm_max = config.getint("Chassis", "pwm_max")
    chassis.pwm_safety = config.getint("Chassis", "pwm_safety")
    chassis.rear_fan_ratio = config.getfloat("Chassis", "rear_fan_ratio")
    chassis.cpu_temp_min = config.getint("Chassis", "cpu_temp_min")
    chassis.cpu_temp_max = config.getint("Chassis", "cpu_temp_max")
    return chassis


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="log the ipmitool command instead of running it, so nothing touches the fans",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single poll cycle and exit",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="log to the file only, never to the terminal",
    )
    return parser.parse_args()


def main(args):
    config = read_config()
    polling_interval = config.getfloat("General", "polling_interval")

    chassis = get_chassis_settings(config)
    chassis.dry_run = args.dry_run
    if args.dry_run:
        logging.info("Dry run: fan speeds will be calculated but not applied")

    # Fail fast at startup rather than from inside the control loop, where
    # the fans would already be under our control but unreachable.
    if not os.access(chassis.ipmitool, os.X_OK):
        logging.error(
            "ipmitool not found or not executable at %s. On TrueNAS SCALE it "
            "should be at %s; check that /dev/ipmi0 exists too.",
            chassis.ipmitool,
            IPMITOOL_FALLBACK,
        )
        return 1
    logging.info("Using ipmitool at %s", chassis.ipmitool)

    def set_safety_speed():
        logging.warning("Exiting: setting fans to safety speed (PWM %s)", chassis.pwm_safety)
        chassis.set_pwm(chassis.pwm_safety)

    atexit.register(set_safety_speed)
    signal.signal(signal.SIGTERM, lambda sig, frame: sys.exit(0))
    signal.signal(signal.SIGHUP, _sighup_handler)

    controller = get_controller(config)
    temp_source = get_temp_source(config)

    mqtt_client = setup_mqtt(config)
    if mqtt_client:
        publish_discovery(mqtt_client, config, temp_source.block_devices)

    # Set the fan to the chassis min on startup.
    chassis.set_pwm(chassis.pwm_min)

    try:
        while True:
            global _reload_config
            if _reload_config:
                _reload_config = False
                config = read_config()
                polling_interval = config.getfloat("General", "polling_interval")
                reload_config_values(config, chassis, controller, temp_source)

            try:
                highest_temperature = temp_source.get_highest_temperature()
            except SmartReadError as e:
                # No drive produced a temperature. Treating that as 0C would
                # wind the fans down while the drives cook, so hold the
                # current PWM and try again next cycle. Every drive being
                # spun down is normal on an idle system; anything else is a
                # fault worth shouting about.
                standby = isinstance(e, AllDrivesStandby)
                logging.log(
                    logging.INFO if standby else logging.ERROR,
                    "%s. Holding PWM at %s", e, chassis.get_pwm(),
                )
                if args.once:
                    return 0 if standby else 1
                time.sleep(polling_interval)
                continue

            cpu_temp = get_cpu_temperature()
            logging.debug("CPU temp: %.1f°C", cpu_temp)
            chassis.cpu_temp = cpu_temp
            fan_speed = controller.update(highest_temperature)
            chassis.set_fan_speed(fan_speed)
            log(highest_temperature, chassis, controller)
            if mqtt_client:
                publish_readings(mqtt_client, config, temp_source.device_temperatures, chassis.fan_speed, cpu_temp)

            if args.once:
                return 0

            time.sleep(polling_interval)

    except (KeyboardInterrupt, SystemExit):
        pass

    return 0


def die(message):
    """Fail before logging exists, so this has to go to stderr."""
    sys.stderr.write("storagefancontrol: %s\n" % message)
    sys.exit(1)


if __name__ == "__main__":
    cli_args = parse_args()

    # ipmitool needs /dev/ipmi0 and smartctl needs raw device access, so
    # this only ever works as root. Checked before anything else, because
    # every other failure from running unprivileged is a confusing one.
    if os.geteuid() != 0:
        die("must run as root (try: sudo %s)" % " ".join(sys.argv))

    # Mirror the log to the terminal for interactive runs. The daemon is
    # started detached from a Post Init script, so it has no tty and keeps
    # logging to file only.
    try:
        configure_logging(
            console=not cli_args.quiet
            and (cli_args.once or cli_args.dry_run or sys.stderr.isatty())
        )
    except (ValueError, OSError) as e:
        die(
            "could not open the log file %s: %s\n"
            "Check ownership of the install directory." % (LOG_FILE, e)
        )

    # Held for the lifetime of the process: if this handle is garbage
    # collected the advisory lock is released and a second instance can
    # start, leaving two daemons fighting over the fans.
    try:
        lock_file = open(os.path.join(SCRIPT_DIR, ".lock"), "w")
    except OSError as e:
        die("could not open the lock file: %s" % e)
    try:
        fcntl.lockf(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError as e:
        if e.errno in (errno.EAGAIN, errno.EACCES):
            logging.error("Another instance already running")
            sys.exit(-1)
        raise

    # Kept beside the script rather than in /var/run so that the whole
    # install is self-contained and works from any directory. The .lock
    # above is what actually enforces a single instance; this file is
    # only so the process is easy to signal.
    try:
        with open(os.path.join(SCRIPT_DIR, "storagefancontrol.pid"), "w") as pid_file:
            pid_file.write(str(os.getpid()))
    except OSError as e:
        logging.warning("Could not write pid file: %s", e)

    sys.exit(main(cli_args))
