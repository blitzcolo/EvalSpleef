#!/bin/sh
# EvalSpleef Linux guest isolation probes (PRD appendix D.2, TECH-DESIGN section 7.7).
# Fed to the guest over exec stdin by the control plane; each check prints one
# "CHECK <name> <pass|fail> [detail]" line, then a final "RESULT pass|fail".

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

# I4: off-network guest has no default route
if [ -r /proc/net/route ]; then
    if awk '$1 != "lo" && $2 == "00000000" { found=1 } END { exit !found }' /proc/net/route; then
        has_route=1
    else
        has_route=0
    fi
else
    has_route=0
fi
[ "$has_route" = "0" ]
report private-net "$?" "default-route=$has_route"

# I5: no host mounts visible beyond the guest's own root filesystems
if command -v findmnt >/dev/null 2>&1; then
    table="$(findmnt -rno TARGET,SOURCE,FSTYPE 2>/dev/null)"
else
    table="$(mount 2>/dev/null)"
fi
leaks="$(printf '%s\n' "$table" | grep -Ei '(9p|virtiofs|nfs|cifs|/var/lib/libvirt)')"
if [ -z "$leaks" ]; then
    report no-host-mounts 0 ""
else
    report no-host-mounts 1 "$leaks"
fi
[ ! -S /var/run/docker.sock ] && [ ! -S /run/docker.sock ]
report no-container-socket "$?"

# I7: hypervisor limits visible to the guest
if [ -r /sys/fs/cgroup/cpu.max ]; then
    cpu="$(cat /sys/fs/cgroup/cpu.max 2>/dev/null)"
    [ -n "$cpu" ]
    report cpu-limit "$?" "cpu.max=$cpu"
else
    nproc="$(nproc 2>/dev/null || echo 0)"
    [ "$nproc" -gt 0 ]
    report cpu-limit "$?" "nproc=$nproc"
fi
if [ -r /sys/fs/cgroup/memory.max ]; then
    mem="$(cat /sys/fs/cgroup/memory.max 2>/dev/null)"
    [ -n "$mem" ] && [ "$mem" != "max" ]
    report memory-limit "$?" "memory.max=$mem"
else
    report memory-limit 0 "hypervisor limit not exposed in the guest"
fi

# I8: neutral identity - no evaluation names in env or hostname
host="$(hostname 2>/dev/null)"
case "$host" in
    *spleef*|*eval*) hok=1 ;;
    *) hok=0 ;;
esac
report neutral-hostname "$hok" "hostname=$host"
env | cut -d= -f1 | grep -Ei 'task|trial|batch|model|eval|harbor|canary|reward' >/dev/null 2>&1
[ "$?" != "0" ]
report neutral-env "$?" "no evaluation identity in the environment"

# I9: no long-lived credentials in the guest environment or common stores
env | grep -E 'sk-[A-Za-z0-9]{8,}|BEGIN [A-Z ]*PRIVATE KEY' >/dev/null 2>&1
[ "$?" != "0" ]
report no-credentials "$?" "environment carries no secret-shaped values"

if [ "$pass" = "1" ]; then
    echo "RESULT pass"
else
    echo "RESULT fail"
fi
exit "$((1 - pass))"
