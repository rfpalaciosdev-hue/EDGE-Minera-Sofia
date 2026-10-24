#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$OutputDirectory = (Join-Path $env:LOCALAPPDATA 'AinData\EdgeHealth'),
    [ValidateRange(60, 3600)][int]$StorageIntervalSeconds = 300
)
$ErrorActionPreference = 'Stop'
$ContainerNames = @('aim_edge_agent', 'aim_influxdb_edge', 'aim_mosquitto_edge')
$VolumeNames = @('aim_agent_state', 'aim_mosquitto_data', 'aim_mosquitto_logs')

function Utc-Stamp { [DateTimeOffset]::UtcNow.ToString('o') }

function Write-AtomicJson($Value, [string]$Path) {
    $temporary = "$Path.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Depth 16 -Compress), (New-Object Text.UTF8Encoding($false)))
        if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($temporary, $Path, [NullString]::Value) }
        else { [IO.File]::Move($temporary, $Path) }
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Quote-NativeArgument([string]$Value) {
    # Windows CommandLineToArgvW escaping, including embedded JSON quotes.
    '"' + [regex]::Replace([regex]::Replace($Value, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"'
}

function Invoke-Docker([string[]]$Arguments, [int]$TimeoutSeconds = 12) {
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $script:DockerExe
    $start.Arguments = (($Arguments | ForEach-Object { Quote-NativeArgument $_ }) -join ' ')
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $start
    try {
        [void]$process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $process.Kill()
            throw 'docker_timeout'
        }
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) { throw 'docker_command_failed' }
        $stdout.GetAwaiter().GetResult()
    } finally { $process.Dispose() }
}

function Convert-Size([string]$Value) {
    if ($Value -match '^\s*([0-9]+(?:\.[0-9]+)?)\s*(B|kB|KB|MB|GB|TB|KiB|MiB|GiB|TiB)\s*$') {
        $units = @{ B = 1; kB = 1000; MB = 1000000; GB = 1000000000; TB = 1000000000000; KiB = 1024; MiB = 1048576; GiB = 1073741824; TiB = 1099511627776 }
        return [long][Math]::Round([double]::Parse($Matches[1], [Globalization.CultureInfo]::InvariantCulture) * $units[$Matches[2]])
    }
    return $null
}

function Convert-Percent([string]$Value) {
    $parsed = 0.0
    if ([double]::TryParse($Value.Trim().TrimEnd('%'), [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$parsed) -and $parsed -ge 0 -and -not [double]::IsInfinity($parsed)) {
        return $parsed
    }
    return $null
}

function Read-HostHealth {
    $sample = [ordered]@{ collected_at = Utc-Stamp; status = 'ok'; cpu_percent = $null; uptime_seconds = $null; memory = $null; disks = @() }
    try {
        $cpu = @(Get-CimInstance Win32_Processor | Where-Object { $null -ne $_.LoadPercentage })
        if ($cpu.Count -eq 0) { throw 'cpu_unavailable' }
        $sample.cpu_percent = [Math]::Round(($cpu | Measure-Object LoadPercentage -Average).Average, 2)
    } catch { $sample.status = 'partial' }
    try {
        $os = Get-CimInstance Win32_OperatingSystem
        $total = [long]$os.TotalVisibleMemorySize * 1024
        $available = [long]$os.FreePhysicalMemory * 1024
        if ($total -le 0 -or $available -gt $total) { throw 'memory_unavailable' }
        $sample.memory = @{ total_bytes = $total; available_bytes = $available; used_bytes = $total - $available; used_percent = [Math]::Round(100 * (1 - $available / $total), 2) }
        $sample.uptime_seconds = [long]([DateTime]::Now - $os.LastBootUpTime).TotalSeconds
    } catch { $sample.status = 'partial' }
    try {
        $sample.disks = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object {
            if ($null -eq $_.Size -or $_.Size -le 0 -or $null -eq $_.FreeSpace) { throw 'disk_unavailable' }
            @{ drive = $_.DeviceID; total_bytes = [long]$_.Size; free_bytes = [long]$_.FreeSpace; used_bytes = [long]($_.Size - $_.FreeSpace); used_percent = [Math]::Round(100 * (1 - $_.FreeSpace / $_.Size), 2) }
        })
        if ($sample.disks.Count -eq 0) { $sample.status = 'partial' }
    } catch { $sample.status = 'partial' }
    return $sample
}

function Invoke-StorageProbe([string]$Image, [array]$Mounts, [array]$Spec) {
    if ($Spec.Count -eq 0) { return $null }
    $probeName = 'aim_edge_health_probe_' + [Guid]::NewGuid().ToString('N')
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject @($Spec) -Compress -Depth 5)))
    $arguments = @('run', '--rm', '--pull=never', '--name', $probeName, '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--memory', '64m', '--cpus', '0.25', '--pids-limit', '32', '--env', "EDGE_STORAGE_SPEC_B64=$encoded")
    foreach ($mount in $Mounts) { $arguments += @('--mount', $mount) }
    $arguments += @('--entrypoint', 'python', $Image, '/app/docker_storage_probe.py')
    try { return (Invoke-Docker -Arguments $arguments -TimeoutSeconds 35 | ConvertFrom-Json) }
    finally {
        # Only this invocation's temporary probe is eligible for cleanup.
        try { $null = Invoke-Docker -Arguments @('rm', '-f', $probeName) -TimeoutSeconds 5 } catch { }
    }
}

function Read-Storage {
    $sample = [ordered]@{ collected_at = Utc-Stamp; status = 'ok'; size_basis = 'logical_bytes'; containers = @(); volumes = @{}; engine = @{} }
    $inspections = @{}
    foreach ($name in $ContainerNames) {
        $row = @{ name = $name; writable_bytes = $null; log_bytes = $null }
        try {
            $inspection = @(Invoke-Docker -Arguments @('container', 'inspect', '--size', $name) | ConvertFrom-Json)[0]
            $inspections[$name] = $inspection
            if ($null -ne $inspection.SizeRw -and $inspection.SizeRw -ge 0) { $row.writable_bytes = [long]$inspection.SizeRw }
        } catch { $sample.status = 'partial' }
        $sample.containers += $row
    }
    $agent = $inspections['aim_edge_agent']
    $image = if ($null -ne $agent) { $agent.Image } else { $null }
    $volumeMounts = @(); $volumeSpec = @()
    foreach ($name in $VolumeNames) {
        $sample.volumes[$name] = $null
        try {
            # Inspect first; never create a missing volume just to measure it.
            $null = Invoke-Docker -Arguments @('volume', 'inspect', $name)
            $volumeMounts += "type=volume,source=$name,target=/probe/volumes/$name,readonly,volume-nocopy"
            $volumeSpec += @{ key = $name; path = "/probe/volumes/$name" }
        } catch { $sample.status = 'partial' }
    }
    if ($image) {
        try {
            $sizes = Invoke-StorageProbe -Image $image -Mounts $volumeMounts -Spec $volumeSpec
            foreach ($name in $VolumeNames) {
                if ($null -ne $sizes -and $null -ne $sizes.PSObject.Properties[$name]) { $sample.volumes[$name] = $sizes.$name }
            }
        } catch { $sample.status = 'partial' }

        # Log files are NOT included in SizeRw or docker system df.
        # Probe separately so Desktop restrictions on engine paths do not hide volumes.
        $logMounts = @(); $logSpec = @()
        foreach ($name in $ContainerNames) {
            $info = $inspections[$name]
            if ($null -ne $info -and $info.HostConfig.LogConfig.Type -eq 'json-file' -and $info.LogPath -match '^(/[^,\r\n]+)/([^/]+)$') {
                $directory = $Matches[1]; $filename = $Matches[2]
                $logMounts += "type=bind,source=$directory,target=/probe/logs/$name,readonly"
                $logSpec += @{ key = $name; path = "/probe/logs/$name"; pattern = "$filename*" }
            }
        }
        try {
            $sizes = Invoke-StorageProbe -Image $image -Mounts $logMounts -Spec $logSpec
            foreach ($row in $sample.containers) {
                if ($null -ne $sizes -and $null -ne $sizes.PSObject.Properties[$row.name]) { $row.log_bytes = $sizes.($row.name) }
            }
        } catch { $sample.status = 'partial' }
    }
    try {
        $summary = Invoke-Docker -Arguments @('system', 'df', '--format', '{{json .}}') -TimeoutSeconds 30
        foreach ($line in ($summary -split '\r?\n')) {
            if (-not $line.Trim()) { continue }
            $entry = $line | ConvertFrom-Json
            $key = switch ($entry.Type) { 'Images' { 'images_bytes_estimate' }; 'Containers' { 'containers_bytes_estimate' }; 'Local Volumes' { 'volumes_bytes_estimate' }; 'Build Cache' { 'build_cache_bytes_estimate' } }
            if ($key) { $sample.engine[$key] = Convert-Size $entry.Size }
        }
    } catch { $sample.status = 'partial' }
    foreach ($row in $sample.containers) {
        if ($null -eq $row.writable_bytes -or $null -eq $row.log_bytes) { $sample.status = 'partial' }
    }
    foreach ($name in $VolumeNames) { if ($null -eq $sample.volumes[$name]) { $sample.status = 'partial' } }
    if ($sample.engine.Count -lt 4) { $sample.status = 'partial' }
    return $sample
}

function Read-DockerHealth {
    $sample = [ordered]@{ collected_at = Utc-Stamp; status = 'ok'; containers = @() }
    $stats = @{}
    try {
        # Running stack containers only; a stopped/missing one cannot fail all stats.
        $running = @(Invoke-Docker -Arguments @('ps', '--format', '{{.Names}}')) -split '\r?\n' | Where-Object { $_ -in $ContainerNames }
        if ($running.Count -gt 0) {
            $raw = Invoke-Docker -Arguments (@('stats', '--no-stream', '--format', '{{json .}}') + @($running)) -TimeoutSeconds 15
            foreach ($line in ($raw -split '\r?\n')) {
                if ($line.Trim()) { $entry = $line | ConvertFrom-Json; $stats[$entry.Name] = $entry }
            }
        }
    } catch { $sample.status = 'partial' }
    foreach ($name in $ContainerNames) {
        $row = @{ name = $name; state = 'unknown'; health = 'unknown'; restart_count = $null; cpu_percent = $null; memory_used_bytes = $null; memory_limit_bytes = $null }
        try {
            # Only extract operational fields; never publish inspect's environment.
            $info = @(Invoke-Docker -Arguments @('container', 'inspect', $name) | ConvertFrom-Json)[0]
            $row.state = $info.State.Status
            $row.health = if ($info.State.Health) { $info.State.Health.Status } else { 'none' }
            $row.restart_count = [long]$info.RestartCount
        } catch { $sample.status = 'partial' }
        if ($stats.ContainsKey($name)) {
            $entry = $stats[$name]
            $row.cpu_percent = Convert-Percent $entry.CPUPerc
            $memory = $entry.MemUsage -split '\s*/\s*'
            if ($memory.Count -eq 2) {
                $row.memory_used_bytes = Convert-Size $memory[0]
                $row.memory_limit_bytes = Convert-Size $memory[1]
            }
        }
        if ($row.state -eq 'running' -and ($null -eq $row.cpu_percent -or $null -eq $row.memory_used_bytes)) { $sample.status = 'partial' }
        $sample.containers += $row
    }
    if (@($sample.containers | Where-Object { $_.state -ne 'unknown' }).Count -eq 0) { $sample.status = 'unavailable' }
    return $sample
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$lock = $null
try {
    # The file handle also serializes manual runs with Task Scheduler.
    try { $lock = [IO.File]::Open((Join-Path $OutputDirectory 'collector.lock'), 'OpenOrCreate', 'ReadWrite', 'None') }
    catch [IO.IOException] {
        if (($_.Exception.HResult -band 0xffff) -eq 32) { exit 0 }
        throw
    }
    $reportPath = Join-Path $OutputDirectory 'host-health.json'
    $cachePath = Join-Path $OutputDirectory 'storage-cache.json'
    $report = [ordered]@{ schema_version = 1; host = (Read-HostHealth); docker = @{ status = 'unavailable'; collected_at = Utc-Stamp; containers = @(); storage = $null } }
    $storage = $null
    try { $storage = Get-Content -LiteralPath $cachePath -Raw | ConvertFrom-Json } catch { }
    $report.docker.storage = $storage
    # Publish the host sample even if Docker is unavailable or a scan is slow.
    Write-AtomicJson $report $reportPath
    try {
        $script:DockerExe = (Get-Command docker.exe -ErrorAction Stop).Source
        # Refuse remote contexts: Windows metrics and Docker metrics must describe this PC.
        if ($env:DOCKER_HOST -and $env:DOCKER_HOST -notmatch '^npipe://') { throw 'remote_docker_context' }
        $context = @(Invoke-Docker -Arguments @('context', 'inspect') | ConvertFrom-Json)[0]
        if ($context.Endpoints.docker.Host -notmatch '^npipe://') { throw 'remote_docker_context' }
        $report.docker = Read-DockerHealth
        $report.docker.storage = $storage
        Write-AtomicJson $report $reportPath
        $refresh = $true
        try {
            $cacheAge = ([DateTimeOffset]::UtcNow - [DateTimeOffset]::Parse($storage.collected_at)).TotalSeconds
            $refresh = $cacheAge -ge $StorageIntervalSeconds -or $cacheAge -lt -30
        } catch { }
        if ($refresh) {
            $storage = Read-Storage
            Write-AtomicJson $storage $cachePath
            $report.docker.storage = $storage
            Write-AtomicJson $report $reportPath
        }
    } catch {
        # No exception text: CLI failures can contain paths or local configuration.
        $report.docker.status = 'unavailable'
        Write-AtomicJson $report $reportPath
    }
} finally {
    if ($null -ne $lock) { $lock.Dispose() }
}
