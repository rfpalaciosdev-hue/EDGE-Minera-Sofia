# Exercise the actual registration helper without Task Scheduler or Windows.
$ErrorActionPreference = 'Stop'
$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$installer = Join-Path $root 'scripts/windows/Install-EdgeHealthTask.ps1'
$tokens = $null; $parseErrors = $null
$tree = [Management.Automation.Language.Parser]::ParseFile($installer, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
$helper = $tree.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Register-EdgeHealthSchedule' }, $false)
Invoke-Expression $helper.Extent.Text
function Assert-Equal($Actual, $Expected, [string]$Label) {
    if ($Actual -cne $Expected) { throw "$Label - expected '$Expected', got '$Actual'" }
}
function New-ScheduledTaskPrincipal($UserId, $LogonType, $RunLevel) {
    $script:Principal = @{ UserId = $UserId; LogonType = $LogonType; RunLevel = $RunLevel }
    return $script:Principal
}
function New-ScheduledTaskAction($Execute, $Argument, $WorkingDirectory) { return @{ Execute = $Execute } }
function New-ScheduledTaskTrigger([switch]$Once, $At, $RepetitionInterval) { return @{ At = $At } }
function New-ScheduledTaskSettingsSet($MultipleInstances, $ExecutionTimeLimit, [switch]$StartWhenAvailable, [switch]$AllowStartIfOnBatteries, [switch]$DontStopIfGoingOnBatteries) { return @{} }
function Register-ScheduledTask {
    [CmdletBinding()]
    param($TaskName, $Action, $Trigger, $Principal, $Settings, $Description, [switch]$Force)
    if ($script:DenyRegistration) { throw [UnauthorizedAccessException]::new('Access denied') }
    return @{ TaskName = $TaskName }
}
$parameters = @{ TaskName = 'fixture'; Collector = 'C:\Health\Collect.ps1'; OutputDirectory = 'C:\Health'; UserId = 'PC\Usuario'; ServiceAccount = $false; PowerShellExe = 'powershell.exe' }
$script:DenyRegistration = $false
Assert-Equal (Register-EdgeHealthSchedule @parameters) $true 'registration succeeds'
Assert-Equal $script:Principal.LogonType 'Interactive' 'regular runner uses interactive logon'
Assert-Equal $script:Principal.RunLevel 'Limited' 'no elevation requested'
$parameters.ServiceAccount = $true
Assert-Equal (Register-EdgeHealthSchedule @parameters) $true 'service account registration'
Assert-Equal $script:Principal.LogonType 'ServiceAccount' 'integrated service logon preserved'
$parameters.ServiceAccount = $false
Assert-Equal (Register-EdgeHealthSchedule @parameters -UserLogonType S4U) $true 'explicit S4U remains available'
Assert-Equal $script:Principal.LogonType 'S4U' 'explicit logon selection'
$script:DenyRegistration = $true
$warnings = @()
$result = Register-EdgeHealthSchedule @parameters -WarningVariable warnings -WarningAction SilentlyContinue
Assert-Equal $result $false 'access denied is a reported failure, not a deployment exception'
if (-not ($warnings -match 'periodic monitoring is not confirmed')) { throw 'Registration failure must warn that monitoring is unavailable' }
# Immediate collection must precede registration, so access denied cannot skip it.
$collection = @($tree.EndBlock.Statements | Where-Object { $_.Extent.Text -match '^& \$collector ' })
$registration = @($tree.EndBlock.Statements | Where-Object { $_.Extent.Text -match '^\$registered = Register-EdgeHealthSchedule' })
if ($collection.Count -ne 1 -or $registration.Count -ne 1 -or $collection[0].Extent.StartOffset -ge $registration[0].Extent.StartOffset) { throw 'An immediate sample must be collected before registering the schedule' }
Write-Host 'PASS: interactive logon, service accounts, explicit S4U, access-denied handling and immediate collection order'
