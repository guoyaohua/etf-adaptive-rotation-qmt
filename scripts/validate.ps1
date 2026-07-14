param(
    [string]$Config = 'configs/strategy.yaml',
    [string]$Start = '20150101',
    [string]$End = (Get-Date -Format 'yyyyMMdd'),
    [string]$Output = 'reports/validation-latest'
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root

python scripts/validate_strategy.py `
    --config $Config `
    --start $Start `
    --end $End `
    --output $Output
exit $LASTEXITCODE
