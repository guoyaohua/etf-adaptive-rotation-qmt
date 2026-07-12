param(
    [string]$Config = 'configs/local.yaml',
    [double]$Capital,
    [switch]$Connect
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root

if (-not (Test-Path -LiteralPath $Config)) {
    throw "Local config not found: $Config. Copy configs/local.example.yaml to configs/local.yaml first."
}
if (-not $env:QMT_CLIENT_PATH) { throw 'QMT_CLIENT_PATH is not set.' }
if (-not $env:QMT_ACCOUNT_ID) { throw 'QMT_ACCOUNT_ID is not set.' }
if ($Capital -le 0) {
    $Capital = [double](Read-Host 'Strategy capital (CNY)')
}

Write-Host '[1/4] Initializing account-bound strategy ledger...'
$runtimeState = Join-Path $root 'runtime/state.json'
if (Test-Path -LiteralPath $runtimeState) {
    Write-Host 'Ledger already exists; preserving it.' -ForegroundColor Yellow
} else {
    etf-rr ledger-init --config $Config --capital $Capital
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$end = Get-Date -Format 'yyyyMMdd'
Write-Host '[2/4] Downloading daily history...'
etf-rr download --config $Config --start 20150101 --end $end
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host '[3/4] Running environment doctor...'
$doctorArgs = @('doctor', '--config', $Config)
if ($Connect) { $doctorArgs += '--connect' }
& etf-rr @doctorArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$now = Get-Date
$clock = $now.TimeOfDay
$weekday = [int]$now.DayOfWeek
$inSession = ($weekday -ge 1 -and $weekday -le 5) -and (
    ($clock -ge [TimeSpan]::Parse('09:30:00') -and $clock -le [TimeSpan]::Parse('11:30:00')) -or
    ($clock -ge [TimeSpan]::Parse('13:00:00') -and $clock -le [TimeSpan]::Parse('14:55:00'))
)
if ($inSession) {
    Write-Host '[4/4] Generating the first connected dry-run plan...'
    etf-rr live-once --config $Config --capital $Capital
} else {
    Write-Host '[4/4] Market is closed; generating an offline signal instead...'
    etf-rr signal --config $Config --output runtime/latest_signal.json
}
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ''
Write-Host 'Setup completed. No order was submitted.' -ForegroundColor Green
