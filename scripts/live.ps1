param(
    [string]$Config = 'configs/local.yaml',
    [double]$Capital,
    [switch]$Execute,
    [switch]$AllowLate
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root

$argsList = @('live-once', '--config', $Config)
if ($Capital -gt 0) { $argsList += @('--capital', [string]$Capital) }
if ($Execute) { $argsList += '--execute' }
if ($AllowLate) { $argsList += '--allow-late' }

& etf-rr @argsList
exit $LASTEXITCODE
