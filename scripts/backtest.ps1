param(
    [string]$Config = 'configs/local.yaml',
    [string]$Start = '20150101',
    [string]$End = (Get-Date -Format 'yyyyMMdd'),
    [string]$Output = 'reports/latest',
    [switch]$SkipDownload
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root

if (-not (Test-Path -LiteralPath $Config)) {
    throw "Config not found: $Config"
}
if (-not $SkipDownload) {
    etf-rr download --config $Config --start $Start --end $End
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
etf-rr backtest --config $Config --start $Start --end $End --output $Output
exit $LASTEXITCODE
