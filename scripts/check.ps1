[CmdletBinding()]
param([switch]$PythonOnly, [switch]$WithNext)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { throw 'Run .\scripts\install.ps1 before checks.' }
Push-Location -LiteralPath $ProjectRoot
try {
    & uv lock --check
    if ($LASTEXITCODE -ne 0) { throw 'The dependency lock does not match pyproject.toml.' }
    & $Python -m pytest backend/tests
    if ($LASTEXITCODE -ne 0) { throw 'Python tests failed.' }
    & $Python -m ruff check backend ui
    if ($LASTEXITCODE -ne 0) { throw 'Python lint failed.' }
    if (-not $PythonOnly -or $WithNext) {
        $env:NEXT_TELEMETRY_DISABLED = '1'
        & npm.cmd --prefix frontend run typecheck
        if ($LASTEXITCODE -ne 0) { throw 'Frontend typecheck failed.' }
        & npm.cmd --prefix frontend run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    }
    Write-Host 'Checks passed. Windows service startup still requires an actual local start.ps1 smoke test.'
}
finally { Pop-Location }
