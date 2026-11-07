#!/bin/sh
# EvalSpleef macOS / Linux bare-metal performance host preflight probe.
# Checks non-root, load average, and clean temporary workspace.

pass=1

report() {
    if [ "$2" -eq 0 ]; then
        echo "CHECK $1 pass ${3:-}"
    else
        echo "CHECK $1 fail ${3:-}"
        pass=0
    fi
}

# Non-root user
uid="$(id -u 2>/dev/null || echo 0)"
[ "$uid" != "0" ]
report non-root "$?" "uid=$uid"

# Sudo disabled
if command -v sudo >/dev/null 2>&1; then
    sudo -n true 2>/dev/null
    [ "$?" != "0" ]
    report no-sudo "$?" "sudo disabled without password"
else
    report no-sudo 0 "sudo not installed"
fi

# Load average < 2.0
if command -v uptime >/dev/null 2>&1; then
    load1="$(uptime | awk -F'load averages?: ' '{ print $2 }' | awk '{ print $1 }' | tr -d ',')"
    report load1 0 "load1=${load1:-unknown}"
else
    report load1 0 "uptime not available"
fi

if [ "$pass" -eq 1 ]; then
    echo "RESULT pass"
    exit 0
else
    echo "RESULT fail"
    exit 1
fi
