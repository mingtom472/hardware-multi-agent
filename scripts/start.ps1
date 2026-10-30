[CmdletBinding()]
param(
    [ValidateRange(1, 65535)][int]$ApiPort = 8000,
    [ValidateRange(1, 65535)][int]$UiPort = 8501,
    [switch]$WithNext,
    [switch]$Studio,
    [ValidateRange(1, 65535)][int]$NextPort = 3000
)

$ErrorActionPreference = 'Stop'
$UseNext = (-not $Studio) -or $WithNext
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$OwnedProcesses = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
if (-not (Test-Path -LiteralPath $Python)) { throw 'Python environment missing. Run .\scripts\install.ps1 first.' }
if ($ApiPort -lt 1 -or $ApiPort -gt 65535 -or $UiPort -lt 1 -or $UiPort -gt 65535) { throw 'Ports must be between 1 and 65535.' }
if (($Studio -and $ApiPort -eq $UiPort) -or ($UseNext -and ($NextPort -eq $ApiPort -or ($Studio -and $NextPort -eq $UiPort)))) { throw 'Each service must use a different port.' }

function Quote-Argument([string]$Value) { return '"' + $Value + '"' }
function Stop-OwnedProcessTree([System.Diagnostics.Process]$Process) {
    if (-not $Process.HasExited) {
        # /T only targets the process tree started by this script, never another user's service.
        try { & taskkill.exe /PID $Process.Id /T /F 2>$null | Out-Null } catch { Write-Warning "Could not stop PID $($Process.Id). Close its local service terminal if it remains running." }
    }
}

Push-Location -LiteralPath $ProjectRoot
try {
    $env:HW_API_URL = "http://127.0.0.1:$ApiPort"
    $env:HW_BROWSER_API_URL = $env:HW_API_URL
    $env:HW_CORS_ORIGINS = '["http://127.0.0.1:' + $UiPort + '","http://localhost:' + $UiPort + '","http://127.0.0.1:' + $NextPort + '","http://localhost:' + $NextPort + '"]'
    $env:STREAMLIT_BROWSER_GATHER_USAGE_STATS = 'false'
    $ApiArgs = @('-m', 'uvicorn', 'app.main:app', '--app-dir', (Quote-Argument (Join-Path $ProjectRoot 'backend')), '--host', '127.0.0.1', '--port', "$ApiPort")
    $ApiProcess = Start-Process -FilePath $Python -ArgumentList $ApiArgs -WorkingDirectory $ProjectRoot -NoNewWindow -PassThru
    $OwnedProcesses.Add($ApiProcess)
    $Ready = $false
    for ($Attempt = 0; $Attempt -lt 60; $Attempt++) {
        if ($ApiProcess.HasExited) { throw 'FastAPI exited during startup. Read the startup error above.' }
        try {
            $Status = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/health" -TimeoutSec 1
            if ($Status.status -eq 'ok') { $Ready = $true; break }
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $Ready) { throw 'FastAPI did not become ready within the startup window.' }
    if ($Studio) {
    $UiArgs = @('-m', 'streamlit', 'run', (Quote-Argument (Join-Path $ProjectRoot 'ui\streamlit_app.py')), '--server.address', '127.0.0.1', '--server.port', "$UiPort", '--server.headless', 'true', '--browser.gatherUsageStats', 'false')
    $UiProcess = Start-Process -FilePath $Python -ArgumentList $UiArgs -WorkingDirectory $ProjectRoot -NoNewWindow -PassThru
    $OwnedProcesses.Add($UiProcess)
    Write-Host "Optional Streamlit studio: http://127.0.0.1:$UiPort"
    }
    if ($UseNext) {
        $env:BACKEND_URL = $env:HW_API_URL
        $env:NEXT_TELEMETRY_DISABLED = '1'
        $NextProcess = Start-Process -FilePath 'npm.cmd' -ArgumentList @('run', 'dev', '--', '--port', "$NextPort") -WorkingDirectory (Join-Path $ProjectRoot 'frontend') -NoNewWindow -PassThru
        $OwnedProcesses.Add($NextProcess)
        Write-Host "Next.js workbench: http://127.0.0.1:$NextPort"
    }
    Write-Host "FastAPI: http://127.0.0.1:$ApiPort/docs"
    Write-Host 'Press Ctrl+C in this terminal to stop only the services started here.'
    while ($true) {
        foreach ($Process in $OwnedProcesses) {
            if ($Process.HasExited) { throw "A service exited unexpectedly (PID $($Process.Id)). Read the error above." }
        }
        Start-Sleep -Milliseconds 500
    }
}
finally {
    foreach ($Process in $OwnedProcesses) { Stop-OwnedProcessTree $Process }
    Pop-Location
}
