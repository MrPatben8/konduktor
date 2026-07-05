# Building Konduktor for Windows

The desktop app can't be cross-compiled — a Windows build must be produced **on
Windows** (Tauri needs the MSVC toolchain + WebView2; PyInstaller must freeze the
backend into a Windows `.exe`). The macOS `.dmg` is built separately on a Mac.

This mirrors the macOS build exactly; only the toolchain install and the
sidecar-build command differ.

## One-time setup

1. **Rust** — install via <https://rustup.rs> (gives the `x86_64-pc-windows-msvc`
   toolchain). You also need the **MSVC C++ build tools**: install "Desktop
   development with C++" from the Visual Studio Build Tools installer.
2. **WebView2 Runtime** — preinstalled on current Windows 10/11. If missing, get
   the Evergreen runtime from Microsoft (Tauri's installer can also bootstrap it).
3. **Python 3.11–3.14** — <https://python.org> (tick "Add to PATH").
4. **Node 18+** — <https://nodejs.org>.

Then, from the repo root:

```powershell
# Backend deps (runtime + build) in a venv
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-build.txt
cd ..

# Frontend deps
cd frontend
npm install
cd ..
```

## Build

```powershell
# 1) Freeze + stage the backend sidecar (from the activated backend venv)
cd backend
.\.venv\Scripts\Activate.ps1     # if not already active
.\build_sidecar.ps1              # → frontend\src-tauri\binaries\konduktor-sidecar-<triple>.exe
cd ..

# 2) Build the app
cd frontend
npx tauri build
```

## Output

Installers land in:

```
frontend\src-tauri\target\release\bundle\msi\Konduktor_0.1.0_x64_en-US.msi
frontend\src-tauri\target\release\bundle\nsis\Konduktor_0.1.0_x64-setup.exe
```

Either one installs the app (bundling the sidecar `.exe` alongside it).

## Rebuilding after changes

Same as macOS: if you changed **backend** Python, re-run `build_sidecar.ps1`
first; then `npx tauri build`. Frontend-only changes just need `npx tauri build`
(its `beforeBuildCommand` rebuilds the web app).

## Known Windows caveats

- **Unsigned build → SmartScreen.** We don't code-sign yet, so the first launch
  shows a "Windows protected your PC" prompt. Click **More info → Run anyway**.
  (Signing is a future step.)
- **Mixed content is already handled.** The webview is forced to the `http`
  custom-protocol scheme (`use_https_scheme(false)` in `src-tauri/src/lib.rs`) so
  it can fetch the sidecar at `http://127.0.0.1:<port>` without being blocked.
  Leave that setting as-is.
- **Sidecar console window.** The sidecar is frozen as a console app (needed for
  the `KONDUKTOR_PORT` stdout handshake). Tauri's shell plugin normally spawns it
  without a visible window; if a console flashes on launch, tell me and we'll add
  the `CREATE_NO_WINDOW` spawn flag.
- **The sidecar must be frozen on Windows** — a macOS-built sidecar won't run.
  `build_sidecar.ps1` handles this; just don't copy binaries between OSes.
