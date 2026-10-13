# Build the frozen backend sidecar on Windows (a PyInstaller onedir folder) and
# stage it for Tauri as frontend\src-tauri\binaries\konduktor-sidecar\, which
# tauri.conf.json bundles as a resource folder (`sidecar\` in the app).
# Output: backend\dist\konduktor-sidecar\  +  frontend\src-tauri\binaries\konduktor-sidecar\
#
# Run from a PowerShell prompt with the backend venv's Python on PATH, e.g.:
#   cd backend
#   .\.venv\Scripts\Activate.ps1
#   pip install -r requirements.txt -r requirements-build.txt
#   .\build_sidecar.ps1
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

# Prefer the venv python if present, else whatever `python` resolves to.
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }

# PyInstaller logs to stderr. Windows PowerShell 5.1 (unlike pwsh 7) turns a
# native command's stderr into an error record, which "Stop" makes fatal on the
# first INFO line — so relax it for this call and judge by the exit code.
$ErrorActionPreference = "Continue"
& $Py -m PyInstaller --noconfirm --clean konduktor-sidecar.spec 2>&1 | ForEach-Object { "$_" }
$ErrorActionPreference = "Stop"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }
Write-Host "Built: $Root\dist\konduktor-sidecar\"

# Stage for Tauri, replacing the whole folder so no file from an older build survives.
$Dest = Join-Path $Root "..\frontend\src-tauri\binaries"
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
$Staged = Join-Path $Dest "konduktor-sidecar"
if (Test-Path $Staged) { Remove-Item -Recurse -Force $Staged }
Copy-Item -Recurse "$Root\dist\konduktor-sidecar" $Staged
Write-Host "Staged: $Staged\"
