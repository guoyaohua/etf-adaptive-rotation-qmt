param(
    [string]$Config = 'configs/local.yaml'
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root
etf-rr reconcile --config $Config
exit $LASTEXITCODE
