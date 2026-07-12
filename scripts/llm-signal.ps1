param(
    [string]$Config = 'configs/local.yaml',
    [string]$Output = 'runtime/latest_llm_signal.json',
    [switch]$Refresh
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root

$argsList = @('signal', '--config', $Config, '--with-llm', '--output', $Output)
if ($Refresh) { $argsList += '--refresh-llm' }
& etf-rr @argsList
exit $LASTEXITCODE
