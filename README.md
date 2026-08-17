storagefancontrol 
=================
Fan speed PID controller based on hard drive temperature
--------------------------------------------------------

This project was forked from a fan control script built for Linux. It runs on
TrueNAS SCALE using an ASRock Rack motherboard (specifically the E3C236D4U.
Other models may differ).

Earlier versions targeted FreeNAS 11 / TrueNAS Core (FreeBSD). The script is now
Linux-only: drives are discovered from `/sys/block`, temperatures come from
`smartctl --json`, and CPU temperature is read from hwmon sysfs. The `ipmitool`
raw command is unchanged, as it is a property of the BMC rather than the OS.

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


This script has been updated to handle multiple PWM devices.

Fan control is coverned by the control loop feedback mechanism [PID][pid].
Here is a [nice intro][video01] on PID. By using PID, the script always finds
the optimal fan speed no matter what the circumstances are.

[video01]: https://www.youtube.com/watch?v=UR0hOmjaHp0
[pid]: http://en.wikipedia.org/wiki/PID_controller  

For example, if you have 24 drives in a chassis, this script checks the temperature
of each drive. The temperature of the hottest drive is used to determine if the 
chassis fans need to run faster, slower or if they should stay at the same speed.

the PID controller makes sure that an optimal fan speed is found to keep the
system at a maximum of - in my case - 40C. The target temp is 
configurable.

The script logs internal variables to a log file by default.

    Temp: 40 | FAN: 51% | PWM: 130 | P=0   | I=51  | D=0   | Err=0  |
    Temp: 40 | FAN: 51% | PWM: 130 | P=0   | I=51  | D=0   | Err=0  |
    Temp: 40 | FAN: 51% | PWM: 130 | P=0   | I=51  | D=0   | Err=0  |
    Temp: 40 | FAN: 51% | PWM: 130 | P=0   | I=51  | D=0   | Err=0  |
    Temp: 39 | FAN: 43% | PWM: 109 | P=-2  | I=50  | D=-5  | Err=-1 |
    Temp: 39 | FAN: 47% | PWM: 119 | P=-2  | I=49  | D=0   | Err=-1 |
    Temp: 40 | FAN: 54% | PWM: 137 | P=0   | I=49  | D=5   | Err=0  |
    Temp: 40 | FAN: 49% | PWM: 124 | P=0   | I=49  | D=0   | Err=0  |
    Temp: 40 | FAN: 49% | PWM: 124 | P=0   | I=49  | D=0   | Err=0  |
    Temp: 40 | FAN: 49% | PWM: 124 | P=0   | I=49  | D=0   | Err=0  |


The disk temperature is read through 'smartctl' (part of smartmontools).


The script performs a poll every 30 seconds by default. 


Forked From: https://github.com/louwrentius/storagefancontrol

INSTALL (TrueNAS SCALE)
-----------------------

Everything lives in one directory: the script, its config, its log, its lock and
pid file. Nothing is written outside it, so it can be installed anywhere root can
read, including a home directory. The only absolute paths it uses are the kernel
interfaces it reads (`/sys/block`, `/sys/class/hwmon`) and the `smartctl` and
`ipmitool` binaries, which are located with `which` at startup.

**1. Pick a location that survives a SCALE update.**

Check where your home directory actually is:

```sh
getent passwd "$(whoami)"      # last-but-one field is the home directory
```

- **`/mnt/<pool>/...`** — on a data pool. Persists. Use this.
- **`/home/<user>`** — on the boot pool. This is the OS dataset, and a major
  update or reinstall can wipe it. Either move the user's home directory onto a
  data pool in Credentials → Local Users, or install under `/mnt/<pool>/`
  instead.

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
- PID/PWM values and, if you prefer a fan curve to a PID loop, `controller = curve`.
- `[MQTT]` if you want Home Assistant integration.

`storagefancontrol.conf` is not tracked by git, so `git pull` will not overwrite
your settings. The example file is kept up to date with all available options.

**6. Install paho-mqtt, if using MQTT.**

Only needed if you set `enabled = true` under `[MQTT]`. Without it the script
runs normally and logs that it is skipping MQTT, so if you are not using Home
Assistant you can skip this step entirely.

Installing Python packages on SCALE is awkward: `apt` is disabled, `pip` refuses
to install system-wide (PEP 668), and `python3 -m venv` usually fails with
*"ensurepip is not available"* because SCALE ships Python without the
`python3-venv` package. **Do not follow that error's advice to run
`apt install python3.11-venv`** — apt is disabled, and anything installed into
the OS dataset is wiped by the next update.

Instead, vendor the dependency into a `lib/` directory beside the script. It is
added to `sys.path` automatically at startup, needs no venv, and survives SCALE
updates along with the rest of the directory.

SCALE has no pip either, so use the bundled installer. It uses only the standard
library, fetches the wheel from PyPI, verifies its sha256, unpacks it into `lib/`
and checks that it imports:

```sh
python3 install_deps.py            # or: python3 install_deps.py 2.1.0
```

If the NAS has no outbound internet access, do it by hand instead — paho-mqtt is
pure Python and a wheel is just a zip. Download the `py3-none-any.whl` from
<https://pypi.org/project/paho-mqtt/#files> on another machine, copy it over, and:

```sh
mkdir -p lib && cd lib
unzip -o ~/paho_mqtt-*.whl && rm -f paho_mqtt-*.whl && cd ..
python3 -c 'import sys; sys.path.insert(0, "lib"); import paho.mqtt.client; print("ok")'
```

Re-run `install_deps.py` after a SCALE update only if you moved `lib/`; it lives
in the install directory, so it normally persists.

A venv still works if your build does have `ensurepip`; the launcher prefers
`venv/bin/python3` when present. But `lib/` is the simpler path on SCALE.

**7. Check the temperature source works.**

By default (`source = auto`) temperatures come from the `drivetemp` kernel
module, which reads over ATA SCT Command Transport. That works even on drives
with SMART switched off, costs no subprocesses, and will not spin up a sleeping
drive. It is what the TrueNAS dashboard itself uses from 25.10 onwards.

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

**8. Test before letting it drive the fans.**

```sh
sudo ./storagefancontrol.py --once --dry-run   # calculates everything, changes nothing
```

Interactive runs mirror the log to the terminal; the daemon, having no tty,
logs only to `fan_control.log`. Add `--quiet` to suppress the terminal copy.

Check that every data drive is listed and the boot drive is not, that the
temperatures look right, and that the logged `ipmitool` command is what you
expect. Then run one real cycle and confirm the fans respond:

```sh
sudo ./storagefancontrol.py --once
ipmitool sdr type fan
```

**9. Start it on boot.**

Go to System Settings → Advanced → Init/Shutdown Scripts, and add:

| Field | Value |
|---|---|
| Type | Script |
| Script | the full path to `storagefancontrol.sh`, e.g. `/mnt/tank/home/mark/storagefancontrol/storagefancontrol.sh` |
| When | Post Init |

`storagefancontrol.sh` works out its own location, so it needs no editing and can
be moved along with the rest of the directory. It fully detaches the daemon with
`setsid`, which matters because the Post Init runner enforces a timeout and would
otherwise kill it.

**10. Reboot and confirm.**

Start it by hand with `sudo ./storagefancontrol.sh`. The daemon needs root for
`/dev/ipmi0`, and its log, lock and pid files end up root-owned, so running the
launcher unprivileged afterwards will not work. Post Init scripts already run as
root, so this only affects manual starts.

```sh
cat storagefancontrol.pid          # should match a live process
ps -p "$(cat storagefancontrol.pid)" -o pid,etime,cmd
tail -20 fan_control.log           # fresh entries since boot
cat startup.log                    # empty unless the daemon failed to start
```

RUNNING
--------

All state lives in the install directory:

| File | Purpose |
|---|---|
| `storagefancontrol.conf` | your settings |
| `lib/` | vendored paho-mqtt, if using MQTT |
| `fan_control.log` | rotating log, 10MB × 5 |
| `startup.log` | only written if the daemon dies before logging starts |
| `storagefancontrol.pid` | pid of the running daemon |
| `.lock` | single-instance guard |

```sh
kill -HUP  "$(cat storagefancontrol.pid)"   # reload config, keep controller state
kill -TERM "$(cat storagefancontrol.pid)"   # stop, setting fans to pwm_safety
```

Only one instance can run at a time; a second exits immediately. The guard is the
`flock` on `.lock`, not the pid file, so a stale `storagefancontrol.pid` left by
an unclean shutdown is harmless.

Drives are polled with `smartctl -n standby`, so sleeping drives are left asleep
and simply report no temperature. If *no* drive can be read at all — smartctl
missing, for instance — the script holds the current fan speed and logs an error
rather than treating the absence of readings as "cold" and winding the fans down.

