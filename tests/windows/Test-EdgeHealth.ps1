# No Pester or Windows dependencies: exercise collector functions with fixture commands.
# Run with Windows PowerShell 5.1 or pwsh: ./tests/windows/Test-EdgeHealth.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$collector = Join-Path $root 'scripts/windows/Collect-EdgeHealth.ps1'
foreach ($path in @($collector, (Join-Path $root 'scripts/windows/Install-EdgeHealthTask.ps1'))) {
    $tokens = $null; $parseErrors = $null
    $tree = [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
    if ($path -eq $collector) { $collectorTree = $tree }
}
foreach ($function in $collectorTree.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
    Invoke-Expression $function.Extent.Text
}
function Assert-Equal($Actual, $Expected, [string]$Label) {
    if ($Actual -cne $Expected) { throw "$Label - expected '$Expected', got '$Actual'" }
}
Assert-Equal (Convert-Size '1.5GiB') 1610612736 'binary memory units'
Assert-Equal (Convert-Size '1.5GB') 1500000000 'decimal disk units'
Assert-Equal (Convert-Size '0B') 0 'real zero'
Assert-Equal (Convert-Size '--') $null 'unavailable is not zero'
Assert-Equal (Convert-Percent '180.25%') 180.25 'multicore container CPU'
Assert-Equal (Convert-Percent '--') $null 'missing CPU'
Assert-Equal (Quote-NativeArgument 'a"b') '"a\"b"' 'embedded quote escaping'
Assert-Equal (Quote-NativeArgument 'C:\space here\') '"C:\space here\\"' 'trailing slash escaping'

function Get-CimInstance([string]$ClassName, [string]$Filter) {
    switch ($ClassName) {
        'Win32_Processor' { [pscustomobject]@{ LoadPercentage = 20 } }
        'Win32_OperatingSystem' { [pscustomobject]@{ TotalVisibleMemorySize = 1000; FreePhysicalMemory = 600; LastBootUpTime = [DateTime]::Now.AddHours(-1) } }
        'Win32_LogicalDisk' { [pscustomobject]@{ DeviceID = 'C:'; Size = 10000; FreeSpace = 7000 } }
    }
}
$hostSample = Read-HostHealth
Assert-Equal $hostSample.status 'ok' 'host status'
Assert-Equal $hostSample.memory.used_bytes 409600 'Windows KiB converted to bytes'
Assert-Equal $hostSample.disks[0].used_percent 30 'disk usage percent'

$ContainerNames = @('aim_edge_agent', 'aim_influxdb_edge', 'aim_mosquitto_edge')
$VolumeNames = @('aim_agent_state', 'aim_mosquitto_data', 'aim_mosquitto_logs')
$script:Calls = @()
function Invoke-Docker([string[]]$Arguments, [int]$TimeoutSeconds = 12) {
    $script:Calls += ,$Arguments
    if ($Arguments[0] -eq 'context') { return '[{"Endpoints":{"docker":{"Host":"npipe:////./pipe/docker_engine"}}}]' }
    if ($Arguments[0] -eq 'ps') { return 'aim_edge_agent' }
    if ($Arguments[0] -eq 'stats') { return '{"Name":"aim_edge_agent","CPUPerc":"180.25%","MemUsage":"1MiB / 1GiB"}' }
    if ($Arguments[0] -eq 'volume') { return '[{}]' }
    if ($Arguments[0] -eq 'system') { return @(
        '{"Type":"Images","Size":"1.5GB"}', '{"Type":"Containers","Size":"1kB"}',
        '{"Type":"Local Volumes","Size":"2GB"}', '{"Type":"Build Cache","Size":"0B"}'
    ) -join "`n" }
    if ($Arguments[0] -eq 'container') {
        return '[{"Image":"sha256:fixture","SizeRw":10,"RestartCount":2,"State":{"Status":"running","Health":{"Status":"healthy"}},"HostConfig":{"LogConfig":{"Type":"json-file"}},"LogPath":"/var/lib/docker/containers/id/id-json.log"}]'
    }
    throw 'unexpected command'
}
function Invoke-StorageProbe([string]$Image, [array]$Mounts, [array]$Spec) {
    foreach ($mount in $Mounts) {
        if ($mount -notmatch 'readonly') { throw 'probe mount must be readonly' }
        if ($mount -match 'influxdb_data|docker.sock') { throw 'probe must not access database or Docker socket' }
    }
    $result = @{}
    foreach ($item in $Spec) { $result[$item.key] = if ($item.path -match '/logs/') { 20 } else { 100 } }
    return [pscustomobject]$result
}
$dockerSample = Read-DockerHealth
Assert-Equal $dockerSample.containers.Count 3 'all expected containers'
Assert-Equal $dockerSample.containers[0].cpu_percent 180.25 'Docker CPU parsing'
Assert-Equal $dockerSample.containers[0].memory_used_bytes 1048576 'Docker RAM parsing'
Assert-Equal $dockerSample.containers[0].restart_count 2 'restart count'
Assert-Equal $dockerSample.containers[1].cpu_percent $null 'missing stats is not zero'
$storage = Read-Storage
Assert-Equal $storage.status 'ok' 'storage status'
Assert-Equal $storage.volumes.aim_agent_state 100 'volume bytes'
Assert-Equal $storage.containers[0].log_bytes 20 'log bytes'
Assert-Equal $storage.engine.images_bytes_estimate 1500000000 'engine summary'
if ($script:Calls | Where-Object { ($_ -join ' ') -match 'influxdb_data' }) { throw 'database volume must not be scanned' }
$dockerFixture = ${function:Invoke-Docker}
$probeFixture = ${function:Invoke-StorageProbe}
function Invoke-StorageProbe([string]$Image, [array]$Mounts, [array]$Spec) {
    if ($Spec | Where-Object { $_.path -match '/logs/' }) { throw 'engine log paths inaccessible' }
    $result = @{}
    foreach ($item in $Spec) { $result[$item.key] = 100 }
    return [pscustomobject]$result
}
$storage = Read-Storage
Assert-Equal $storage.status 'partial' 'restricted engine log paths'
Assert-Equal $storage.volumes.aim_agent_state 100 'known volumes survive log probe failure'
Assert-Equal $storage.containers[0].log_bytes $null 'inaccessible logs are unknown'
Set-Item Function:Invoke-StorageProbe $probeFixture

function Invoke-Docker([string[]]$Arguments, [int]$TimeoutSeconds = 12) { throw 'Docker unavailable' }
$dockerSample = Read-DockerHealth
Assert-Equal $dockerSample.status 'unavailable' 'unavailable Docker'
Assert-Equal $dockerSample.containers[0].restart_count $null 'unknown restart count'
$storage = Read-Storage
Assert-Equal $storage.status 'partial' 'unknown storage'
Assert-Equal $storage.containers[0].writable_bytes $null 'unknown writable bytes'

$temp = Join-Path ([IO.Path]::GetTempPath()) ('edge-health-test-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temp | Out-Null
try {
    $path = Join-Path $temp 'sample.json'
    Write-AtomicJson @{ sample = 1 } $path
    Write-AtomicJson @{ sample = 2 } $path
    Assert-Equal (Get-Content -LiteralPath $path -Raw | ConvertFrom-Json).sample 2 'atomic replacement'
    # Execute the actual top-level collection flow with fixture OS/Docker commands.
    Set-Item Function:Invoke-Docker $dockerFixture
    function Get-Command([string]$Name) { [pscustomobject]@{ Source = 'fixture-docker' } }
    $OutputDirectory = $temp
    $StorageIntervalSeconds = 300
    $main = ($collectorTree.EndBlock.Statements | Where-Object { $_ -isnot [Management.Automation.Language.FunctionDefinitionAst] } | ForEach-Object { $_.Extent.Text }) -join "`n"
    Invoke-Expression $main
    $sample = Get-Content -LiteralPath (Join-Path $temp 'host-health.json') -Raw | ConvertFrom-Json
    Assert-Equal $sample.host.status 'ok' 'full collector host status'
    Assert-Equal $sample.docker.status 'partial' 'full collector partial Docker stats'
    Assert-Equal $sample.docker.storage.status 'ok' 'full collector storage status'
    Assert-Equal $sample.docker.storage.volumes.aim_agent_state 100 'full collector storage bytes'
    $script:Calls = @()
    Invoke-Expression $main
    if ($script:Calls | Where-Object { $_ -contains '--size' }) { throw 'storage cache must prevent repeated scans' }
} finally { Remove-Item -LiteralPath $temp -Recurse -Force }
Write-Host 'PASS: parser, host metrics, Docker stats, storage isolation, failure handling, atomic file writes'
