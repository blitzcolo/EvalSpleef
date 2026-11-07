#!/bin/sh
# EvalSpleef Linux isolation probes for apple/container (PRD appendix D.2,
# TECH-DESIGN section 7.7). Fed to the sandbox over exec stdin by the control
# plane; each check prints one "CHECK <name> <pass|fail> [detail]" line, then a
# final "RESULT pass|fail".

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

# I2: read-only root filesystem
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

# I3: /workspace and /tmp are private tmpfs mounts
if command -v findmnt >/dev/null 2>&1; then
    workspace_fstype="$(findmnt -no FSTYPE /workspace 2>/dev/null)"
    tmp_fstype="$(findmnt -no FSTYPE /tmp 2>/dev/null)"
else
    workspace_fstype="$(awk '$2 == "/workspace" { print $3 }' /proc/mounts)"
    tmp_fstype="$(awk '$2 == "/tmp" { print $3 }' /proc/mounts)"
fi
[ "$workspace_fstype" = "tmpfs" ] && [ "$tmp_fstype" = "tmpfs" ]
report tmpfs-workspace "$?" "workspace=$workspace_fstype tmp=$tmp_fstype"

# I4: an off-network container has no default route
if awk '$1 != "lo" && $2 == "00000000" { found=1 } END { exit !found }' \
    /proc/net/route 2>/dev/null; then
    has_route=1
else
    has_route=0
fi
[ "$has_route" = "0" ]
report private-net "$?" "default-route=$has_route"

# I5: no container socket, no host mount
[ ! -S /var/run/container.sock ] && [ ! -S /run/container.sock ] \
    && [ ! -S /var/run/docker.sock ] && [ ! -S /run/docker.sock ]
report no-container-socket "$?" "no runtime socket in the guest"

# I6: minimal capability set
if [ -r /proc/self/status ]; then
    capeff="$(awk '/^CapEff:/ { print $2 }' /proc/self/status)"
    [ "$capeff" != "0000003fffffffff" ] && [ "$capeff" != "000001ffffffffff" ]
    report minimal-caps "$?" "CapEff=$capeff"
else
    report minimal-caps 0 "no /proc/self/status"
fi

# I7: cpu and memory limits are present (pids and disk are waived)
cpu="$(cat /sys/fs/cgroup/cpu.max 2>/dev/null)"
[ -n "$cpu" ]
report cgroup-cpu "$?" "cpu.max=$cpu"
mem="$(cat /sys/fs/cgroup/memory.max 2>/dev/null)"
[ -n "$mem" ] && [ "$mem" != "max" ]
report cgroup-memory "$?" "memory.max=$mem"

# I8: neutral identity - hostname must not leak the host or the platform
host="$(hostname 2>/dev/null)"
case "$host" in
    *spleef*|*eval*|*Mac*|*local*) hok=1 ;;
    *) hok=0 ;;
esac
report neutral-hostname "$hok" "hostname=$host"
env | cut -d= -f1 | grep -Ei 'task|trial|batch|model|eval|harbor|canary|reward' >/dev/null 2>&1
[ "$?" != "0" ]
report neutral-env "$?" "no evaluation identity in the environment"

if [ "$pass" = "1" ]; then
    echo "RESULT pass"
else
    echo "RESULT fail"
fi
exit "$((1 - pass))"
