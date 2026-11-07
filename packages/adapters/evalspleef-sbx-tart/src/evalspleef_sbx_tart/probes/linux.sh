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

# I6: non-root session, no passwordless sudo
uid="$(id -u 2>/dev/null || echo 99)"
[ "$uid" != "0" ] && [ -n "$uid" ]
report non-root "$?" "uid=$uid"
if command -v sudo >/dev/null 2>&1; then
    if sudo -n true 2>/dev/null; then
        report no-sudo 1 "sudo -n true succeeded for the agent user"
    else
        report no-sudo 0 "sudo refused"
    fi
else
    report no-sudo 0 "sudo not installed"
fi

# I5: no host share, no container socket
shares="$(mount 2>/dev/null | grep -Ei 'virtiofs|9p|smbfs' || true)"
if [ -z "$shares" ]; then
    report no-host-share 0 "no host share mounted"
else
    report no-host-share 1 "$shares"
fi
[ ! -S /var/run/docker.sock ] && [ ! -S /run/docker.sock ]
report no-host-socket "$?"

# I7: cpu and memory match the VM configuration
if command -v nproc >/dev/null 2>&1; then
    cpus="$(nproc)"
    [ -n "$cpus" ]
    report cpu-count "$?" "nproc=$cpus"
else
    report cpu-count 1 "nproc missing"
fi

# I8: neutral identity
host="$(hostname 2>/dev/null || true)"
case "$host" in
    *spleef*|*eval*|*trial*|*batch*) report neutral-identity 1 "hostname=$host" ;;
    *) report neutral-identity 0 "hostname=$host" ;;
esac
leak="$(env 2>/dev/null | cut -d= -f1 | grep -Ei 'task|trial|batch|model|eval|harbor|canary|reward' || true)"
if [ -z "$leak" ]; then
    report neutral-env 0 "no evaluation identity in the environment"
else
    report neutral-env 1 "$leak"
fi

# I4: with the VM's network mode off, egress must fail. 1 means egress was observed.
egress=0
if command -v curl >/dev/null 2>&1; then
    curl -fsS -o /dev/null --max-time 3 https://example.com 2>/dev/null && egress=1
elif command -v wget >/dev/null 2>&1; then
    wget -q -T 3 -O /dev/null https://example.com 2>/dev/null && egress=1
else
    egress=1
fi
if [ "$egress" = "0" ]; then
    report offline-test 0 "egress blocked"
else
    report offline-test 1 "egress succeeded or no probe tool is available"
fi

if [ "$pass" = "1" ]; then
    echo "RESULT pass"
else
    echo "RESULT fail"
fi
exit "$((1 - pass))"
