#Requires -Version 5.1
# Only fixed fields are exported. No logs, environment values or raw Docker output.
[CmdletBinding()]
param([string]$ExpectedSite, [string]$ExpectedTenant)
$ErrorActionPreference = 'Stop'
$script:DockerExecutable = (Get-Command docker.exe -ErrorAction Stop).Source

function Read-DockerOutput {
    param([string[]]$Arguments, [string]$InputText)
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        if ($PSBoundParameters.ContainsKey('InputText')) {
            $lines = @($InputText | & $script:DockerExecutable @Arguments 2>&1 | ForEach-Object { $_.ToString() })
        } else {
            $lines = @(& $script:DockerExecutable @Arguments 2>&1 | ForEach-Object { $_.ToString() })
        }
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previous }
    if ($code -ne 0) { throw 'Docker diagnostic command failed' }
    return ($lines -join "`n")
}

function Get-ForwardingErrorSummary([string]$Logs) {
    # Classify locally. Never return a line, substring, URL or exception message.
    $counts = @{}
    $lastKind = 'none'
    foreach ($line in ($Logs -split '\r?\n')) {
        if ($line -notmatch 'Detalle:|Error enviando reconex|Error [45][0-9][0-9]') { continue }
        $kind = if ($line -match 'Out of range float|not JSON compliant|not JSON serializable') { 'invalid_json' }
            elseif ($line -match 'InvalidHeader|header value|leading whitespace') { 'invalid_header' }
            elseif ($line -match 'timed out|ReadTimeout|ConnectTimeout|TimeoutError') { 'timeout' }
            elseif ($line -match 'NameResolution|getaddrinfo|name or service not known') { 'dns' }
            elseif ($line -match 'SSLError|SSL:|CERTIFICATE_VERIFY|certificate verify') { 'tls' }
            elseif ($line -match 'No connection adapters|Invalid URL|No scheme supplied') { 'invalid_url' }
            elseif ($line -match 'Error ([45][0-9][0-9])') { 'http_' + $Matches[1] }
            elseif ($line -match 'ConnectionError|Connection aborted|Connection reset|RemoteDisconnected|Max retries') { 'connection' }
            else { 'unclassified' }
        if (-not $counts.ContainsKey($kind)) { $counts[$kind] = 0 }
        $counts[$kind] += 1
        $lastKind = $kind
    }
    return @{ forwarding_error_summary = @{ counts = $counts; last_kind = $lastKind } }
}

foreach ($name in @('aim_edge_agent', 'aim_influxdb_edge', 'aim_mosquitto_edge')) {
    try {
        $state = Read-DockerOutput -Arguments @('inspect', '--format', '{{json .State}}', $name) | ConvertFrom-Json
        $status = if ($state.Status -in @('running', 'created', 'paused', 'restarting', 'exited', 'dead')) { $state.Status } else { 'unknown' }
        $health = if ($state.Health.Status -in @('healthy', 'unhealthy', 'starting')) { $state.Health.Status } else { 'unknown' }
        @{ service = $name; status = $status; health = $health } | ConvertTo-Json -Compress | Write-Host
    } catch { Write-Host 'Container state unavailable' }
}
try {
    $localLogs = Read-DockerOutput -Arguments @('logs', '--since', '30m', '--tail', '1000', 'aim_edge_agent')
    Get-ForwardingErrorSummary $localLogs | ConvertTo-Json -Compress -Depth 5 | Write-Host
    $localLogs = $null
} catch { Write-Host 'Error classification unavailable' }
try {
    $probe = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'edge_connection.py') -Raw
    $arguments = @('exec', '-i', 'aim_edge_agent', 'python', '-', '--host-utc', [DateTimeOffset]::UtcNow.ToString('o'))
    if ($ExpectedSite) { $arguments += @('--expected-site', $ExpectedSite) }
    if ($ExpectedTenant) { $arguments += @('--expected-tenant', $ExpectedTenant) }
    $summary = Read-DockerOutput -Arguments $arguments -InputText $probe | ConvertFrom-Json
    $summary | ConvertTo-Json -Compress -Depth 5 | Write-Host
} catch { Write-Host 'Connection summary unavailable' }
