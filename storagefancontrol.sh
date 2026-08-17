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
# would be invisible on a headless boot. Truncated per start: appending
# means a stale failure from an earlier attempt gets blamed for this one.
STARTUP_LOG="$DIR/startup.log"
: >"$STARTUP_LOG"

# setsid puts the daemon in a new session with no controlling terminal, so
# it survives the Post Init runner or the shell that started it going away.
# That is all nohup would have bought us, and chaining the two only adds a
# second binary that has to be executable: on TrueNAS SCALE 'setsid nohup'
# fails with "failed to execute nohup: Function not implemented".
#
# nohup is kept purely as a fallback for the case where setsid is missing,
# and a plain background job as a last resort.
# stdin comes from /dev/null so nothing retains a handle on the terminal.
if command -v setsid >/dev/null 2>&1; then
    setsid "$PYTHON" "$DIR/storagefancontrol.py" >"$STARTUP_LOG" 2>&1 </dev/null &
elif command -v nohup >/dev/null 2>&1; then
    nohup "$PYTHON" "$DIR/storagefancontrol.py" >"$STARTUP_LOG" 2>&1 </dev/null &
else
    "$PYTHON" "$DIR/storagefancontrol.py" >"$STARTUP_LOG" 2>&1 </dev/null &
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
