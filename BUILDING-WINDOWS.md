# Building Konduktor for Windows

The desktop app can't be cross-compiled. A Windows build must be produced **on
Windows**: Tauri needs the MSVC toolchain and WebView2, and PyInstaller must
freeze the backend into a Windows `.exe`. The macOS `.dmg` is built separately
on a Mac (see [docs/BUILDING.md](docs/BUILDING.md)).

The recipe is the same as on macOS. Only the toolchain install and the
sidecar-build command differ. CI does exactly this on `windows-latest`
([.github/workflows/build.yml](.github/workflows/build.yml)), so if a local build
disagrees with CI, check that workflow first.

## One-time setup

1. **Rust**: install via <https://rustup.rs> (gives the `x86_64-pc-windows-msvc`
   toolchain). You also need the **MSVC C++ build tools**: install "Desktop
   development with C++" from the Visual Studio Build Tools installer.
2. **WebView2 Runtime**: preinstalled on current Windows 10/11. If it's missing,
   get the Evergreen runtime from Microsoft (Tauri's installer can also
   bootstrap it).
3. **Python 3.11–3.14**: <https://python.org> (tick "Add to PATH"). CI builds
   with **3.12**, the safest choice.
4. **Node 20.19+ or 22.12+**: <https://nodejs.org>. Vite 8 refuses older
   versions, so Node 18 no longer works. CI uses Node 20.

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

If PowerShell refuses to run `Activate.ps1` or `dev.ps1` ("running scripts is
disabled on this system"), either allow local scripts once with
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or run a script with
`powershell -ExecutionPolicy Bypass -File .\dev.ps1`.

## Develop (no build)

```powershell
.\dev.ps1     # backend on :8000 + Vite on :5173, both reloading; Ctrl-C stops both
```

Then open <http://localhost:5173> in a browser. `dev.ps1` is the Windows
counterpart of `dev.sh`. It needs the `backend\.venv` and `frontend\node_modules`
from the setup above. It runs the Python source, not the frozen sidecar, so it
can't show desktop-shell behaviour (startup screen, quit prompt, WebView2, file
drops from Explorer). For that, run `npx tauri dev` in `frontend`. Note that this
still spawns the **frozen** sidecar staged in `src-tauri\binaries\`, so backend
changes need `build_sidecar.ps1` first.

### Tests

`backend/run_tests.sh` expects a POSIX venv (`.venv/bin/activate`), so it won't
run as-is on Windows. Run a suite directly with the venv's Python instead:

```powershell
cd backend
.\.venv\Scripts\python.exe test_save_fidelity.py
```

## Build

One command does both steps and lists the installers it produced:

```powershell
.\build.ps1                  # sidecar + app
.\build.ps1 -SkipSidecar     # frontend-only change: reuse the staged sidecar
.\build.ps1 --debug          # any other argument is passed on to `tauri build`
```

By hand, that is:

```powershell
# 1) Freeze + stage the backend sidecar
cd backend
.\build_sidecar.ps1              # → frontend\src-tauri\binaries\konduktor-sidecar\ (a folder)
cd ..

# 2) Build the app (its beforeBuildCommand runs the Vite build)
cd frontend
npx tauri build --config src-tauri/tauri.unsigned.conf.json
```

The `--config` override skips the in-app updater packages, which are signed
with the release key that only CI holds (`TAURI_SIGNING_PRIVATE_KEY`). With
that key set in the environment, plain `npx tauri build` makes them too.
`build.ps1` picks the right one by itself.

`build_sidecar.ps1` uses `backend\.venv\Scripts\python.exe` when it exists (else
whatever `python` resolves to), so the venv doesn't need to be activated. It
replaces the whole staged folder, so no file from an older build survives.

The sidecar is a PyInstaller **onedir** folder (`konduktor-sidecar.exe` plus
`_internal\`), not a single `.exe`. Don't switch it back to onefile; see
gotcha 4 in [CLAUDE.md](CLAUDE.md).

## Output

Installers land in:

```
frontend\src-tauri\target\release\bundle\nsis\Konduktor_<version>_x64-setup.exe
frontend\src-tauri\target\release\bundle\msi\Konduktor_<version>_x64_en-US.msi
```

`<version>` is the `version` in [frontend/package.json](frontend/package.json),
the app's only version source. Either installer works. Releases ship only the
NSIS `-setup.exe`. Both put the sidecar folder in the app's `sidecar\` resource
folder (`tauri.windows.conf.json` → `bundle.resources`), where `lib.rs` spawns
`sidecar\konduktor-sidecar.exe`.

## Rebuilding after changes

If you changed **backend** Python, run `.\build.ps1` (or `build_sidecar.ps1`,
then `npx tauri build` as above). Frontend-only changes just need
`.\build.ps1 -SkipSidecar`.

## Not part of this build: the stem engine

Convert to Stems uses a separate program, `engine/` (torch + demucs), built and
released on its own channel by
[.github/workflows/engine.yml](.github/workflows/engine.yml) (`windows-x64-cpu`
and `windows-x64-cuda`). The app downloads it at runtime into a short
`%LOCALAPPDATA%\Konduktor\engine` path to stay under Windows' 260-character path
limit. You don't need it to build or run the app.

## Known Windows caveats

- **Unsigned build → SmartScreen.** Builds aren't code-signed yet, so the first
  launch shows a "Windows protected your PC" prompt. Click **More info → Run
  anyway**.
- **Mixed content is already handled.** The webview is forced to the `http`
  custom-protocol scheme (`use_https_scheme(false)` in `src-tauri/src/lib.rs`) so
  it can fetch the sidecar at `http://127.0.0.1:<port>` without being blocked.
  Leave that setting as-is.
- **The sidecar is a console app, but no console appears.** It's frozen with
  `console=True` because the shell reads the `KONDUKTOR_PORT=<n>` line from its
  stdout. Tauri's shell plugin spawns it with `CREATE_NO_WINDOW`, so no window
  flashes.
- **The sidecar doesn't watch stdin on Windows.** On macOS it exits when its
  stdin closes. On Windows a pending stdin read stalled loading scipy's OpenBLAS
  DLL, which hung the first analysis, so it waits on the parent process instead
  (`backend/sidecar.py`). Keep this in mind before touching that watcher.
- **Decode audio through `waveform.load`, never `librosa.load`.** librosa reads
  AAC only through CoreAudio or a system ffmpeg, so on Windows every `.m4a` /
  `.stem.m4a` failed with "Format not recognised". PyAV bundles FFmpeg.
- **The sidecar must be frozen on Windows.** A macOS-built sidecar won't run.
  `build_sidecar.ps1` handles this; just don't copy binaries between OSes.
