#!/bin/sh
# EvalSpleef Linux isolation probes for the k8s agent-sandbox provider
# (PRD appendix D.2, TECH-DESIGN section 7.7). The control plane feeds this
# script to the sandbox over exec; each check prints one
# "CHECK <name> <pass|fail> [detail]" line, then "RESULT pass|fail".

pass=1

report() {
    # $1 = check name, $2 = ok (0/1), $3 = detail
    if [ "$2" -eq 0 ]; then
        echo "CHECK $1 pass ${3:-}"
    else
        echo "CHECK $1 fail ${3:-}"
        pass=0
    fi
}

# I6: non-root, no passwordless sudo
uid="$(id -u 2>/dev/null || echo 99)"
[ "$uid" != "0" ] && [ -n "$uid" ]
report non-root "$?" "uid=$uid"
if command -v sudo >/dev/null 2>&1; then
    sudo -n true 2>/dev/null
    [ "$?" != "0" ]
    report no-sudo "$?" "sudo present but must not grant passwordless access"
else
    report no-sudo 0 "sudo not installed"
fi

# I2: read-only root filesystem (workspace and tmp are separate emptyDirs)
if command -v findmnt >/dev/null 2>&1; then
    opts="$(findmnt -no OPTIONS / 2>/dev/null)"
    case "$opts" in
        ro,*) ro_ok=0 ;;
        *) ro_ok=1 ;;
    esac
else
    touch /.__probe 2>/dev/null
    if [ "$?" != "0" ]; then ro_ok=0; else ro_ok=1; fi
fi
report ro-rootfs "$ro_ok" "options=$opts"

# I3 / I5: workspace is writable and no hostPath is mounted below it
[ -w /workspace ]
report writable-workspace "$?"
findmnt -rno SOURCE,TARGET 2>/dev/null | awk '$2 ~ "^/workspace" && $1 ~ /^\// { found=1 } END { exit !found }'
[ "$?" != "0" ]
report workspace-not-hostpath "$?"

# I5: no docker socket and no service-account token mount
[ ! -S /var/run/docker.sock ] && [ ! -S /run/docker.sock ]
report no-docker-socket "$?"
[ ! -e /var/run/secrets/kubernetes.io/serviceaccount/token ] \
    && [ ! -e /run/secrets/kubernetes.io/serviceaccount/token ]
report no-sa-token "$?"

# I5: minimal capability set (CapEff must not be the full mask)
if [ -r /proc/self/status ]; then
    capeff="$(awk '/^CapEff:/ { print $2 }' /proc/self/status)"
    [ "$capeff" != "0000003fffffffff" ] && [ "$capeff" != "000001ffffffffff" ]
    report minimal-caps "$?" "CapEff=$capeff"
else
    report minimal-caps 0 "no /proc/self/status"
fi

# I7: cgroup limits are present
if [ -r /sys/fs/cgroup/cpu.max ]; then
    cpu="$(cat /sys/fs/cgroup/cpu.max 2>/dev/null)"
    [ -n "$cpu" ]
    report cgroup-cpu "$?" "cpu.max=$cpu"
else
    report cgroup-cpu 1 "no /sys/fs/cgroup/cpu.max"
fi
if [ -r /sys/fs/cgroup/memory.max ]; then
    mem="$(cat /sys/fs/cgroup/memory.max 2>/dev/null)"
    [ -n "$mem" ] && [ "$mem" != "max" ]
    report cgroup-memory "$?" "memory.max=$mem"
else
    report cgroup-memory 1 "no /sys/fs/cgroup/memory.max"
fi

# I8: neutral hostname
host="$(hostname 2>/dev/null)"
[ "$host" = "sandbox" ]
report neutral-hostname "$?" "hostname=$host"
env | cut -d= -f1 | grep -Ei 'task|trial|batch|model|eval|harbor|canary|reward' >/dev/null 2>&1
[ "$?" != "0" ]
report neutral-env "$?" "no evaluation identity in the environment"

if [ "$pass" = "1" ]; then
    echo "RESULT pass"
else
    echo "RESULT fail"
fi
exit "$((1 - pass))"
