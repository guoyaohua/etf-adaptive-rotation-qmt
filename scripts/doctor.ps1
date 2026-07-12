param(
    [string]$Config = 'configs/local.yaml',
    [switch]$Connect
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root

$argsList = @('doctor', '--config', $Config)
if ($Connect) { $argsList += '--connect' }
& etf-rr @argsList
exit $LASTEXITCODE
