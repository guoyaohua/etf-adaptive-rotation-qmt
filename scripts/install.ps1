param(
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root

Write-Host '[1/4] Checking Python...'
python --version
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host '[2/4] Installing project...'
python -m pip install -e '.[dev]'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host '[3/4] Running security checks...'
python scripts/security_check.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not $SkipTests) {
    Write-Host '[4/4] Running tests...'
    python -m pytest
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} else {
    Write-Host '[4/4] Tests skipped by request.'
}

Write-Host ''
Write-Host 'Installation completed.' -ForegroundColor Green
Write-Host 'Next: copy configs/local.example.yaml to configs/local.yaml, then run scripts/setup.ps1.'
