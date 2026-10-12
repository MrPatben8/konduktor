# Start both Konduktor servers (backend + frontend) together - the Windows
# counterpart of dev.sh. Ctrl-C stops both. First run? See one-time setup in
# BUILDING-WINDOWS.md (the backend venv and `npm install`).
#
#   .\dev.ps1
#   (if scripts are blocked: powershell -ExecutionPolicy Bypass -File .\dev.ps1)
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

$Py = Join-Path $Root "backend\.venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { throw "No backend venv at backend\.venv - see BUILDING-WINDOWS.md for setup." }
# Vite through node, not `npm run dev`: npm is a .cmd batch file, and Ctrl-C in a
# shared console makes cmd ask "Terminate batch job (Y/N)?" instead of exiting.
$Vite = Join-Path $Root "frontend\node_modules\vite\bin\vite.js"
if (-not (Test-Path $Vite)) { throw "No frontend\node_modules - run ``npm install`` in frontend first." }

# Backend (FastAPI on :8000)
$Backend = Start-Process -FilePath $Py -NoNewWindow -PassThru `
    -ArgumentList "-m", "uvicorn", "konduktor.main:app", "--reload", "--port", "8000", "--app-dir", "`"$Root\backend`""

# Frontend (Vite on :5173, proxies /api -> :8000)
$Frontend = Start-Process -FilePath "node" -NoNewWindow -PassThru `
    -WorkingDirectory (Join-Path $Root "frontend") -ArgumentList "`"$Vite`""

Write-Host ""
Write-Host "  Konduktor is starting..."
Write-Host "  -> App:      http://localhost:5173"
Write-Host "  -> API docs: http://localhost:8000/docs"
Write-Host "  (Ctrl-C to stop both)"
Write-Host ""

try {
    # If either one dies, stop the other rather than leave half a dev setup.
    while (-not $Backend.HasExited -and -not $Frontend.HasExited) { Start-Sleep -Milliseconds 500 }
}
finally {
    # Kill the whole tree: uvicorn --reload runs the server in a child process,
    # which would otherwise keep :8000 bound after the reloader has gone.
    foreach ($p in $Backend, $Frontend) {
        if ($p -and -not $p.HasExited) { & taskkill /PID $p.Id /T /F 2>&1 | Out-Null }
    }
}
