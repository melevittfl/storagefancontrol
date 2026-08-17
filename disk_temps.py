"""
Drive discovery and temperature reading for Linux (TrueNAS SCALE).

Devices are enumerated from /sys/block rather than by shelling out, and
temperatures come from smartctl's JSON output, which reports a single
`temperature.current` value regardless of whether the drive is SATA, SAS
or NVMe.

Drives are polled with `-n standby` so that a sleeping drive is left
asleep and simply reports no temperature.
"""

import json
import logging
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

SYS_BLOCK = "/sys/block"
SMARTCTL_FALLBACK = "/usr/sbin/smartctl"


class SmartReadError(Exception):
    """Raised when no drive could be read at all, as opposed to all
    drives being asleep. The caller must not interpret this as 'cool'."""


class Smart:
    """
    Uses SMART data from storage devices to determine the temperature
    of the hottest drive.
    """

    def __init__(self):
        """
        Init.
        """
        self.block_devices = set()
        self.device_filter = "sd"
        self.boot_device = "sda"
        self.highest_temperature = 0
        self.device_temperatures = {}
        self.smart_workers = 24
        self.smartctl_timeout = 30
        self.smartctl = shutil.which("smartctl") or SMARTCTL_FALLBACK

    def _boot_devices(self):
        """
        The boot_device setting accepts a comma separated list so that
        mirrored boot pools can be excluded.
        """
        return {d.strip() for d in self.boot_device.split(",") if d.strip()}

    def get_block_devices(self):
        """
        Enumerate real block devices from /sys/block.

        Entries without a 'device' symlink are virtual (loop, zram, md,
        dm) and are skipped, which avoids maintaining a name blacklist.
        """
        try:
            entries = os.listdir(SYS_BLOCK)
        except OSError as e:
            logging.error("Error reading block devices from %s", SYS_BLOCK)
            logging.error(e)
            raise SystemExit(1)

        devices = set()
        for name in entries:
            if name.startswith("sr"):
                continue
            if not os.path.exists(os.path.join(SYS_BLOCK, name, "device")):
                continue
            devices.add(name)

        devices -= self._boot_devices()
        devices = {d for d in devices if d.startswith(self.device_filter)}

        if not devices:
            logging.warning(
                "No block devices matched filter '%s' in %s",
                self.device_filter,
                SYS_BLOCK,
            )

        self.block_devices = devices

    def get_temperature(self, device):
        """
        Return the current temperature of a block device in Celsius, or
        None if the drive reported none (typically because it is in
        standby). Raises SmartReadError if the drive could not be read.

        smartctl's exit status is a bitmask that is non-zero for plenty
        of benign reasons, so it is ignored: valid JSON is still written
        to stdout either way.
        """
        path = "/dev/" + device

        try:
            child = subprocess.run(
                [self.smartctl, "-j", "-A", "-n", "standby", path],
                capture_output=True,
                timeout=self.smartctl_timeout,
            )
        except FileNotFoundError:
            raise SmartReadError(
                "Could not execute %s, is smartmontools installed?" % self.smartctl
            )
        except subprocess.TimeoutExpired:
            raise SmartReadError("smartctl timed out reading %s" % path)
        except OSError as e:
            raise SmartReadError("Error executing smartctl on %s: %s" % (path, e))

        try:
            data = json.loads(child.stdout.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise SmartReadError("Could not parse smartctl output for %s: %s" % (path, e))

        temperature = data.get("temperature", {}).get("current")
        if temperature is None:
            logging.debug("%s: no temperature reported (standby?)", device)
            return None

        return int(temperature)

    def _read_device(self, device):
        """Wrapper for the worker pool: never raises, reports the error."""
        try:
            return self.get_temperature(device), None
        except SmartReadError as e:
            return None, e

    def get_highest_temperature(self):
        """
        Get the highest temperature of all the block devices in the system.
        Because retrieving SMART data is slow, a thread pool is used to
        collect SMART data in parallel from multiple devices. The workers
        only wait on smartctl subprocesses, so threads are sufficient.

        Devices that reported no temperature are left out of
        device_temperatures entirely rather than being recorded as zero.
        """
        devices = sorted(self.block_devices)
        if not devices:
            raise SmartReadError("No block devices to poll")

        workers = max(1, min(int(self.smart_workers), len(devices)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(self._read_device, devices))

        temperatures = {}
        errors = []
        for device, (temperature, error) in zip(devices, results):
            if error is not None:
                errors.append(error)
                continue
            if temperature is None:
                continue
            logging.debug("%s: %s°C", device, temperature)
            temperatures[device] = temperature

        if not temperatures and errors:
            raise SmartReadError(
                "Could not read any drive: %s (%d device(s) failed)"
                % (errors[0], len(errors))
            )

        for error in errors:
            logging.error("SMART read failed: %s", error)

        self.device_temperatures = temperatures
        self.highest_temperature = max(temperatures.values()) if temperatures else 0

        return self.highest_temperature
