#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$OutputDirectory = (Join-Path $env:LOCALAPPDATA 'AinData\EdgeHealth'),
    [string]$TaskName = 'AinData-EdgeHealth-Minera-Sofia'
)
$ErrorActionPreference = 'Stop'
if ($OutputDirectory.Contains('"')) { throw 'Invalid output directory' }
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
$collectorDirectory = Join-Path $OutputDirectory 'collector'
New-Item -ItemType Directory -Path $collectorDirectory -Force | Out-Null
$collector = Join-Path $collectorDirectory 'Collect-EdgeHealth.ps1'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Collect-EdgeHealth.ps1') -Destination $collector -Force
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$logonType = if ($identity.User.Value -in @('S-1-5-18', 'S-1-5-19', 'S-1-5-20')) { 'ServiceAccount' } else { 'S4U' }
$principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType $logonType -RunLevel Limited
$powerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -OutputDirectory "{1}"' -f $collector, $OutputDirectory
$action = New-ScheduledTaskAction -Execute $powerShellExe -Argument $arguments -WorkingDirectory $OutputDirectory
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'AinData: Windows and local Docker health. No telemetry or database writes.' -Force | Out-Null
& $collector -OutputDirectory $OutputDirectory
Write-Host "OK: health collector registered as $($identity.Name); output: $OutputDirectory"
