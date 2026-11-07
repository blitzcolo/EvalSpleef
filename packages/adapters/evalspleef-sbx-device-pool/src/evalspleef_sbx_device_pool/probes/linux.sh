#!/bin/sh
# EvalSpleef Linux preflight probe for device pool build container.

pass=1

report() {
    if [ "$2" -eq 0 ]; then
        echo "CHECK $1 pass ${3:-}"
    else
        echo "CHECK $1 fail ${3:-}"
        pass=0
    fi
}

uid="$(id -u 2>/dev/null || echo 0)"
[ "$uid" != "0" ]
report non-root "$?" "uid=$uid"

[ ! -S /var/run/docker.sock ] && [ ! -S /run/docker.sock ]
report no-docker-socket "$?"

if [ "$pass" -eq 1 ]; then
    echo "RESULT pass"
    exit 0
else
    echo "RESULT fail"
    exit 1
fi
