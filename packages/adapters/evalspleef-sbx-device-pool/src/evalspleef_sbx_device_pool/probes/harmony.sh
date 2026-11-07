#!/bin/sh
# EvalSpleef HarmonyOS build container and device isolation probe.
# Checks non-root user, hdc connectivity, and clean /data/local/tmp.

pass=1

report() {
    if [ "$2" -eq 0 ]; then
        echo "CHECK $1 pass ${3:-}"
    else
        echo "CHECK $1 fail ${3:-}"
        pass=0
    fi
}

# Check non-root
uid="$(id -u 2>/dev/null || echo 0)"
[ "$uid" != "0" ]
report non-root "$?" "uid=$uid"

# Check hdc availability
if command -v hdc >/dev/null 2>&1; then
    hdc_ok=0
else
    hdc_ok=1
fi
report hdc-cli "$hdc_ok"

# Check /data/local/tmp empty
if [ -d /data/local/tmp ]; then
    count="$(ls -1 /data/local/tmp 2>/dev/null | wc -l | tr -d ' ')"
    [ "$count" = "0" ]
    report device-tmp-clean "$?" "files=$count"
else
    report device-tmp-clean 0 "no /data/local/tmp"
fi

if [ "$pass" -eq 1 ]; then
    echo "RESULT pass"
    exit 0
else
    echo "RESULT fail"
    exit 1
fi
