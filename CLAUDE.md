# CLAUDE.md

Guidance for working in this repo. Read before making changes.

## What this is

**Konduktor** — a library-management and track-preparation tool for **Native
Instruments Traktor** (Pro 4 / NML v20), built on the `traktor-nml-utils`
library. The user is a DJ; the goal is a polished, multiplatform web app for
browsing the collection and editing playlists, later possibly wrapped in Tauri.

## Architecture

Two independent apps that talk over HTTP:

- **`backend/`** — Python + FastAPI. Parses `collection.nml` via
  `traktor-nml-utils` and serves a query API. Entry point:
  [backend/konduktor/main.py](backend/konduktor/main.py).
  - `collection_service.py` — loads the NML once into memory, flattens ENTRYs
    into `Track` objects, indexes them by primary key, and builds the playlist
    tree. All query logic (filter/sort/facets/stats) lives here.
  - `schemas.py` — Pydantic response models.
  - `main.py` — FastAPI routes (all under `/api`).
- **`frontend/`** — React + TypeScript + Vite. A dark, virtualized track
  explorer. Entry: [frontend/src/App.tsx](frontend/src/App.tsx).
  - `api.ts` — typed client + all API types (keep in sync with backend
    `schemas.py`).
  - `components/` — `Sidebar` (playlist tree), `Toolbar` (search/filters),
    `TrackTable` (TanStack Table + Virtual grid), `StatusBar`, `RatingStars`.
  - Data flow: the whole library is fetched once (`/api/tracks?limit=20000`);
    filtering and sorting happen **client-side** for instant interaction.
    Styling is Tailwind v4 with design tokens in `src/index.css`.

## Commands

```bash
./dev.sh                       # start both servers (see README for one-time setup)

# Backend
cd backend && source .venv/bin/activate
uvicorn konduktor.main:app --reload --port 8000
# point at another file: KONDUKTOR_NML=/path/to/collection.nml uvicorn ...

# Frontend
cd frontend
npm run dev          # dev server (proxies /api -> :8000)
npm run build        # tsc -b && vite build — run this to typecheck
```

**Tests:** `cd backend && ./run_tests.sh` (runs against a temp copy of the real
collection). **ALWAYS run this before changing anything in the save /
serialization path.** It enforces:
- `test_save_fidelity.py` — a no-op save is byte-identical, and an edit changes
  only the edited playlist's block (this is the guard that catches serialization
  regressions like the lxml reformatting bug).
- `test_phase3.py` — full create/add/reorder/rename/delete/save cycle stays
  Traktor-valid, backup-first, COLLECTION byte-identical, original untouched.

Also validate the backend interactively at `http://localhost:8000/docs` and the
frontend at `http://localhost:5173`.

## Critical gotchas — read these

1. **`traktor-nml-utils` MUST come from GitHub, not PyPI.** PyPI's `3.1.0` is
   stale and **cannot parse Traktor Pro 4 (NML v20)** files — its strict parser
   dies on the v4 `<GRID>` element inside `CUE_V2`. `requirements.txt` pins the
   GitHub `master` (v4.0.0). Do not "simplify" this to a plain PyPI pin.
2. **Never write to the user's `collection.nml`.** It is real, irreplaceable
   data (8,485 tracks). The app is read-only today. Phase 3 writes must operate
   on a copy and **back up first**.
3. **Saving is lossy.** The library's `save()` re-serializes the entire file
   (reflows whitespace, `124.000000`→`124.0`, reorders attributes) and does
   **not** update count attributes (e.g. `PLAYLIST ENTRIES`). For Phase 3, prefer
   **surgical edits** to just the `PLAYLISTS` node (e.g. via lxml), recompute
   counts, and back up — do not round-trip the whole 14 MB collection.

## Data model notes

- **Track primary key** = `f"{location.volume}{location.dir}{location.file}"`,
  e.g. `Macintosh HD/:Music/:one.mp3`. This is how playlist entries
  (`PRIMARYKEY.KEY`) join to collection tracks. `Track.id` uses this.
- **Rating** = `RANKING / 51`, giving 0–5 stars (`_rating_stars`).
- **Key** is Traktor's display key string, e.g. `"10m"` (Open Key notation).
- **Playlist tree**: recurse `nml.playlists.node.subnodes.node`. A node's `.type`
  is `FOLDER`, `PLAYLIST`, or `SMARTLIST`. Smart playlists are rule-based and
  have no static entry list. Synthetic ids (`pl-N`/`fld-N`/`sl-N`) are assigned
  at load and map to entries in `CollectionService._playlist_entries`.
- Model classes use single-capital names: `Entrytype`, `Primarykeytype`,
  `CueV2Type`. `Entrytype` is shared by collection entries and playlist entries.

## Conventions

- Backend: type hints, Pydantic models for all responses, keep query logic in
  `collection_service.py` (routes stay thin).
- Frontend: functional components, TanStack Query for fetching, Tailwind
  utility classes with the `ink-*`/`accent`/`gold` tokens from `index.css`.
  Keep `api.ts` types aligned with `schemas.py`.
- Python 3.14 note: pin dependencies loosely enough to get prebuilt wheels
  (older `pydantic-core` pins force a from-source Rust build that fails).

## Roadmap

- ✅ Phase 1 — backend core + read-only API
- ✅ Phase 2 — track explorer UI
- ✅ Phase 3 — playlist editing/creation + safe (backup-first, surgical) saves
- ⬜ Phase 4 — polish + optional Tauri desktop packaging

## Phase 3 write path (playlists)

- `playlist_store.py` (`PlaylistStore`) edits the **traktor-nml-utils dataclass
  model** in memory (create/rename/delete/set-entries). On `save()` it renders
  the model, extracts only the freshly rendered `<PLAYLISTS>…</PLAYLISTS>` span,
  and splices it into the original file bytes — COLLECTION stays byte-identical,
  and a timestamped `.bak` is written first.
- **Minimal-diff is load-bearing — do NOT re-serialize with lxml.** lxml
  reformats the whole PLAYLISTS section (~2500 changed lines). The render MUST
  use the library's pipeline: `XmlSerializer().render(nml)` →
  `restore_traktor_float_format(s, nml)` → `format_traktor_layout(s)` (both
  imported from `traktor_nml_utils`). That reproduces Traktor's exact layout, so
  unedited playlists stay byte-identical and only edited entries diff (verified:
  a 13-track reorder changes exactly 24 lines).
- Playlists are keyed by their stable `UUID`; folders by synthetic path id
  (`fld:<name>/<name>`). PRIMARYKEY `TYPE` is `STEM` if the track has a STEMS
  child (`CollectionService.stem_keys`, resolved via `entries_for`), else
  `TRACK`.
- `POST /api/save` always writes (backup-first); no write gate. Endpoints:
  `POST/PATCH/DELETE /api/playlists[...]`, `PUT .../entries` (replace),
  `POST .../add` (append), `POST /api/save`.
- Test: `backend/test_phase3.py` (runs against a temp copy; asserts surgical +
  backup + Traktor-valid). Run: `python test_phase3.py` in the backend venv.
