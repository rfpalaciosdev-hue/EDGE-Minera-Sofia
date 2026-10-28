#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$OutputDirectory = (Join-Path $env:LOCALAPPDATA 'AinData\EdgeHealth'),
    [string]$TaskName = 'AinData-EdgeHealth-Minera-Sofia',
    [ValidateSet('Interactive', 'S4U')][string]$UserLogonType = 'Interactive'
)
$ErrorActionPreference = 'Stop'

function Register-EdgeHealthSchedule {
    [CmdletBinding()]
    param(
        [string]$TaskName, [string]$Collector, [string]$OutputDirectory,
        [string]$UserId, [bool]$ServiceAccount, [string]$PowerShellExe,
        [ValidateSet('Interactive', 'S4U')][string]$UserLogonType = 'Interactive'
    )
    $logonType = if ($ServiceAccount) { 'ServiceAccount' } else { $UserLogonType }
    try {
        $principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType $logonType -RunLevel Limited
        # Starting powershell.exe directly can flash a console before it hides itself.
        # The GUI script host creates it hidden from the outset; batch mode forbids dialogs.
        $launcher = Join-Path (Split-Path $Collector -Parent) 'Run-EdgeHealthHidden.vbs'
        $scriptHost = Join-Path $env:SystemRoot 'System32\wscript.exe'
        $arguments = '//B //NoLogo "{0}" "{1}" "{2}" "{3}"' -f $launcher, $PowerShellExe, $Collector, $OutputDirectory
        $action = New-ScheduledTaskAction -Execute $scriptHost -Argument $arguments -WorkingDirectory $OutputDirectory
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'AinData: Windows and local Docker health. No telemetry or database writes.' -Force -ErrorAction Stop | Out-Null
        return $true
    } catch {
        # Task Scheduler permissions/policies must not abort telemetry deployment.
        $code = $_.Exception.HResult.ToString('X8')
        Write-Warning "Periodic health task could not be registered (0x$code). The current snapshot was collected, but periodic monitoring is not confirmed. Check Task Scheduler permissions for $UserId."
        return $false
    }
}

if ($OutputDirectory.Contains('"')) { throw 'Invalid output directory' }
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
$collectorDirectory = Join-Path $OutputDirectory 'collector'
New-Item -ItemType Directory -Path $collectorDirectory -Force | Out-Null
$collector = Join-Path $collectorDirectory 'Collect-EdgeHealth.ps1'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Collect-EdgeHealth.ps1') -Destination $collector -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Run-EdgeHealthHidden.vbs') -Destination $collectorDirectory -Force
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$serviceAccount = $identity.User.Value -in @('S-1-5-18', 'S-1-5-19', 'S-1-5-20')
$powerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
# Always leave a current host sample, even if Task Scheduler denies registration.
& $collector -OutputDirectory $OutputDirectory
$registered = Register-EdgeHealthSchedule -TaskName $TaskName -Collector $collector -OutputDirectory $OutputDirectory -UserId $identity.Name -ServiceAccount $serviceAccount -PowerShellExe $powerShellExe -UserLogonType $UserLogonType
if ($registered) {
    Write-Host "OK: periodic health collector registered as $($identity.Name); output: $OutputDirectory"
} else {
    Write-Host '::warning::Periodic Windows health monitoring was not installed. A single sample is available; it will expire unless collection resumes.'
}
