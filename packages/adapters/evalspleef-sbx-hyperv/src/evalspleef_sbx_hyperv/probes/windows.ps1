# EvalSpleef Windows isolation probes (PRD appendix D.2, TECH-DESIGN section 7.7).
# Fed to the guest over exec stdin by the control plane (powershell -Command -);
# each check prints one "CHECK <name> <pass|fail> [detail]" line, then a final
# "RESULT pass|fail". Exit code 0 means every check passed.

$ErrorActionPreference = 'Continue'
$pass = $true

function Report([string]$Name, [bool]$Ok, [string]$Detail) {
    if ($Ok) {
        Write-Output "CHECK $Name pass $Detail"
    } else {
        Write-Output "CHECK $Name fail $Detail"
        $script:pass = $false
    }
}

# I6: the agent account must not be an administrator
$groups = whoami /groups
$isAdmin = $false
foreach ($line in ($groups -split "`n")) {
    if ($line -match 'S-1-5-32-544' -or $line -match '\bAdministrators\b') { $isAdmin = $true }
}
Report non-admin (-not $isAdmin) "Administrators membership: $isAdmin"

# I5: no host share is visible inside the guest (only the default hidden shares)
$extraShares = Get-SmbShare | Where-Object { $_.Name -notin @('ADMIN$', 'IPC$') -and $_.Name -notmatch '^\w\$$' }
Report no-host-share ($null -eq $extraShares -or $extraShares.Count -eq 0) (
    "unexpected shares: " + (($extraShares | ForEach-Object { $_.Name }) -join ','))

# I4: the two network checks hold for network mode off only. The control plane passes the
# trial's mode in SPLEEF_NET_MODE (exec env); without it the strictest mode (off) is assumed.
$mode = if ($env:SPLEEF_NET_MODE) { $env:SPLEEF_NET_MODE } else { 'off' }
if ($mode -eq 'off') {
    # the firewall outbound default must be Block on every profile
    $profiles = Get-NetFirewallProfile
    $blocked = $true
    foreach ($profile in $profiles) {
        if ($profile.DefaultOutboundAction -ne 'Block') { $blocked = $false }
    }
    Report firewall-outbound-block $blocked ("profiles: " + (($profiles | ForEach-Object {
        "$($_.Name)=$($_.DefaultOutboundAction)" }) -join ','))

    # an external connection must fail outright in an off-network sandbox
    $probe = Test-NetConnection -ComputerName example.com -Port 443 -WarningAction SilentlyContinue
    Report no-external-egress (-not $probe.TcpTestSucceeded) ("remote=$($probe.ComputerName)")
} else {
    Report firewall-outbound-block $true "skipped: network mode $mode"
    Report no-external-egress $true "skipped: network mode $mode"
}

# I8: no credential or evaluation identity in the guest environment
$deny = 'e2b|novita|task[_-]?id|trial[_-]?id|batch|model|eval|harbor|canary|reward|secret|token|key'
$leaks = Get-ChildItem Env: | Where-Object { $_.Name -match $deny }
Report neutral-env ($null -eq $leaks -or $leaks.Count -eq 0) (
    "leaking names: " + (($leaks | ForEach-Object { $_.Name }) -join ','))

# I8: neutral computer name
$computer = $env:COMPUTERNAME
$neutral = $computer -notmatch 'spleef|eval|trial|batch'
Report neutral-computername $neutral "computername=$computer"

if ($pass) {
    Write-Output "RESULT pass"
    exit 0
} else {
    Write-Output "RESULT fail"
    exit 1
}
