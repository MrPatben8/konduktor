# Build the Konduktor desktop app on Windows: freeze + stage the backend sidecar,
# then build the Tauri installers. First run? See one-time setup in
# BUILDING-WINDOWS.md (the backend venv with requirements-build.txt, and
# `npm install`).
#
#   .\build.ps1                  # sidecar + app (release installers)
#   .\build.ps1 -SkipSidecar     # frontend-only change: reuse the staged sidecar
#   .\build.ps1 --debug          # anything else is passed on to `tauri build`
#   (if scripts are blocked: powershell -ExecutionPolicy Bypass -File .\build.ps1)
param([switch]$SkipSidecar)
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$TauriArgs = $args

$Py = Join-Path $Root "backend\.venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { throw "No backend venv at backend\.venv - see BUILDING-WINDOWS.md for setup." }
if (-not (Test-Path (Join-Path $Root "frontend\node_modules"))) { throw "No frontend\node_modules - run ``npm install`` in frontend first." }
$Staged = Join-Path $Root "frontend\src-tauri\binaries\konduktor-sidecar\konduktor-sidecar.exe"

# rustup puts cargo in %USERPROFILE%\.cargo\bin and adds it to PATH only for
# terminals opened AFTER the install - so find it there before giving up, rather
# than failing ten minutes in, after the sidecar build.
if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    $CargoBin = Join-Path $env:USERPROFILE ".cargo\bin"
    if (-not (Test-Path (Join-Path $CargoBin "cargo.exe"))) { throw "No Rust toolchain (cargo) - install it from https://rustup.rs, see BUILDING-WINDOWS.md." }
    $env:PATH = "$CargoBin;$env:PATH"
}

$Started = Get-Date
# build_sidecar.ps1 changes directory itself, and a location change outlives the
# script it was made in - so put ours back whatever happens.
Push-Location $Root
try {
    if ($SkipSidecar) {
        if (-not (Test-Path $Staged)) { throw "No staged sidecar to reuse - run without -SkipSidecar first." }
        Write-Host "==> Reusing the staged sidecar (-SkipSidecar)"
    }
    else {
        Write-Host "==> 1/2 Freezing the backend sidecar"
        & (Join-Path $Root "backend\build_sidecar.ps1")
    }

    Write-Host ""
    Write-Host "==> 2/2 Building the app (tauri build $TauriArgs)"
    Set-Location (Join-Path $Root "frontend")
    # npx is a .cmd and cargo reports progress on stderr: relax "Stop" for the
    # call (as build_sidecar.ps1 does for PyInstaller) and judge by exit code.
    $ErrorActionPreference = "Continue"
    & npx tauri build @TauriArgs
    $code = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($code -ne 0) { throw "tauri build failed ($code)" }
}
finally {
    Pop-Location
}

# List what this run produced (release or --debug profile).
$Bundles = Join-Path $Root "frontend\src-tauri\target\*\bundle"
$Built = Get-ChildItem -Path "$Bundles\nsis\*-setup.exe", "$Bundles\msi\*.msi" -File -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -ge $Started }
Write-Host ""
if ($Built) {
    Write-Host "Installers:"
    $Built | ForEach-Object { Write-Host "  $($_.FullName)" }
}
else {
    Write-Host "Done (no installer bundled - see frontend\src-tauri\target\)."
}
