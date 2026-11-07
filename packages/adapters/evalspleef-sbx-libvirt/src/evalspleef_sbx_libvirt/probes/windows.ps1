# EvalSpleef Windows guest isolation probes (PRD appendix D.2, TECH-DESIGN sections
# 7.4.6 and 7.7). Fed to the guest over exec stdin from the control plane; each
# check prints one "CHECK <name> <pass|fail> [detail]" line, then "RESULT pass|fail".

$ErrorActionPreference = 'Continue'
$script:failed = 0

function Report-Check {
    param([string]$Name, [bool]$Ok, [string]$Detail = '')
    if ($Ok) {
        Write-Output "CHECK $Name pass $Detail"
    } else {
        Write-Output "CHECK $Name fail $Detail"
        $script:failed = 1
    }
}

# I6: the session user is not an administrator (no Administrators group token).
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
Report-Check 'non-admin' (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) "user=$($identity.Name)"

# I5: no host shares are mapped into the guest.
$shares = @(Get-SmbShare -ErrorAction SilentlyContinue | Where-Object { $_.Name -notin @('IPC$', 'ADMIN$', 'C$') })
Report-Check 'no-host-shares' ($shares.Count -eq 0) "shares=$($shares.Name -join ',')"

# I4: outbound traffic is blocked by the firewall profile in off mode.
$profiles = @(Get-NetFirewallProfile -ErrorAction SilentlyContinue)
$blocked = ($profiles.Count -gt 0) -and (@($profiles | Where-Object { $_.DefaultOutboundAction -ne 'Block' }).Count -eq 0)
Report-Check 'outbound-blocked' $blocked "profiles=$($profiles.Name -join ',')"

# I4: the network canary cannot be reached from an off sandbox.
$probe = Test-NetConnection -ComputerName 'example.com' -Port 443 -InformationLevel Quiet -WarningAction SilentlyContinue
Report-Check 'no-internet' (-not $probe) 'example.com:443'

# I8/I9: no evaluation identity or credential-shaped values in the environment.
$denied = @(Get-ChildItem Env: | Where-Object { $_.Name -match 'TASK|TRIAL|BATCH|MODEL|EVAL|HARBOR|CANARY|REWARD' })
Report-Check 'neutral-env' ($denied.Count -eq 0) "names=$($denied.Name -join ',')"
$secrets = @(Get-ChildItem Env: | Where-Object { $_.Value -match 'sk-[A-Za-z0-9]{8,}|BEGIN [A-Z ]*PRIVATE KEY' })
Report-Check 'no-credentials' ($secrets.Count -eq 0) "names=$($secrets.Name -join ',')"

if ($script:failed -eq 0) {
    Write-Output 'RESULT pass'
    exit 0
}

Write-Output 'RESULT fail'
exit 1
