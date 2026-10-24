$ErrorActionPreference = 'Stop'
$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$tokens = $null; $issues = $null
$tree = [Management.Automation.Language.Parser]::ParseFile((Join-Path $root 'scripts/diagnostics/Read-EdgeDiagnostics.ps1'), [ref]$tokens, [ref]$issues)
if ($issues.Count) { throw ($issues | Out-String) }
$helper = $tree.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-ForwardingErrorSummary' }, $false)
Invoke-Expression $helper.Extent.Text
$fixture = @'
Detalle: Read timed out. PRIVATE
Detalle: Invalid header value PRIVATE
Detalle: Out of range float values are not JSON compliant PRIVATE
Detalle: NameResolutionError PRIVATE
Detalle: SSLError CERTIFICATE_VERIFY_FAILED PRIVATE
Error 403: PRIVATE
Error 503: PRIVATE
sensor payload PRIVATE
Detalle: unknown PRIVATE
'@
$summary = Get-ForwardingErrorSummary $fixture
foreach ($kind in @('timeout', 'invalid_header', 'invalid_json', 'dns', 'tls', 'http_403', 'http_503', 'unclassified')) {
    if ($summary.forwarding_error_summary.counts[$kind] -ne 1) { throw "Incorrect classification: $kind" }
}
$json = $summary | ConvertTo-Json -Compress -Depth 5
if ($json -match 'PRIVATE|Detalle|payload|Read timed out') { throw 'Summary leaked raw log content' }
if ($summary.forwarding_error_summary.last_kind -ne 'unclassified') { throw 'Incorrect last error category' }
Write-Host 'PASS: error categories only; no raw logs, identifiers or credentials in the summary'
