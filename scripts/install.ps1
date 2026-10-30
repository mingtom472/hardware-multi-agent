[CmdletBinding()]
param([switch]$PythonOnly, [switch]$WithNext)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $ProjectRoot
try {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw 'uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/ and reopen PowerShell.'
    }
    if (-not $env:UV_CACHE_DIR) { $env:UV_CACHE_DIR = Join-Path $ProjectRoot '.cache\uv' }
    & uv sync --frozen --python 3.12
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
    if (-not (Test-Path -LiteralPath '.env')) {
        Copy-Item -LiteralPath '.env.example' -Destination '.env'
        Write-Host 'Created .env from .env.example. Mock mode does not require any API key.'
    }
    if (-not $PythonOnly -or $WithNext) {
        if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw 'Next.js requires Node.js 22 or newer.' }
        & node -e 'if(Number(process.versions.node.split(".")[0])<22)process.exit(1)'
        if ($LASTEXITCODE -ne 0) { throw 'Next.js requires Node.js 22 or newer.' }
        if (-not $env:npm_config_cache) { $env:npm_config_cache = Join-Path $ProjectRoot '.cache\npm' }
        & npm.cmd ci --prefix frontend
        if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
    }
    Write-Host 'Installation complete. Run .\scripts\check.ps1 then .\scripts\start.ps1.'
    Write-Host 'API keys belong only in the local .env file or process environment. Never commit populated .env.'
}
finally { Pop-Location }
