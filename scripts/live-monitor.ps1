param(
    [string]$Config = 'configs/local.yaml',
    [int]$Interval = 5,
    [switch]$Execute,
    [switch]$Once
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root

$argsList = @('live-monitor', '--config', $Config, '--interval', [string]$Interval)
if ($Execute) { $argsList += '--execute' }
if ($Once) { $argsList += '--once' }
& etf-rr @argsList
exit $LASTEXITCODE
