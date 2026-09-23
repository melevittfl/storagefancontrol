storagefancontrol 
=================
Fan speed controller based on hard drive temperature
--------------------------------------------------------

This script is meant for storage servers with lots of (spinning) hard drives.
It regulates the chassis (PWM) fan speed based on the hard drive temperature. 

The script is intended for people who build large storage servers used in an
environment (at home) where noise matters.

The hard drive temperature is monitored through SMART.

Fan speed is governed by PWM fan controls and sensors as supported the ipmitool.

The ASRock Rack motherboard fan speed is controlled by the following command

```
ipmitool raw 0x3a 0x01 0x64 0x00 0x64 0x00 0x64 0x64 0x00 0x00
		       CPU	 REAR	   FRNT1 FRNT2

0x00 is Auto
0x01 is Min
0x64 is Max
```
If you're not running this on the same hardware, you'll need to modify the script for 
however your motherboard controls fan speed.

Two controllers are available, selected with `controller` in the config.

The default is a fan curve that gives you a linear map from the hottest drive
temperature to a fan speed. 

The alternative is a PID control loop targeting a set temperature. It adapts to changing ambient conditions, but if the target is
set below what the drives actually reach at full fan speed, the integrator
saturates and the fans sit at 100% indefinitely. 

The temperature of the hottest drive is used to determine if the 
chassis fans need to run faster, slower or if they should stay at the same speed.

Both controllers are configurable, so the temperature the drives settle at is
yours to choose. 

The disk temperature is read through 'smartctl' (part of smartmontools).

The script performs a poll every 30 seconds by default. 

Forked From: https://github.com/louwrentius/storagefancontrol

INSTALL (TrueNAS SCALE)
-----------------------

Everything lives in one directory and nothing is written outside it, so it can be installed anywhere root can
read, including a home directory. It needs to be located somewhere it won't get overwritten by a TrueNAS update. 

**1. Pick a location that survives a SCALE update.**

- **`/mnt/<pool>/...`** — on a data pool. Persists. Use this.
- **`/home/<user>`** — on the boot pool. This is the OS dataset, and a major
  update or reinstall can wipe it. 

If the pool is encrypted with a passphrase and not unlocked automatically, it
will not be mounted when the Post Init script runs, and the daemon will not
start at boot.

**2. Clone it.**

```sh
cd ~                                  # or wherever you chose above
git clone <repository-url> storagefancontrol
cd storagefancontrol
chmod +x storagefancontrol.sh storagefancontrol.py
```

The daemon must run as root: `smartctl` needs raw device access and `ipmitool`
needs `/dev/ipmi0`. Post Init scripts already run as root, so the files only need
to be readable by root; the directory itself can stay owned by your user. Use
`sudo` when testing by hand.

**3. Check IPMI is reachable.**

```sh
ls -l /dev/ipmi0
which ipmitool
```

If `/dev/ipmi0` is missing, load the kernel modules:

```sh
modprobe ipmi_devintf ipmi_si
```

If they were needed, add `modprobe ipmi_devintf ipmi_si` as a Post Init
**Command** entry (see step 9) so it happens on every boot, ordered before the
script itself.

**4. Identify your drives.**

```sh
lsblk -dno NAME,SIZE,MODEL     # all disks
zpool status boot-pool         # which one(s) are the boot drives
```

On my system, I only monitor spinning rust drives. My boot drive is an SSD that I exclude from the calculation. 
You do not normally need to write the boot drive down: `boot_device = auto`
asks the boot pool directly. Linux assigns `/dev/sd*` letters in discovery
order, so the boot drive is not necessarily `sda` and can move between boots —
a hardcoded letter will eventually exclude the wrong disk and monitor the boot
drive in its place. To pin it by hand, use a stable `/dev/disk/by-id` name
(`ls -l /dev/disk/by-id/ | grep -v part`) rather than a letter.

**5. Create the config.**

```sh
cp storagefancontrol.conf.example storagefancontrol.conf
```

Then edit it:

- `device_filter` — `sd` for SATA/SAS drives, `nvme` for NVMe.
- `boot_device` — leave at `auto` to detect the boot pool's drives and exclude
  them. Override with `/dev/disk/by-id` names, comma separated for a mirrored
  boot pool.
- `controller` — `curve` (default) or `pid`, plus the `[FanCurve]` points or
  `[Pid]` gains for whichever you pick.
- `[MQTT]` if you want Home Assistant integration.

**6. Setup integration with Home Assistant (Optional).**

Set `enabled = true` under `[MQTT]`. Without it the script
runs normally and logs that it is skipping MQTT, so if you are not using Home
Assistant you can skip this step entirely.

#### MQTT Dependencies Installation

```sh
python3 install_deps.py            # or: python3 install_deps.py 2.1.0
```


**7. Check the temperature source works.**

By default (`source = auto`) temperatures come from the `drivetemp` kernel
module, which reads over ATA SCT Command Transport. 

```sh
grep -l drivetemp /sys/class/hwmon/hwmon*/name | wc -l    # should match your drive count
```

If that returns 0, load the module:

```sh
sudo modprobe drivetemp
```

To make it persistent, add `modprobe drivetemp` as a Post Init **Command**
entry alongside the IPMI one from step 3, or:

```sh
echo drivetemp | sudo tee /etc/modules-load.d/drivetemp.conf
```

The `/etc/modules-load.d` route lives on the OS dataset and is lost on a SCALE
update, so the Post Init entry is the more durable of the two.

*Falling back to smartctl.* With `source = smartctl`, each drive needs SMART
enabled or smartctl refuses to read anything:

```sh
sudo smartctl -i /dev/sdb | grep 'SMART support is'
for d in /dev/sd?; do sudo smartctl -s on "$d"; done
```

`smartctl -s on` issues the ATA SMART ENABLE OPERATIONS command. It changes a
setting on the drive, touches no data, and is safe to run on drives in a live
pool. Note that TrueNAS 25.10 removed the SMART UI entirely, so there is no
longer a per-disk checkbox, and some drives do not retain the setting across a
power cycle — another reason to prefer `drivetemp`.

**8. Start it on boot.**

Go to System Settings → Advanced → Init/Shutdown Scripts, and add:

| Field | Value |
|---|---|
| Type | Script |
| Script | the full path to `storagefancontrol.sh`, e.g. `/mnt/tank/home/mark/storagefancontrol/storagefancontrol.sh` |
| When | Post Init |

`storagefancontrol.sh` works out its own location, so it needs no editing and can
be moved along with the rest of the directory.

It starts the daemon with `systemd-run`, as a transient unit under
`system.slice`. 
```sh
systemctl status storagefancontrol
systemctl stop storagefancontrol
journalctl -u storagefancontrol
```

The unit is transient, so it exists only while running and does not need
installing or removing. `Restart=on-failure` restarts the daemon after 30s if it
crashes. Where systemd is unavailable the launcher falls back to `setsid`.

RUNNING
--------

All state lives in the install directory:

| File | Purpose |
|---|---|
| `storagefancontrol.conf` | your settings |
| `lib/` | vendored paho-mqtt, if using MQTT |
| `fan_control.log` | rotating log, 10MB × 5 |
| `startup.log` | `Running as unit: storagefancontrol.service` on a good start; anything more means the daemon failed before logging began |
| `storagefancontrol.pid` | pid of the running daemon |
| `.lock` | single-instance guard |

```sh
kill -HUP  "$(cat storagefancontrol.pid)"   # reload config, keep controller state
kill -TERM "$(cat storagefancontrol.pid)"   # stop, setting fans to pwm_safety
```


Drives are polled with `smartctl -n standby`, so sleeping drives are left asleep
and simply report no temperature. If *no* drive can be read at all — smartctl
missing, for instance — the script holds the current fan speed and logs an error
rather than treating the absence of readings as "cold" and winding the fans down.

TESTS
-----

```sh
python3 -m unittest test_mqtt_handler -v
```

Standard library only, with a stub in place of paho-mqtt, so it runs on the NAS
as well as on a workstation and needs nothing installed.

