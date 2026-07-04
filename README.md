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
frontend). One-time setup, then a single command to launch.

### 1. Install (once)

**Backend** — needs Python 3.11+:

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd ..
```

**Frontend** — needs Node 18+:

```bash
cd frontend
npm install
cd ..
```

### 2. Launch

```bash
./dev.sh
```

Then open **http://localhost:5173** in your browser. Press `Ctrl-C` to stop.

### 3. Open your collection

On launch, Konduktor offers to:

- **Automatically** open the collection from your latest installed Traktor,
- reopen the **last collection** you used, or
- **browse** for a `collection.nml` yourself.

That's it — you're in.

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
- It's a web app today, with an eye toward a packaged desktop build (Tauri) and
  a mobile version down the road.
- Under the hood: **FastAPI** (Python) backend + **React / TypeScript / Vite**
  frontend. Curious about internals or contributing? See
  [CLAUDE.md](CLAUDE.md) for the architecture and conventions.
