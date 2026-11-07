# Windows guest isolation preflight (PRD appendix D.2, TECH-DESIGN section 7.7).
# Run inside a Windows Sandbox over `wsb exec`; one CHECK line per invariant.
# The adapter parses lines of the form: CHECK <I#> <PASS|FAIL> <detail>.
$ErrorActionPreference = 'SilentlyContinue'

function Check([string] $id, [bool] $ok, [string] $detail) {
    $state = if ($ok) { 'PASS' } else { 'FAIL' }
    Write-Output ("CHECK {0} {1} {2}" -f $id, $state, $detail)
}

$groups = (whoami /groups | Out-String)
$admins = $groups -match 'S-1-5-32-544|Administrators'
Check 'I6' (-not $admins) 'account is not a member of the Administrators group'

$shares = @(Get-SmbShare | Where-Object { $_.Name -notin @('IPC$', 'ADMIN$', 'C$') })
Check 'I5' ($shares.Count -eq 0) ('non-default SMB shares: {0}' -f $shares.Count)

$allowlisted = 'C:\Users\WDAGUtilityAccount\Desktop'
$mounts = @(Get-ChildItem Env: | Where-Object { $_.Name -in @('USERPROFILE', 'TEMP', 'TMP') })
$outside = @($mounts | Where-Object { $_.Value -and -not $_.Value.StartsWith($allowlisted) })
Check 'I3' ($outside.Count -eq 0) ('paths outside the mapped desktop: {0}' -f $outside.Count)

$firewall = @(Get-NetFirewallProfile | Where-Object { $_.Outbound -eq 'Allow' -and $_.Enabled })
Check 'I4' ($firewall.Count -eq 0) ('firewall profiles allowing outbound: {0}' -f $firewall.Count)

$network = Test-NetConnection -ComputerName 'example.com' -Port 443 -InformationLevel Quiet
Check 'I4' (-not $network) 'no external egress to example.com:443'

$denylist = 'NOVITA|EVALSPLEEF|SPLEEF|TASK_ID|TRIAL_ID|BATCH|MODEL|CANARY|REWARD'
$leaked = @(Get-ChildItem Env: | Where-Object { $_.Name -match $denylist })
Check 'I8' ($leaked.Count -eq 0) ('evaluation identity variables: {0}' -f $leaked.Count)

$secrets = @(Get-ChildItem Env: | Where-Object {
    $_.Value -match 'sk-[A-Za-z0-9]{8,}' -or $_.Value -match 'BEGIN [A-Z ]*PRIVATE KEY'
})
Check 'I9' ($secrets.Count -eq 0) ('credential-looking environment values: {0}' -f $secrets.Count)

Write-Output 'RESULT done'
