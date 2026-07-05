# Build the frozen backend sidecar on Windows (PyInstaller) and stage it for
# Tauri as binaries\konduktor-sidecar-<target-triple>.exe.
# Output: backend\dist\konduktor-sidecar.exe  +  frontend\src-tauri\binaries\...
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

& $Py -m PyInstaller --noconfirm --clean konduktor-sidecar.spec
Write-Host "Built: $Root\dist\konduktor-sidecar.exe"

# Stage for Tauri: name with the host target triple + .exe so the shell plugin
# resolves it. (rustc must be on PATH.)
$Triple = (& rustc -vV | Select-String '^host: ').ToString().Replace('host: ', '').Trim()
$Dest = Join-Path $Root "..\frontend\src-tauri\binaries"
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
Copy-Item "$Root\dist\konduktor-sidecar.exe" (Join-Path $Dest "konduktor-sidecar-$Triple.exe") -Force
Write-Host "Staged: $Dest\konduktor-sidecar-$Triple.exe"
