#!/bin/sh
# EvalSpleef macOS guest isolation probes (PRD appendix D.2, TECH-DESIGN section 7.7).
# Fed to the guest over exec stdin by the control plane; each check prints one
# "CHECK <name> <pass|fail> [detail]" line, then a final "RESULT pass|fail".
# Individual checks never abort the script: a missing tool is reported, not hidden.

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

# I6: the session runs as the unprivileged agent user
uid="$(id -u 2>/dev/null || echo 99)"
[ "$uid" != "0" ] && [ -n "$uid" ]
report non-root "$?" "uid=$uid"

# I6: passwordless sudo must be unavailable to the agent
if command -v sudo >/dev/null 2>&1; then
    if sudo -n true 2>/dev/null; then
        report no-sudo 1 "sudo -n true succeeded for the agent user"
    else
        report no-sudo 0 "sudo refused"
    fi
else
    report no-sudo 0 "sudo not installed"
fi

# I5: no host directory share (virtiofs/9p/smbfs) and no host socket mounted
shares="$(mount 2>/dev/null | grep -Ei 'virtiofs|9p|smbfs|/Volumes/host' | grep -v '^/dev/' || true)"
if [ -z "$shares" ]; then
    report no-host-share 0 "no host share mounted"
else
    report no-host-share 1 "$shares"
fi
sockets=""
for s in /var/run/docker.sock /run/docker.sock /tmp/docker.sock; do
    [ -S "$s" ] && sockets="$sockets $s"
done
if [ -z "$sockets" ]; then
    report no-host-socket 0 "no container socket"
else
    report no-host-socket 1 "$sockets"
fi

# I8: the guest identity carries no evaluation marker
if command -v scutil >/dev/null 2>&1; then
    computer="$(scutil --get ComputerName 2>/dev/null || true)"
else
    computer="$(hostname 2>/dev/null || true)"
fi
case "$computer" in
    *spleef*|*eval*|*trial*|*batch*) report neutral-identity 1 "ComputerName=$computer" ;;
    *) report neutral-identity 0 "ComputerName=$computer" ;;
esac

# I8: no evaluation identity or credentials in the environment
leak="$(env 2>/dev/null | cut -d= -f1 | grep -Ei 'task|trial|batch|model|eval|harbor|canary|reward' || true)"
if [ -z "$leak" ]; then
    report neutral-env 0 "no evaluation identity in the environment"
else
    report neutral-env 1 "$leak"
fi
secret="$(env 2>/dev/null | grep -E 'sk-[A-Za-z0-9]{8,}|BEGIN [A-Z ]*PRIVATE KEY' || true)"
if [ -z "$secret" ]; then
    report no-env-secret 0 "no credential in the environment"
else
    report no-env-secret 1 "a credential-shaped value is present in the environment"
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
