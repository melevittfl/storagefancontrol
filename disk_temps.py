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

# ATA SMART attribute ids that carry a temperature, in order of preference.
TEMPERATURE_ATTRIBUTES = (194, 190)


def _attribute_temperature(data):
    """
    Pull a temperature out of the ATA SMART attribute table.

    The raw value of attribute 194 often packs extra fields alongside the
    reading (lifetime min/max, for instance), so the leading integer of
    the formatted string is used where possible and the low byte of the
    raw value only as a fallback.
    """
    table = data.get("ata_smart_attributes", {}).get("table", [])
    by_id = {a.get("id"): a for a in table if isinstance(a, dict)}

    for attribute_id in TEMPERATURE_ATTRIBUTES:
        attribute = by_id.get(attribute_id)
        if not attribute:
            continue
        raw = attribute.get("raw", {})

        text = str(raw.get("string", "")).strip()
        if text:
            head = text.split()[0]
            if head.isdigit():
                return int(head)

        value = raw.get("value")
        if isinstance(value, int):
            return value & 0xFF

    return None


def _messages(data):
    """smartctl's own diagnostics, as a list of plain strings."""
    out = []
    for message in data.get("smartctl", {}).get("messages", []):
        if isinstance(message, dict) and message.get("string"):
            out.append(str(message["string"]).strip())
        elif isinstance(message, str):
            out.append(message.strip())
    return out


def _is_standby(messages):
    """
    True when smartctl skipped the drive because it was spun down.
    Exit status alone cannot be used: bit 1 covers both the low-power
    skip and a failure to open the device.
    """
    joined = " ".join(messages).upper()
    return "STANDBY" in joined or "SLEEP" in joined or "LOW POWER" in joined


def extract_temperature(data):
    """
    Return the current temperature in Celsius from smartctl JSON, or None.

    smartctl reports temperature in a different place for each device
    class, and the top-level 'temperature' block is only emitted when the
    info/health section is requested, so every known location is tried.
    """
    if not isinstance(data, dict):
        return None

    # Preferred: the unified field, present for ATA, SCSI and NVMe alike.
    current = data.get("temperature", {}).get("current")
    if isinstance(current, int):
        return current

    # ATA drives, straight from the attribute table.
    from_attributes = _attribute_temperature(data)
    if from_attributes is not None:
        return from_attributes

    # NVMe health log, already in Celsius.
    nvme = data.get("nvme_smart_health_information_log", {}).get("temperature")
    if isinstance(nvme, int):
        return nvme

    # SAS/SCSI environmental reporting.
    reports = data.get("scsi_environmental_reports", {})
    for key in sorted(reports):
        if key.startswith("temperature"):
            value = reports[key]
            if isinstance(value, dict) and isinstance(value.get("current"), int):
                return value["current"]

    return None


class SmartReadError(Exception):
    """Raised when no drive temperature could be obtained. The caller
    must not interpret this as 'cool' and wind the fans down."""


class AllDrivesStandby(SmartReadError):
    """Every drive is spun down, so none reported a temperature. Normal
    on an idle system, and not an error, but still no basis for a control
    decision."""


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
        # Per-device flag, set when smartctl says the drive was skipped
        # because it was spun down. Written by worker threads, each under
        # its own key, and read after they have all finished.
        self._standby = {}

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

        # -x rather than -A: the top-level 'temperature' block comes from
        # the info/health section, which -A alone does not emit, and -x is
        # the only form that covers ATA, SAS and NVMe uniformly.
        try:
            child = subprocess.run(
                [self.smartctl, "-j", "-x", "-n", "standby", path],
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

        temperature = extract_temperature(data)
        if temperature is None:
            # smartctl reports why in the JSON itself. Exit status bit 1 is
            # ambiguous (it means both "skipped, low power" and "device open
            # failed"), so the messages are what actually tell them apart.
            messages = _messages(data)
            if _is_standby(messages):
                self._standby[device] = True
                logging.debug("%s: in standby, not woken to read it", device)
            else:
                logging.warning(
                    "%s: no temperature reported. smartctl exit %s%s",
                    device,
                    child.returncode,
                    (": " + "; ".join(messages)) if messages else
                    " and no message; run: smartctl -x /dev/" + device,
                )
            return None

        return int(temperature)

    def _read_device(self, device):
        """Wrapper for the worker pool: never raises, reports the error."""
        try:
            temperature = self.get_temperature(device)
            return temperature, None, self._standby.get(device, False)
        except SmartReadError as e:
            return None, e, False

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

        self._standby = {}
        workers = max(1, min(int(self.smart_workers), len(devices)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(self._read_device, devices))

        temperatures = {}
        errors = []
        standby = 0
        for device, (temperature, error, was_standby) in zip(devices, results):
            if error is not None:
                errors.append(error)
                continue
            if temperature is None:
                standby += was_standby
                continue
            logging.debug("%s: %s°C", device, temperature)
            temperatures[device] = temperature

        if not temperatures:
            # No reading from anything. Returning 0 here would look like
            # "everything is cold" and wind the fans down, so refuse to
            # produce a number at all and let the caller hold its output.
            if errors:
                raise SmartReadError(
                    "Could not read any drive: %s (%d device(s) failed)"
                    % (errors[0], len(errors))
                )
            if standby == len(devices):
                raise AllDrivesStandby(
                    "All %d drive(s) are in standby, no temperature available"
                    % len(devices)
                )
            raise SmartReadError(
                "No temperature from any of %d drive(s) (%d in standby). "
                "See the per-drive warnings above" % (len(devices), standby)
            )

        for error in errors:
            logging.error("SMART read failed: %s", error)

        self.device_temperatures = temperatures
        self.highest_temperature = max(temperatures.values())

        return self.highest_temperature
