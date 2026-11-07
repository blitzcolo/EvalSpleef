#!/bin/sh
# EvalSpleef Linux guest isolation probes for Hyper-V VMs (PRD appendix D.2,
# TECH-DESIGN section 7.7). Fed to the guest over exec stdin by the control
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

# I6: non-root agent user, no passwordless sudo
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

# I3: /workspace is on the guest disk and starts empty per trial
host="$(hostname 2>/dev/null)"
case "$host" in
    *spleef*|*eval*) hok=1 ;;
    *) hok=0 ;;
esac
report neutral-hostname "$hok" "hostname=$host"

# I4: an off-network VM has no default route (Private vSwitch has no uplink)
if awk '$1 != "lo" && $2 == "00000000" { found=1 } END { exit !found }' \
    /proc/net/route 2>/dev/null; then
    has_route=1
else
    has_route=0
fi
[ "$has_route" = "0" ]
report private-netns "$?" "default-route=$has_route"

# I8: no evaluation identity or credentials in the environment
env | cut -d= -f1 | grep -Ei 'task|trial|batch|model|eval|harbor|canary|reward' >/dev/null 2>&1
[ "$?" != "0" ]
report neutral-env "$?" "no evaluation identity in the environment"

if [ "$pass" = "1" ]; then
    echo "RESULT pass"
else
    echo "RESULT fail"
fi
exit "$((1 - pass))"
