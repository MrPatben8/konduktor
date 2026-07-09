# Konduktor

**A fast, modern home for your Traktor collection.**

Konduktor is a lightweight desktop-style app for DJs who run **Native
Instruments Traktor** (Pro 4 / NML v20). It opens your existing
`collection.nml`, gives you a beautiful dark library to explore, and lets you
build playlists and prep tracks — without launching the whole heavyweight
Traktor app just to tidy up your tags or drop a few cue points.

Your collection stays exactly where it is. Konduktor reads and writes the same
file Traktor uses, so everything you do shows up next time you open Traktor.

---

## Why you'll like it

- **Instant, searchable library.** Your whole collection loads into a snappy,
  virtualized grid. Search, filter by genre / key / BPM / rating, and sort by
  any column — all instant, no waiting.
- **Make it yours.** Show only the columns you care about, drag them into the
  order you like, and resize them to fit. Your layout is remembered.
- **Edit tags without the ceremony.** Double-click any field to fix a title or
  genre on the spot, click the stars to rate a track, or right-click for a full
  tag + cover-art editor. Great for quick one-off fixes.
- **Playlists, the easy way.** Create, rename, and delete playlists, then
  drag-and-drop to reorder or add tracks.
- **A real prep deck.** Load any track into the deck at the top and:
  - play it with a **frequency-colored waveform** (Traktor-style Spectrum look),
    scrolling under a fixed playhead with zoom in/out
  - scratch it by dragging the waveform
  - see and adjust the **beatgrid** and BPM
  - set, jump to, and delete **hotcues** (with a color-coded 8-slot bar)
  - build **loops** in fixed beat sizes, tied into the hotcue system
  - use keyboard shortcuts: **Space** to play/pause, **1–8** to fire hotcues,
    **Shift+1–8** to clear them
- **Your data is safe.** Every save makes a timestamped backup first, and only
  ever touches the parts you changed — the rest of the file stays byte-for-byte
  identical.

---

## Getting started

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

### 3. Open your collection

On launch, Konduktor offers to:

- **Automatically** open the collection from your latest installed Traktor,
- reopen the **last collection** you used, or
- **browse** for a `collection.nml` yourself.

That's it — you're in.

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
  [BUILDING-WINDOWS.md](BUILDING-WINDOWS.md) for the one-time toolchain setup.

The build is a two-step recipe on both platforms: **freeze the backend into a
sidecar binary**, then **build the app**. The only difference is which sidecar
script you run — `.sh` on macOS/Linux, `.ps1` on Windows. Each build runs on its
own OS (the app can't be cross-compiled).

### macOS / Linux

```bash
# 1) Freeze the backend into a sidecar binary (from the activated backend venv)
cd backend && source .venv/bin/activate && ./build_sidecar.sh && cd ..

# 2) Build the app
cd frontend && npx tauri build
```

The installer lands at
`frontend/src-tauri/target/release/bundle/dmg/Konduktor_<version>_aarch64.dmg`
(with the `.app` beside it).

### Windows

```powershell
# 1) Freeze the backend into a sidecar binary (from the activated backend venv)
cd backend; .\.venv\Scripts\Activate.ps1; .\build_sidecar.ps1; cd ..

# 2) Build the app
cd frontend; npx tauri build
```

The installers land in `frontend\src-tauri\target\release\bundle\` (an `.msi` and
an NSIS `-setup.exe`). See [BUILDING-WINDOWS.md](BUILDING-WINDOWS.md) for the full
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

## Saving back to Traktor

Playlist edits, tag changes, and prep tweaks all apply live in the app. When
you're happy, hit **Save to Traktor** to write them to `collection.nml`.

> ⚠️ **Close Traktor before saving.** Traktor rewrites `collection.nml` when it
> quits, so if it's open it will overwrite your changes. Konduktor always writes
> a backup first (into a `backups/` folder next to your collection), but closing
> Traktor first is the safe habit.

Want to experiment risk-free? Point Konduktor at a *copy* of your collection and
play around.

---

## Good to know

- Konduktor is built on [`traktor-nml-utils`](https://github.com/MrPatben8/traktor-nml-utils)
  and speaks Traktor's real file format — no import/export, no separate database.
- Run it in your browser from source, or package it into a native desktop app
  (macOS `.dmg` / Windows `.msi`) that bundles the backend — see
  [Build a standalone app](#build-a-standalone-app-production). A mobile version
  is on the horizon.
- Under the hood: **FastAPI** (Python) backend + **React / TypeScript / Vite**
  frontend. Curious about internals or contributing? See
  [CLAUDE.md](CLAUDE.md) for the architecture and conventions.
