# Building Konduktor

Most people should just [download a release](https://github.com/MrPatben8/konduktor/releases). This page is for running Konduktor from source or packaging the desktop app yourself.

## Run from source (development)

Konduktor runs as two small local servers (a Python backend and a web
frontend). Do the one-time setup, then start them — the same steps work on
macOS, Linux, and Windows.

### 1. Install (once)

**Backend** — needs Python 3.11+. Create a virtual environment and install the
dependencies:

```bash
cd backend
python3 -m venv .venv          # Windows: python -m venv .venv
```

Activate it, then install:

```bash
source .venv/bin/activate      # Windows (PowerShell): .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cd ..
```

**Frontend** — needs Node 18+ (same on every platform):

```bash
cd frontend
npm install
cd ..
```

### 2. Run it (development)

Konduktor runs as two local servers (backend on `:8000`, frontend on `:5173`).

**macOS / Linux** — one command starts both:

```bash
./dev.sh
```

**Windows** — start each server in its own terminal:

```powershell
# Terminal 1 — backend
cd backend; .\.venv\Scripts\Activate.ps1; uvicorn konduktor.main:app --reload --port 8000

# Terminal 2 — frontend
cd frontend; npm run dev
```

Either way, open **http://localhost:5173** in your browser (`Ctrl-C` stops a
server). This is the fastest way to try Konduktor or hack on it — the frontend
hot-reloads as you edit. To package a real double-clickable app instead, see
[Build a standalone app](#build-a-standalone-app-production) below.

### 3. Open a library

On launch, pick your platform (Traktor, Rekordbox, OneLibrary or Remote), then
let Konduktor find the library **automatically**, reopen the **last one** you
used, or **browse** for it yourself.

Testing writes? `./dev-sandbox.sh` starts both servers against a throwaway copy
of your Traktor collection, with prefs and version history in a temp folder.

---

## Build a standalone app (production)

Prefer a real double-clickable app over running the dev servers? Konduktor
packages into a native desktop app (via **Tauri**) that bundles the Python
backend as a sidecar — no terminal, no servers to start by hand.

### Extra prerequisites

On top of the Python 3.11+ and Node 18+ from above:

- **Rust** — install from <https://rustup.rs> (the app shell is Tauri/Rust).
- **PyInstaller** — into the backend venv:
  `pip install -r backend/requirements-build.txt`
- **Windows only:** also the MSVC C++ build tools and the WebView2 runtime — see
  [BUILDING-WINDOWS.md](../BUILDING-WINDOWS.md) for the one-time toolchain setup.

The build is a two-step recipe on both platforms: **freeze the backend into a
sidecar folder**, then **build the app**. The only difference is which sidecar
script you run — `.sh` on macOS/Linux, `.ps1` on Windows. Each build runs on its
own OS (the app can't be cross-compiled).

### macOS / Linux

```bash
# 1) Freeze the backend into a sidecar folder (from the activated backend venv)
cd backend && source .venv/bin/activate && ./build_sidecar.sh && cd ..

# 2) Build the app
cd frontend && npx tauri build
```

The installer lands at
`frontend/src-tauri/target/release/bundle/dmg/Konduktor_<version>_aarch64.dmg`
(with the `.app` beside it).

### Windows

```powershell
# 1) Freeze the backend into a sidecar folder (from the activated backend venv)
cd backend; .\.venv\Scripts\Activate.ps1; .\build_sidecar.ps1; cd ..

# 2) Build the app
cd frontend; npx tauri build
```

The installers land in `frontend\src-tauri\target\release\bundle\` (an `.msi` and
an NSIS `-setup.exe`). See [BUILDING-WINDOWS.md](../BUILDING-WINDOWS.md) for the full
walkthrough and Windows-specific notes.

> **First launch — getting past the OS security prompt.** Builds are
> **ad-hoc signed, not notarized** (no paid Apple/Windows developer cert yet), so
> the OS flags them the first time. This is expected — the app isn't damaged.
>
> - **macOS:** right-click the app → **Open** → **Open** (or System Settings →
>   Privacy & Security → **Open Anyway** after the first blocked attempt). If it
>   still refuses — common for a `.dmg` downloaded via a browser, which macOS
>   quarantines — clear the quarantine flag once from Terminal:
>   ```bash
>   xattr -cr /Applications/Konduktor.app
>   ```
> - **Windows:** click **More info → Run anyway** past SmartScreen.
>
> The macOS `.dmg` is Apple-Silicon (`aarch64`) only.

> **Faster local builds:** `npx tauri build --debug --bundles app` skips the
> optimizer and the installer, giving you a runnable app in seconds — handy while
> iterating.

### Rebuilding after changes

Changed **backend** Python? Re-run the sidecar script for your OS
(`./build_sidecar.sh` or `.\build_sidecar.ps1`), then `npx tauri build`.
Frontend-only change? Just `npx tauri build` — it rebuilds the web app for you.

---

Curious about the internals or want to contribute? [CLAUDE.md](../CLAUDE.md) describes the architecture and conventions.
