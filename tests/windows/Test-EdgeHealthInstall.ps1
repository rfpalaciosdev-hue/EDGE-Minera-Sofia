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
function New-ScheduledTaskAction($Execute, $Argument, $WorkingDirectory) {
    $script:Action = @{ Execute = $Execute; Argument = $Argument; WorkingDirectory = $WorkingDirectory }
    return $script:Action
}
function New-ScheduledTaskTrigger([switch]$Once, $At, $RepetitionInterval) {
    $script:RepetitionInterval = $RepetitionInterval
    return @{ At = $At }
}
function New-ScheduledTaskSettingsSet($MultipleInstances, $ExecutionTimeLimit, [switch]$StartWhenAvailable, [switch]$AllowStartIfOnBatteries, [switch]$DontStopIfGoingOnBatteries) { return @{} }
function Register-ScheduledTask {
    [CmdletBinding()]
    param($TaskName, $Action, $Trigger, $Principal, $Settings, $Description, [switch]$Force)
    if ($script:DenyRegistration) { throw [UnauthorizedAccessException]::new('Access denied') }
    return @{ TaskName = $TaskName }
}
$parameters = @{ TaskName = 'fixture'; Collector = 'C:\Health\Collect.ps1'; OutputDirectory = 'C:\Health'; UserId = 'PC\Usuario'; ServiceAccount = $false; PowerShellExe = 'powershell.exe' }
if (-not $env:SystemRoot) { $env:SystemRoot = [IO.Path]::GetTempPath() }
$script:DenyRegistration = $false
Assert-Equal (Register-EdgeHealthSchedule @parameters) $true 'registration succeeds'
if ($script:Action.Execute -notmatch '[\\/]wscript\.exe$') { throw 'GUI host must avoid console creation' }
if ($script:Action.Argument -notmatch '^//B //NoLogo "[^"\r\n]*Run-EdgeHealthHidden.vbs" ') { throw 'Periodic collection must use the launcher in batch mode without dialogs' }
Assert-Equal $script:RepetitionInterval.TotalMinutes 5 'collection repeats every five minutes'
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
# On Windows, exercise the real GUI launcher, including paths with spaces and an
# accented character, waiting for completion and preserving a failing exit code.
if ($env:OS -eq 'Windows_NT') {
    $fixtureDirectory = Join-Path ([IO.Path]::GetTempPath()) ('Edge Health ' + [char]0xF1 + ' ' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $fixtureDirectory -Force | Out-Null
    try {
        $fixture = Join-Path $fixtureDirectory 'collector fixture.ps1'
        Set-Content -LiteralPath $fixture -Encoding UTF8 -Value 'param([string]$OutputDirectory); [IO.File]::WriteAllText((Join-Path $OutputDirectory "completed.txt"), "done"); exit 7'
        $launcher = Join-Path $root 'scripts/windows/Run-EdgeHealthHidden.vbs'
        $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $start = New-Object Diagnostics.ProcessStartInfo
        $start.FileName = Join-Path $env:SystemRoot 'System32\wscript.exe'
        $start.Arguments = '//B //NoLogo "{0}" "{1}" "{2}" "{3}"' -f $launcher, $powershell, $fixture, $fixtureDirectory
        $start.UseShellExecute = $false
        $start.CreateNoWindow = $true
        $process = [Diagnostics.Process]::Start($start)
        try {
            if (-not $process.WaitForExit(30000)) { $process.Kill(); throw 'Hidden launcher timed out' }
            Assert-Equal $process.ExitCode 7 'launcher preserves collector failure'
            Assert-Equal ([IO.File]::ReadAllText((Join-Path $fixtureDirectory 'completed.txt'))) 'done' 'launcher waits for collection with quoted paths'
        } finally { $process.Dispose() }
    } finally { Remove-Item -LiteralPath $fixtureDirectory -Recurse -Force }
}
Write-Host 'PASS: interactive logon, service accounts, explicit S4U, access-denied handling and immediate collection order'
