#!/usr/local/bin/python
"""
This program controls the chassis fan speed through PWM based on the temperature
of the hottest hard drive in the chassis. It uses the SMART utility
for reading hard drive temperatures.
"""
import atexit
import errno
import os
import signal
import sys
import subprocess
import re
import time
import multiprocessing as mp
import copyreg
import types
import configparser

import fcntl
import logging
import logging.config
from log_config import *
from mqtt_handler import setup_mqtt, publish_discovery, publish_readings


def _reduce_method(meth):
    """
    This is a hack to work around the fact that multiprocessing
    can't operate on class methods by default.
    """
    return (getattr, (meth.__self__, meth.__func__.__name__))


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

    def update(self, current_value):
        """
        Calculate PID output value for given reference input and feedback
        Current_value = set_point - measured value (difference)
        """
        self.error = current_value - int(self.set_point)

        self.P_value = self.Kp * self.error
        self.D_value = self.Kd * (self.error - self.Derivator)
        self.Derivator = self.error

        self.Integrator = self.Integrator + self.error

        if self.Integrator > self.Integrator_max:
            self.Integrator = self.Integrator_max
        elif self.Integrator < self.Integrator_min:
            self.Integrator = self.Integrator_min

        self.I_value = self.Integrator * self.Ki

        PID = self.P_value + self.I_value + self.D_value

        return PID

    def set_target_value(self, set_point):
        """
        Initilize the setpoint of PID
        """
        self.set_point = set_point


copyreg.pickle(types.MethodType, _reduce_method)


class Smart:
    """
    Uses SMART data from storage devices to determine the temperature
    of the hottest drive.
    """

    def __init__(self):
        """
        Init.
        """
        self.block_devices = ""
        self.device_filter = "sd"
        self.boot_device = "ada0"
        self.highest_temperature = 0
        self.device_temperatures = {}
        self.smart_workers = 24

    def get_block_devices(self):
        """
        Call 'geom part status -s' to get a list of drives
        """
        try:
            child = subprocess.Popen(
                ["geom", "part", "status", "-s"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as e:
            logging.error("Error reading block devices")
            logging.error(e)
            sys.exit(1)

        stdout, stderr = child.communicate()

        devices = set()
        for line in stdout.splitlines():
            devices.add(str(line.split()[2], "utf-8"))

        devices.discard(self.boot_device)
        devices = {d for d in devices if d.startswith(self.device_filter)}

        self.block_devices = devices

    def get_smart_data(self, device):
        """
        Call the smartctl command line utilily on a device to get the raw
        smart data output.
        """

        device = "/dev/" + device

        try:
            child = subprocess.Popen(
                ["/usr/local/sbin/smartctl", "-a", device],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError:
            print("Executing smartctl gave an error,")
            print("is smartmontools installed?")
            sys.exit(1)

        rawdata = child.communicate()

        smartdata = str(rawdata[0], "utf-8")
        return smartdata

    def get_parameter_from_smart(self, data, parameter, distance):
        """
        Retreives the desired value from the raw smart data.
        """
        regex = re.compile(parameter + "(.*)")
        match = regex.search(data)

        if match:
            tmp = match.group(1)
            length = len(tmp.split("   "))
            if length <= distance:
                distance = length - 1

            #
            # SMART data is often a bit of a mess,  so this
            # hack is used to cope with this.
            #

            try:
                model = match.group(1).split("   ")[distance].split(" ")[1]
            except:
                model = match.group(1).split("   ")[distance + 1].split(" ")[1]
            return str(model)
        return 0

    def get_temperature(self, device):
        """
        Get the current temperature of a block device.
        """
        smart_data = self.get_smart_data(device)
        temperature = int(
            self.get_parameter_from_smart(smart_data, "Temperature_Celsius", 10)
        )
        return temperature

    def get_highest_temperature(self):
        """
        Get the highest temperature of all the block devices in the system.
        Because retrieving SMART data is slow, multiprocessing is used
        to collect SMART data in parallel from multiple devices.
        """
        highest_temperature = 0
        devices = list(self.block_devices)
        pool = mp.Pool(processes=int(self.smart_workers))
        results = pool.map(self.get_temperature, devices)
        pool.close()
        pool.join()

        self.device_temperatures = dict(zip(devices, results))
        for device, temperature in self.device_temperatures.items():
            logging.debug("%s: %s°C", device, temperature)
            if temperature > highest_temperature:
                highest_temperature = temperature
        self.highest_temperature = highest_temperature

        return self.highest_temperature


class FanControl:
    """
    Controls chassis fan speed via ipmitool PWM commands.
    """

    def __init__(self):
        self.pwm_max = 64
        self.pwm_min = 1
        self.pwm_safety = 32
        self.fan_speed = 50
        self.pwm_value = 0
        self.previous_pwm_value = 0

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

        IPMITOOL = "/usr/local/bin/ipmitool"
        if value < 40:
            raw_rear = (
                value / 2
            )  # Spin up the rear case fan at half the speed of the front fans
        else:
            raw_rear = value

        if raw_rear < 20:
            raw_rear = "00"  # Set to auto

        CPU = "0x00"
        REAR = "0x" + str(raw_rear)
        FRNT1 = "0x" + str(value)
        FRNT2 = "0x" + str(value)

        ipmitool_args = "raw 0x3a 0x01 %s 0x00 %s 0x00 %s %s 0x00 0x00" % (
            CPU,
            REAR,
            FRNT1,
            FRNT2,
        )

        logging.debug(ipmitool_args)

        ipmi_cmd = [IPMITOOL] + (ipmitool_args.split())

        self.pwm_value = value

        if self.previous_pwm_value != value:
            logging.info("PWM value changed. Updating fan speed")
            try:
                child = subprocess.Popen(
                    ipmi_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
            except OSError:
                print("Executing ipmitool gave an error,")
                sys.exit(1)

            output = child.communicate()

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


def log(temperature, chassis, pid):
    """
    Logging to log file.
    """
    P = str(pid.P_value)
    I = str(pid.I_value)
    D = str(pid.D_value)
    E = str(pid.error)

    TMP = str(temperature)
    PWM = str(chassis.get_pwm())
    PCT = str(chassis.fan_speed)

    all_vars = [TMP, PCT, PWM, P, I, D, E]
    formatstring = (
        "Temp: {:2} | Fan: {:2}% | PWM: {:3} | P={:3} | I={:3} | " "D={:3} | Err={:3}|"
    )

    logging.info(formatstring.format(*all_vars))


_reload_config = False


def _sighup_handler(sig, frame):
    global _reload_config
    _reload_config = True
    logging.info("SIGHUP received: reloading config on next cycle")


def reload_config_values(config, chassis, pid, temp_source):
    """Update all tunable settings in place. PID integrator state is preserved."""
    chassis.pwm_min = config.getint("Chassis", "pwm_min")
    chassis.pwm_max = config.getint("Chassis", "pwm_max")
    chassis.pwm_safety = config.getint("Chassis", "pwm_safety")

    pid.Kp = config.getint("Pid", "P")
    pid.Ki = config.getint("Pid", "I")
    pid.Kd = config.getint("Pid", "D")
    pid.Integrator_max = config.getint("Pid", "I_max")
    pid.Integrator_min = config.getint("Pid", "I_min")
    pid.set_target_value(config.getint("General", "target_temperature"))

    temp_source.device_filter = config.get("Smart", "device_filter")
    temp_source.boot_device = config.get("Smart", "boot_device")
    temp_source.smart_workers = config.getint("Smart", "smart_workers")
    temp_source.get_block_devices()

    logging.info("Config reloaded. MQTT changes require a restart.")


def read_config():
    config_file = "./storagefancontrol.conf"
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


def get_temp_source(config):
    """ Configure temperature source."""

    temp_source = Smart()
    temp_source.device_filter = config.get("Smart", "device_filter")
    temp_source.boot_device = config.get("Smart", "boot_device")
    temp_source.smart_workers = config.getint("Smart", "smart_workers")
    temp_source.get_block_devices()
    return temp_source


def get_chassis_settings(config):
    """ Initialise chassis fan settings. """

    chassis = FanControl()
    chassis.pwm_min = config.getint("Chassis", "pwm_min")
    chassis.pwm_max = config.getint("Chassis", "pwm_max")
    chassis.pwm_safety = config.getint("Chassis", "pwm_safety")
    return chassis


def main():
    config = read_config()
    polling_interval = config.getfloat("General", "polling_interval")

    chassis = get_chassis_settings(config)

    def set_safety_speed():
        logging.warning("Exiting: setting fans to safety speed (PWM %s)", chassis.pwm_safety)
        chassis.set_pwm(chassis.pwm_safety)

    atexit.register(set_safety_speed)
    signal.signal(signal.SIGTERM, lambda sig, frame: sys.exit(0))
    signal.signal(signal.SIGHUP, _sighup_handler)

    pid = get_pid_settings(config)
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
                reload_config_values(config, chassis, pid, temp_source)

            highest_temperature = temp_source.get_highest_temperature()
            fan_speed = pid.update(highest_temperature)
            chassis.set_fan_speed(fan_speed)
            log(highest_temperature, chassis, pid)
            if mqtt_client:
                publish_readings(mqtt_client, config, temp_source.device_temperatures, chassis.fan_speed)
            time.sleep(polling_interval)

    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    logging.config.dictConfig(LOG_SETTINGS)

    f = open(".lock", "w")
    try:
        fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError as e:
        if e.errno == errno.EAGAIN:
            logging.error("Another instance already running")
            sys.exit(-1)

    with open('/var/run/storagefancontrol.pid', 'w') as f:
        f.write(str(os.getpid()))

    main()
