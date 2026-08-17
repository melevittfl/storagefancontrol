#!/bin/sh
#
# Launcher for storagefancontrol on TrueNAS SCALE.
#
# Register this under System Settings > Advanced > Init/Shutdown Scripts
# with Type "Script" and When "Post Init". The Post Init runner enforces a
# timeout, so the daemon is fully detached and this script returns at once.
#
# No editing needed: it works out its own location, so the install can live
# anywhere the root user can read, including a home directory.

DIR=$(cd "$(dirname "$0")" && pwd) || exit 1
cd "$DIR" || exit 1

# Post Init scripts already run as root; this only catches running it by
# hand. Without root, ipmitool cannot reach /dev/ipmi0 and the log file
# is usually not writable either.
if [ "$(id -u)" -ne 0 ]; then
    echo "storagefancontrol: must run as root (try: sudo $0)" >&2
    exit 1
fi

# Prefer the venv interpreter (paho-mqtt cannot be installed system-wide
# on SCALE), falling back to the system python3 if no venv is present.
if [ -x "$DIR/venv/bin/python3" ]; then
    PYTHON="$DIR/venv/bin/python3"
else
    PYTHON=/usr/bin/python3
fi

if [ ! -x "$PYTHON" ]; then
    echo "storagefancontrol: no python3 at $PYTHON" >&2
    exit 1
fi

# Anything the daemon writes before logging is configured (a syntax error,
# a missing module) would otherwise vanish into /dev/null and the failure
# would be invisible on a headless boot.
STARTUP_LOG="$DIR/startup.log"

# setsid detaches from the Post Init session so the daemon is not killed
# when this script returns. It is part of util-linux and present on SCALE;
# fall back to plain nohup if it is ever missing.
# stdin is closed as well as stdout/stderr redirected, so nothing keeps a
# handle on the terminal that started us.
if command -v setsid >/dev/null 2>&1; then
    setsid nohup "$PYTHON" "$DIR/storagefancontrol.py" >>"$STARTUP_LOG" 2>&1 </dev/null &
else
    nohup "$PYTHON" "$DIR/storagefancontrol.py" >>"$STARTUP_LOG" 2>&1 </dev/null &
fi

# Confirm it actually stayed up, rather than reporting success for a
# process that exited a moment later.
sleep 2
if [ -f "$DIR/storagefancontrol.pid" ] &&
   kill -0 "$(cat "$DIR/storagefancontrol.pid")" 2>/dev/null; then
    echo "storagefancontrol: running as pid $(cat "$DIR/storagefancontrol.pid")"
    exit 0
fi

echo "storagefancontrol: did not stay running; see $DIR/fan_control.log and $STARTUP_LOG" >&2
exit 1
