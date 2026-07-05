# CLAUDE.md

Guidance for working in this repo. Read before making changes.

## What this is

**Konduktor** — a library-management and track-preparation tool for **Native
Instruments Traktor** (Pro 4 / NML v20), built on the `traktor-nml-utils`
library. The user is a DJ; the goal is a polished, multiplatform web app for
browsing the collection, editing playlists, and preparing tracks (metadata,
cover art, and audio prep: playback, waveform, beatgrid, cues/hotcues, loops) —
a lightweight alternative to heavy Traktor, later possibly wrapped in Tauri and
given a mobile version.

## Architecture

Two independent apps that talk over HTTP:

- **`backend/`** — Python + FastAPI. Parses `collection.nml` via
  `traktor-nml-utils` and serves a query + edit API. Entry point:
  [backend/konduktor/main.py](backend/konduktor/main.py).
  - `collection_service.py` — the **read** model: loads the NML, flattens ENTRYs
    into `Track` objects, indexes them by primary key. All query logic
    (filter/sort/facets/stats) lives here. `replace_track()` refreshes one
    projection after an edit (bridges the read model and the edit store).
  - `playlist_store.py` (`PlaylistStore`) — the **edit** model: owns the parsed
    dataclass NML and applies all edits (playlists, track metadata, cover art,
    AND track-prep edits: hotcues/cues, beatgrid/BPM, lock), then renders +
    saves. See "Write path" below.
  - `file_tags.py` — reads/writes embedded audio-file tags & cover art (mutagen)
    and resolves Traktor `LOCATION`s to OS paths. Best-effort.
  - `discovery.py` — finds Traktor collections in their default install location
    (`~/Documents/Native Instruments/Traktor <version>/collection.nml`) for the
    picker's "Automatic" option; sorts newest Traktor version first.
  - `prefs.py` — tiny persisted user-prefs store in `userprefs.json` (resolved
    relative to the package so it travels with the app; gitignored). Holds the
    last-opened collection path, the library column layout, and the prep deck's
    main-waveform zoom (`mainZoomSec`). Best-effort I/O.
  - `schemas.py` — Pydantic response/request models.
  - `main.py` — FastAPI routes (all under `/api`). Holds an `AppState` with the
    currently-loaded collection; it starts **unloaded** and the collection is
    chosen at runtime via `POST /api/collection/open` (data routes 409 until
    then). `GET /api/fs/list` powers the in-app file browser;
    `GET /api/collection/options` feeds the picker's auto-detect + last-opened
    shortcuts; `GET`/`PATCH /api/prefs` persist UI prefs. Setting `KONDUKTOR_NML`
    auto-loads on startup (dev/tests).
- **`frontend/`** — React + TypeScript + Vite. A dark, virtualized track
  explorer. Entry: [frontend/src/App.tsx](frontend/src/App.tsx).
  - `api.ts` — typed client + all API types (keep in sync with backend
    `schemas.py`).
  - `components/` — `CollectionPicker` (startup chooser: Automatic / Open last /
    Find manually — the last reveals a file browser), `Sidebar` (playlist tree +
    create/rename/delete), `SaveBar`, `Toolbar` (search/filters + `ColumnsMenu`),
    `TrackTable` (TanStack Table + Virtual grid; per-row play button, configurable
    columns, inline double-click editing), `PlaylistEditor` (dnd-kit reorder),
    `SelectionBar` (bulk add-to-playlist), `ContextMenu` + `EditTagsDialog`
    (right-click → multi-field metadata + album-art edit), `StatusBar`,
    `RatingStars` (read-only, or click-to-set when given `onChange`), `Toast`.
  - **Prep strip** (DJ deck across the top of the window): `PrepStrip` owns it —
    transport (play/pause, CUE), two waveforms (`MainWaveform` scrolling+zoomable
    — its zoom is owned by `PrepStrip` so it survives track switches and persists,
    `OverviewWaveform` whole-track), `LoopControls`, `HotcueBar`, `GridControls`
    (BPM readout/editor, fine ±0.01 / coarse ±0.25 BPM nudge, /2·×2, tap tempo,
    grid-phase nudge, set beat 1, lock, delete grid).
    Prep libs live in `src/lib/`: `playbackEngine.ts` (Web Audio, seamless loops),
    `scratchEngine.ts` (drag-to-scratch), `waveform.ts` (frequency-colored
    analysis + paint), `cues.ts` (draw cues/loop/beatgrid/cue-point). See "Prep
    engine" below.
  - `lib/trackColumns.tsx` — single source of truth for the library table's
    columns (defs, default widths/visibility/order, the Columns-menu list, the
    inline `InlineEdit` cell, and the `TableMeta.onEditField` augmentation).
  - Data flow: the whole library is fetched once (`/api/tracks?limit=20000`);
    filtering and sorting happen **client-side** for instant interaction. Edits
    apply in-memory (server holds them) and the sidebar **Save to Traktor**
    button flushes to disk. Styling is Tailwind v4 with tokens in `src/index.css`.
    Library column layout (visibility/order/width) persists to `userprefs.json`
    via `GET`/`PATCH /api/prefs` (debounced; hydrated on launch, merged against
    defaults so newly-added columns still appear). The prep deck's main-waveform
    zoom persists the same way (`mainZoomSec`, hydrated in `PrepStrip` from the
    shared `['prefs']` query).

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
- `test_save_fidelity.py` — a no-op save is byte-identical (A); a playlist edit
  changes only that playlist's block (B); a **track-metadata edit changes only
  that `<ENTRY>`** (C); a **hotcue create is localized + round-trips** with
  START stored in ms (D); a **beatgrid/BPM edit is localized + round-trips** (E).
  The guard that catches serialization regressions like the lxml reformatting bug.
- `test_phase3.py` — full create/add/reorder/rename/delete/save cycle stays
  Traktor-valid, backup-first, COLLECTION byte-identical, original untouched.

Also validate the backend interactively at `http://localhost:8000/docs` and the
frontend at `http://localhost:5173`.

## Critical gotchas — read these

1. **`traktor-nml-utils` MUST come from GitHub, not PyPI.** PyPI's `3.1.0` is
   stale and **cannot parse Traktor Pro 4 (NML v20)** files — its strict parser
   dies on the v4 `<GRID>` element inside `CUE_V2`. `requirements.txt` pins the
   GitHub `master` (v4.0.0). Do not "simplify" this to a plain PyPI pin.
2. **The collection is real, irreplaceable data — protect it.** Every save
   writes a timestamped `.bak` first (into a `backups/` folder next to the
   collection). When testing writes, point `KONDUKTOR_NML` at a COPY. Warn the
   user to close Traktor before saving (it overwrites `collection.nml` on exit).
3. **Never re-serialize with lxml.** lxml reformats the whole file (collapses
   empty tags, drops float precision) → thousands of noise lines. The ONLY safe
   render is the library's own pipeline (see "Write path"), which reproduces
   Traktor's byte-exact layout so a no-op save is byte-identical and only edited
   objects diff. `test_save_fidelity.py` enforces this.

## Data model notes

- **Track primary key** = `f"{location.volume}{location.dir}{location.file}"`,
  e.g. `Macintosh HD/:Music/:one.mp3`. This is how playlist entries
  (`PRIMARYKEY.KEY`) join to collection tracks. `Track.id` uses this.
- **Rating** = `RANKING / 51`, giving 0–5 stars (`_rating_stars`).
- **Key** is Traktor's display key string, e.g. `"10m"` (Open Key notation).
- **Playlist tree**: recurse `nml.playlists.node.subnodes.node`. A node's `.type`
  is `FOLDER`, `PLAYLIST`, or `SMARTLIST`. Smart playlists are rule-based and
  have no static entry list. `PlaylistNode.id` is the playlist's stable `UUID`;
  folders use a synthetic path id `fld:<name>/<name>`, smart playlists `sl:<path>`.
- Model classes use single-capital names: `Entrytype`, `Primarykeytype`,
  `CueV2Type`. `Entrytype` is shared by collection entries and playlist entries.

## Conventions

- Backend: type hints, Pydantic models for all responses, keep query logic in
  `collection_service.py` (routes stay thin).
- Frontend: functional components, TanStack Query for fetching, Tailwind
  utility classes with the `ink-*`/`accent`/`gold`/`mint`/`pink` tokens from
  `index.css`. Keep `api.ts` types aligned with `schemas.py`.
- Python 3.14 note: pin dependencies loosely enough to get prebuilt wheels
  (older `pydantic-core` pins force a from-source Rust build that fails).

## Roadmap

- ✅ Phase 1 — backend core + read-only API
- ✅ Phase 2 — track explorer UI
- ✅ Phase 3 — playlist editing/creation + safe (backup-first, surgical) saves
- ✅ Runtime collection picker (in-app file browser)
- ✅ Track metadata editing (right-click → Edit Tags dialog, plus inline
  double-click cell editing + click-to-set rating stars) with embedded file-tag
  & cover-art writing
- ✅ Configurable library columns (show/hide, drag-reorder, resize) + auto-detect
  collection picker (Automatic / last-opened / manual), persisted to `userprefs.json`
- ✅ Track prep — audio playback, frequency-colored scrolling waveform, scratch,
  beatgrid display + editing, cue/hotcue create/jump/delete, loops; keyboard
  shortcuts (Space = play/pause, 1–8 = hotcues, Shift+1–8 = delete). See "Prep engine".
- ✅ Track prep Tier 2 — cue-point fine editing / audio export polish
- ⬜ Bulk metadata editing; ⬜ Phase 4 — polish + optional Tauri desktop packaging

## Write path (playlists + track metadata + prep)

`PlaylistStore` edits the **traktor-nml-utils dataclass model** in memory and,
on `save()`, renders the WHOLE model and writes it (backup-first, into a
`backups/` folder next to the collection). Whole-file render is proven
byte-identical for unedited content, so only the objects you actually changed
diff — no COLLECTION splicing needed.

- **The render MUST use the library's pipeline** (do NOT use lxml — see gotcha 3):
  `XmlSerializer().render(nml)` → `restore_traktor_float_format(s, nml)` →
  `format_traktor_layout(s)` (both funcs imported from `traktor_nml_utils`).
  `_render()` in `playlist_store.py` is the single place this happens.
- **Playlists**: keyed by stable `UUID`; folders by synthetic path id
  (`fld:<name>/<name>`). PRIMARYKEY `TYPE` is `STEM` if the track has a STEMS
  child (`CollectionService.stem_keys`, resolved via `entries_for`), else `TRACK`.
- **Track metadata**: `set_track_metadata(track_id, fields)` edits only the SAFE
  set — title, artist, album, genre, label, remixer, producer, mix,
  release_date, comment, rating (0–5 stars → `RANKING = stars*51`). Path, BPM and
  key are intentionally NOT editable via metadata (path = identity; BPM/key are
  audio/grid — BPM is edited through the grid path below). This is the set the
  Edit Tags dialog AND the inline table editing (double-click cell, click rating
  stars) both write.
- **Track prep** (also `PlaylistStore`, same render/save pipeline):
  `set_hotcue(track_id, slot, start_sec, type, length_sec)` creates/replaces a
  `CUE_V2` (types 0 cue, 1 fade-in, 2 fade-out, 3 load, 5 loop; START/LEN stored
  in **ms**; a loop hotcue carries LEN); `set_hotcue_type` changes just the type;
  `delete_hotcue` removes a slot. `set_grid(track_id, bpm?, anchor_sec?)` edits
  the TEMPO + the grid marker (`CUE_V2` type 4 with a `<GRID BPM>` child);
  `delete_grid` drops it; `set_lock(track_id, locked)` toggles the `LOCK` attr.
  Invariants D/E prove hotcue + grid edits are localized and round-trip.
- **Embedded file tags + cover art**: on save, for each edited track,
  `_sync_file_tags()` writes the changed fields (and staged cover art) into the
  audio file via `file_tags` (mutagen; ID3/MP4/FLAC/AIFF, WAV skipped) —
  best-effort (reports `file-not-found` if the drive isn't mounted, `.nml` still
  saves). Frame/atom-level writes preserve everything else (verified: cover art,
  BPM, key, and Traktor STEM atoms all survive). `.nml` is the source of truth;
  Traktor caches its own cover thumbnail so it may need a manual "Import Cover
  Art" to show replaced art.
- Reads come from `CollectionService`; after a metadata edit the endpoint calls
  `service.replace_track()` so the change shows immediately (the two models
  aren't yet consolidated).
- `POST /api/save` always writes (backup-first); no write gate. Endpoints:
  `POST/PATCH/DELETE /api/playlists[...]`, `PUT .../entries` (replace),
  `POST .../add` (append); `PATCH /api/tracks` (metadata); `GET/PUT /api/tracks/art`
  (cover art); `GET /api/tracks/audio` (Range-aware stream for playback/analysis);
  `GET /api/tracks/cues`, `POST/PATCH/DELETE /api/tracks/hotcue`,
  `PATCH/DELETE /api/tracks/grid`, `PATCH /api/tracks/lock` (prep); `POST /api/save`.
- Tests: `backend/run_tests.sh` → `test_save_fidelity.py` + `test_phase3.py`
  (both run against a temp copy). Run before touching the save/serialization path.

## Prep engine (frontend)

The prep strip plays every track through the **Web Audio API** (a plain
`<audio>` element can't do reverse/arbitrary-rate scratch or seamless loops).

- `playbackEngine.ts` — an `AudioBufferSourceNode` with a `startOffset`/
  `startedAt` position model; `getPosition()` reads the AudioContext clock
  (high-res → smooth playhead). Seamless loops via native `loopStart`/`loopEnd`;
  `setLoop()` re-anchors position so enabling/disabling a loop doesn't desync.
- `scratchEngine.ts` — a persistent (idle-silent) `ScriptProcessorNode` that,
  while dragging the main waveform, plays PCM at a position gliding toward the
  cursor, then coasts with inertia on release.
- **CRITICAL gotcha**: playing an `AudioBuffer` "acquires the content" in
  Firefox and **detaches** any `getChannelData()` views held elsewhere. The
  scratch engine therefore **copies** the PCM in `load()` (never holds the
  playback buffer's views). Playback and scratch also run on **separate
  `AudioContext`s** — sharing one goes silent after a source has played.
- `waveform.ts` — decodes once (buffer reused by both engines, no re-decode),
  splits into 3 bands via `OfflineAudioContext` biquads, and colors each column
  by blending three palette targets (bass→orange, mids→violet, highs→cyan — no
  green, matching Traktor's Spectrum). Height = amplitude.
- `cues.ts` — canvas drawers for cue markers (type-colored), the active loop
  band, the beatgrid (white beats/brighter downbeats), and the floating CUE
  point (gold). The main waveform's playhead is red; both waveforms share these.
- Prep edits go straight to the prep endpoints above; after each edit the
  endpoint refreshes the read model (`service.replace_track()`) so cue/grid
  counts update live, and the frontend invalidates the relevant queries.
