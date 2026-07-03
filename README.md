# Konduktor

A library-management and track-preparation tool for **Native Instruments
Traktor** (Pro 4 / NML v20), built on top of
[`traktor-nml-utils`](https://github.com/MrPatben8/traktor-nml-utils).

- **backend/** — FastAPI service that parses `collection.nml` and serves it as a
  query API (tracks, stats, facets, playlists). Read-only in the current phase.
- **frontend/** — React + TypeScript (Vite) track explorer: a polished, dark,
  virtualized grid with search, filtering, sorting, and playlist browsing.

## Quick start

After the one-time setup below, start everything with a single command:

```bash
./dev.sh
```

Then open **http://localhost:5173**. Press `Ctrl-C` to stop both servers.

## One-time setup

You only need to do this once (installs backend and frontend dependencies).

**Backend** (Python 3.11+):

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd ..
```

**Frontend** (Node 18+):

```bash
cd frontend
npm install
cd ..
```

## Running the servers manually

`./dev.sh` runs both for you, but you can also start them in separate terminals:

**Backend** (terminal 1) — FastAPI on port 8000:

```bash
cd backend && source .venv/bin/activate
uvicorn konduktor.main:app --reload --port 8000
```

**Frontend** (terminal 2) — Vite on port 5173:

```bash
cd frontend && npm run dev
```

The Vite dev server proxies `/api` to the backend on port 8000, so both must be
running. API docs (Swagger UI): **http://localhost:8000/docs**.

## Choosing your collection

On launch, Konduktor asks you to pick a `collection.nml` — browse to it in the
in-app file picker (or paste a full path). You can switch collections any time
via the file name shown in the bottom-right status bar.

To skip the picker and auto-load a file on startup (handy for development), set
`KONDUKTOR_NML`:

```bash
KONDUKTOR_NML=/path/to/collection.nml uvicorn konduktor.main:app --port 8000
```

## Status

- ✅ **Phase 1** — backend core + read-only API
- ✅ **Phase 2** — track explorer UI (grid, search, filters, playlist browsing)
- ✅ **Phase 3** — playlist editing/creation (drag-reorder, add/remove, rename,
  delete) with surgical, backup-first saves
- ⬜ **Phase 4** — polish + optional Tauri desktop packaging

## Editing & saving (Phase 3)

Playlist edits (create/rename/delete, add/remove, drag-reorder) are live in the
UI. Click **Save to Traktor** to write them to `collection.nml`.

> **Close Traktor before saving** — it overwrites `collection.nml` on exit, so
> it would clobber your changes otherwise.

**Safety guarantees:**
- Every save writes a timestamped `.bak` backup first, into a `backups/` folder
  next to your collection.
- Saves are *surgical* — only the `<PLAYLISTS>` section is rewritten; the track
  `COLLECTION` stays byte-for-byte identical.
- To try it against a copy, point `KONDUKTOR_NML` at one.

The backend test `backend/test_phase3.py` exercises the full write path against
a temp copy and asserts these guarantees.
