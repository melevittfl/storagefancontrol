"""
CPU temperature reading for Linux (TrueNAS SCALE).

Replaces the FreeBSD 'sysctl -a dev.cpu' scrape. Values are read straight
out of hwmon sysfs, so there is no subprocess and no dependency on
lm-sensors (which cannot be installed on SCALE anyway).
"""

import glob
import logging
import os

HWMON_ROOT = "/sys/class/hwmon"

# coretemp covers the Intel Xeon E3-1200 v5 in the E3C236D4U;
# k10temp is the AMD equivalent, accepted so the module is portable.
DRIVERS = ("coretemp", "k10temp")

_hwmon_path = None
_warned = False


def _find_hwmon():
    """Locate the hwmon directory belonging to a CPU temperature driver."""
    for name_file in sorted(glob.glob(os.path.join(HWMON_ROOT, "hwmon*", "name"))):
        try:
            with open(name_file) as f:
                name = f.read().strip()
        except OSError:
            continue
        if name in DRIVERS:
            path = os.path.dirname(name_file)
            logging.info("Reading CPU temperature from %s (%s)", path, name)
            return path
    return None


def get_cpu_temperature():
    """
    Return the highest CPU core temperature in Celsius, or 0.0 if it
    could not be read. CPU temperature only ever raises the rear fan
    speed, so 0.0 degrades to drive-only control rather than to
    something unsafe.
    """
    global _hwmon_path, _warned

    if _hwmon_path is None:
        _hwmon_path = _find_hwmon()

    if _hwmon_path is None:
        if not _warned:
            logging.warning(
                "No CPU temperature driver found in %s (looked for %s). "
                "Rear fan will be driven by drive temperature only.",
                HWMON_ROOT,
                ", ".join(DRIVERS),
            )
            _warned = True
        return 0.0

    temps = []
    for input_file in glob.glob(os.path.join(_hwmon_path, "temp*_input")):
        try:
            with open(input_file) as f:
                temps.append(int(f.read().strip()) / 1000.0)
        except (OSError, ValueError) as e:
            logging.debug("Could not read %s: %s", input_file, e)

    if not temps:
        # The device may have gone away, e.g. after a module reload.
        logging.error("No temperature inputs readable under %s", _hwmon_path)
        _hwmon_path = None
        return 0.0

    return max(temps)
