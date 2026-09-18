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

- **`backend/`** — Python + FastAPI. Entry point:
  [backend/konduktor/main.py](backend/konduktor/main.py).

  **Architecture: projection + command replay.** The generic model is a *read
  projection*; edits are strictly-generic commands that a per-platform **adapter**
  replays onto its **retained native model**, which stays the write target. The
  generic model is NEVER serialized back over a library — that is what preserves
  byte-exact saves, and it is the only model that works for formats you cannot
  regenerate (a Rekordbox SQLite row, a Serato tag inside an audio file). Traktor
  is currently the only adapter; see `.claude/discussions/` for the decision log.

  - `core/` — **platform-independent. Must not import `traktor_nml_utils` or
    `konduktor.adapters`** (`test_layering.py` enforces this). This is the
    contract a Rekordbox or Serato adapter will be written against.
    - `model.py` — the generic model (`Track`, `CuePoint`, `GridMarker`,
      `TrackCues`, `PlaylistNode`, `Facets`, `Stats`, …). Served straight to HTTP.
    - `adapter.py` — the `LibraryAdapter` / `LibraryDriver` protocols and the
      error tree (`NotFound` → 404, `InvalidCommand` → 400, `Unsupported` → 422),
      mapped to status codes once in `main.py` rather than per route.
    - `capabilities.py` — `Capabilities`: what the loaded library can persist, so
      the UI can gate controls instead of branching on the platform.
    - `query.py` (`TrackIndex`) — all query/filter/sort/facet/stats logic, over
      generic `Track`s. Shared so "sort by artist" cannot mean different things
      on different platforms behind one UI.
    - `edit_journal.py` (`EditJournal`) — every edit this session at **field
      resolution** (`track/set/genre`, `cue/add/slot:1`, `grid/marker-bpm/marker:0`).
      Drives the file-tag sync, the version-history message, and `retarget()`
      when a path remap changes track ids. Recording `before` keeps undo cheap later.
    - `registry.py` — adapter selection by **`can_open()` probe**, not extension
      (Rekordbox is a `.db`, Serato is a directory).
    - `audio_tags.py`, `pathmap.py`, `auto_hotcues.py` — format-agnostic helpers.
  - `adapters/traktor/` — everything that knows NML exists.
    - `store.py` (`TraktorStore`) — the retained native model: owns the parsed
      dataclass NML, applies every edit, renders + saves. See "Write path".
    - `adapter.py` (`TraktorAdapter`) — owns the store **and** the `TrackIndex`
      built from it, so projection and native model cannot drift. Translates the
      generic cue vocabulary to Traktor's integers (the only place that mapping
      exists). **Every mutating command returns its refreshed projection** — a
      forgotten refresh would be a silently stale UI no byte test would catch.
    - `projection.py` — native → generic (`to_track`, `to_track_cues`).
    - `driver.py`/`discovery.py` — `can_open`, default-location detection, restore.
    - `beatgrid.py`, `locations.py`, `capabilities.py` — Traktor's grid/companion
      rules, LOCATION ↔ OS path conversion, and its capability set.
  - `adapters/rekordbox/` — **READ-ONLY** (milestone 1 of the Rekordbox work).
    `master.db` is SQLCipher-encrypted SQLite, read via `pyrekordbox`. Every
    mutating command raises `Unsupported` and `capabilities()` advertises nothing
    as editable, so the UI never offers an edit. Three things differ from Traktor
    and are the reason this adapter is not a copy of it:
    - **The beatgrid is NOT in the database.** There is no grid table at all; it
      lives in per-track ANLZ analysis files as a `PQTZ` tag listing EVERY BEAT.
      `beatgrid.py` collapses those into the generic marker list on read and
      expands them back on write. `store.anlz_grid()` parses them **lazily** —
      ~1.4 ms each, i.e. ~12 s for a library of 8,500 if done at open — so
      `Track.grid_marker_count` carries a documented approximation (1 when the
      track has a BPM) that `track_cues()` corrects.
    - **Cues live in two places** that must agree: `djmdCue` rows and a JSON
      mirror in `contentCue.Cues` (+ `rb_cue_count`). Rekordbox stamps
      `rb_local_usn` on `contentCue`, never on the cue rows.
    - **`djmdCue.Kind` is an OPAQUE slot id**: 0 = memory cue, N = hot cue slot N.
      Pads A and C store 1 and 3, but the 6th pad stored 7 — so the slot→letter
      mapping is NOT a plain index and is deliberately left to the UI.
    Writes are refused outright on a **cloud-synced** library (detected by a
    server-issued `usn` on any row): a bad sync state would propagate to the
    user's other machines, which version history cannot undo.
  - `app_state.py` — the one loaded library, and the **version-history commit**.
    History is app-level: the adapter returns the bytes it wrote plus a summary,
    and `AppState.save()` versions them. Every write path must go through it.
  - `prefs.py` — persisted user prefs (`userprefs.json` in the per-OS app-data
    dir). Last-opened collection, library column layout, prep-deck zoom. Best-effort.
  - `schemas.py` — HTTP request bodies + envelopes; re-exports `core.model`.
  - `main.py` — thin FastAPI routes (all under `/api`), talking only to the
    adapter. Starts **unloaded**; the library is chosen at runtime via
    `POST /api/collection/open` (data routes 409 until then).
    `GET /api/capabilities` feeds the UI's gating; `GET /api/fs/list` powers the
    file browser; `GET`/`PATCH /api/prefs` persist UI prefs. Setting
    `KONDUKTOR_NML` auto-loads on startup (dev/tests).
- **`frontend/`** — React + TypeScript + Vite. A dark, virtualized track
  explorer. Entry: [frontend/src/App.tsx](frontend/src/App.tsx).
  - `api.ts` — typed client + all API types. **Generic, not Traktor-shaped**:
    cue types are the strings `cue`/`fade_in`/`fade_out`/`load`/`loop`, a cue has
    a `role` (`hotcue`/`memory`) and a nullable `slot`, and `editable` +
    `readonly_reason` say whether the adapter will accept a command on it — gate
    on `editable`, never on a platform-specific reason like `grid_marker`. Keep
    in lockstep with `core/model.py`; land both in the same commit.
  - `lib/capabilities.tsx` — `useCaps()` over `GET /api/capabilities`. The app
    does not render until capabilities resolve, so no first frame can offer an
    edit the adapter would reject. Containers call `useCaps()`; presentational
    leaves take plain values as props. **No component branches on the platform.**
  - `lib/platformCopy.ts` — composes user-facing wording from the *facts* the
    adapter supplies (`app_name`, `library_label`, `overwrite_risk`), so
    "Close Traktor before saving — it overwrites collection.nml on exit" stays
    specific without being hard-coded. Never put finished sentences in the API.
  - `components/` — `CollectionPicker` (startup chooser: Automatic / Open last /
    Find manually — the last reveals a file browser), `Sidebar` (playlist tree +
    create/rename/delete), `SaveBar`, `Toolbar` (search/filters + `ColumnsMenu`),
    `TrackTable` (All Tracks — TanStack Table + **virtualized** grid; per-row play
    button, configurable columns, inline double-click editing) and `PlaylistTable`
    (playlists — same columns/cells/play/inline-edit via shared `HeaderRow`/
    `RowCells`/`PlayButton`, so the two views look identical, PLUS dnd-kit
    drag-to-reorder + a remove button, header sorting disabled so the manual order
    stands; `canReorder` is false while a filter/search is active so a partial order
    can't overwrite the full entry list). `PlaylistTable` is non-virtualized
    (playlists are small) and feeds `useReactTable` a **stable** empty `sorting`
    array (`NO_SORTING`): a controlled `state.sorting` that's a fresh `[]` each
    render with no `onSortingChange` makes TanStack Table re-sync its internal
    state every commit → infinite re-render loop. Keep that reference stable.
    `SelectionBar` (bulk add-to-playlist), `ContextMenu` + `EditTagsDialog`
    (right-click → multi-field metadata + album-art edit), `StatusBar`,
    `RatingStars` (read-only, or click-to-set when given `onChange`), `Toast`,
    `UpdateDialog` (`UpdateCheck`, mounted in `main.tsx` beside `App` so it shows
    even on the picker screen: on startup fetches the latest GitHub release and,
    if its tag's **semver part** is newer than `__APP_VERSION__` — build-number
    `-b<N>` bumps don't count — shows an update dialog with the release's
    "What's Changed" bullets; Download opens the release page in the system
    browser via `@tauri-apps/plugin-shell` `open` — falls back to `window.open`
    outside Tauri; "Skip this version" persists `skippedUpdateVersion` to
    userprefs via `/api/prefs`; all failures are silent).
  - **Prep strip** (DJ deck across the top of the window): `PrepStrip` owns it —
    transport (play/pause, CUE), two waveforms (`MainWaveform` scrolling+zoomable
    — its zoom is owned by `PrepStrip` so it survives track switches and persists,
    `OverviewWaveform` whole-track), `LoopControls`, `HotcueBar`, `GridControls`
    (BPM readout/editor, fine ±0.01 / coarse ±0.25 BPM nudge, /2·×2, tap tempo,
    marker nudge, add/delete marker, lock, delete grid (confirmed) — all on the
    marker **governing the playhead**, since a beatgrid is a marker list and
    there is no separate marker selection; see "Beatgrid" below).
    Prep libs live in `src/lib/`: `playbackEngine.ts` (Web Audio, seamless loops),
    `scratchEngine.ts` (drag-to-scratch), `waveform.ts` (frequency-colored
    analysis + paint), `beatgrid.ts` (the marker-list beat math — every
    beat/bar/snap/jump calculation goes through it), `cues.ts` (draw
    cues/loop/beatgrid/cue-point). See "Prep engine" below.
  - Capability-gated today: hotcue bank size (`HotcueBar`, the auto-cue guard and
  the digit shortcuts all read `cues.hotcue_slots`), the cue-type dropdown
  (`cues.types`), the rating scale (`tracks.rating_max`), the grid Lock button
  (`grid.lockable`). Carried but deliberately unused until a second adapter
  exists: `slot_labels: 'letter'`, `palette`, `loops: 'separate_bank'`, and
  memory-cue *editing* (one-platform features stay preserved-but-uneditable).
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
npm run test         # vitest — unit tests for src/lib/beatgrid.ts
```

**Tests:** `cd backend && ./run_tests.sh` (runs against a temp copy of the real
collection). **ALWAYS run this before changing anything in the save /
serialization path.** It enforces:
- `test_save_fidelity.py` — a no-op save is byte-identical (A); a playlist edit
  changes only that playlist's block (B); a **track-metadata edit changes only
  that `<ENTRY>`** (C); a **hotcue create is localized + round-trips** with
  START stored in ms (D); a **grid-marker edit is localized + round-trips** (E);
  **flexible (multi-marker) grids** add/move/delete reversibly, `delete_grid`
  leaves no companion debris (I); **companion cues are protected** from hotcue
  commands (J); and real flexible grids in the collection project correctly (K).
  The guard that catches serialization regressions like the lxml reformatting bug.
- `test_phase3.py` — full create/add/reorder/rename/delete/save cycle stays
  Traktor-valid, backup-first, COLLECTION byte-identical, original untouched.
- `test_traktor_adapter.py` — the **generic layer**: one parse per open, the
  projection refreshing after every command family, cue-type translation,
  capabilities, and `set_analysed_grid` vs `replace_grid`. `test_save_fidelity`
  covers the store and its bytes; without this the adapter layer would be untested.
- `test_rekordbox_adapter.py` — the second adapter against the same contract:
  cue/grid/key translation as pure units (they need no library), then the
  projection, playlist tree, capabilities and the refusal of all 21 commands
  against a **temp copy** of the local Rekordbox library. Skips cleanly when no
  Rekordbox is installed, so it is safe on any machine.
- `test_layering.py` — `core/` imports nothing platform-specific, and no adapter
  imports another platform's library (checked on real imports via AST, so merely
  naming a platform in a comment is fine).

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
- **Beatgrid = an ORDERED LIST of markers**, never a single BPM + anchor.
  Traktor stores each as a `CUE_V2 TYPE="4"` with its own `<GRID BPM>` child
  (flexible beatgrids, 3.4+); `<TEMPO BPM>` mirrors the **first marker by
  START**. A constant-tempo track is simply a list of length one, so this is the
  model for every track. Two rules verified against the real 8485-entry
  collection — get these wrong and you corrupt grids:
  - **Marker NAME is not a discriminator** (`AutoGrid` ×7888, `n.n.` ×64,
    `Beat Marker` ×14, `Unnamed` ×4). Identify by the `<GRID>` child, order by
    `START` — never by name or document order.
  - **Companion cues are conventional, not structural.** Traktor usually pairs a
    marker with a white (`COLOR="#FFFFFF"`) `TYPE="0"` cue at the same position,
    which occupies a **real hotcue slot** — but 60 of 7970 markers have none.
    Konduktor follows them, keeps them in sync, and **never invents one** (except
    `place_grid_companion`, used only by Auto Grid). The `#FFFFFF` test is
    load-bearing: 49 uncoloured cues and 18 *loops* also sit within 1 ms of a
    marker and must not be dragged or deleted. `backend/konduktor/beatgrid.py`
    is the single shared definition of all of this.
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

## Versioning

**There is ONE source of truth for the app version: the `version` field in
[frontend/package.json](frontend/package.json).** To release a new version, bump
that number and nothing else — everything derives from it:

- **Tauri / installers** — `tauri.conf.json` sets `"version": "../package.json"`,
  so the `.dmg`/`.exe` names + bundle version come from `package.json`.
- **Backend API** — `konduktor/__init__.py` `_read_version()` reads
  `package.json` (dev: `<repo>/frontend/package.json`; frozen sidecar: bundled at
  the PyInstaller root via `sys._MEIPASS`, added in `konduktor-sidecar.spec`).
  `main.py` uses this for the FastAPI `version`. Falls back to `"0.0.0"` if not
  found (cosmetic, must never crash startup).
- **Frontend / update check** — `vite.config.ts` bakes it in at build time as
  the `__APP_VERSION__` global (declared in `src/vite-env.d.ts`); `UpdateCheck`
  compares it against the latest GitHub release tag on startup.
- **CI release** — `.github/workflows/build.yml` reads it with
  `jq .version frontend/package.json` and tags each push
  `v<version>-b<run_number>` (release name `Konduktor v<version>-b<run_number>`).
  The `-b<N>` suffix keeps every push's tag/release unique.
- **NOT a source**: `src-tauri/Cargo.toml` `version` is inert Rust crate metadata
  (Tauri uses the config's `../package.json`); don't treat it as the app version.

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
- ✅ Flexible beatgrids — the grid is a marker list end to end (marker-level
  commands, piecewise beat math, playhead-derived marker editing). Step 1 of the
  multi-platform plan in `.claude/discussions/`.
- ✅ Generic model + adapter interface (Traktor as the only adapter) — step 2 of
  the multi-platform plan. Backend, wire format and UI are all generic; nothing
  above `adapters/traktor/` knows what Traktor is.
- 🟡 Rekordbox adapter — **read-only milestone done**: a Rekordbox library opens,
  projects and browses through the generic layer (tracks, playlists, cues,
  beatgrid), with every command refused. Research for the write path is captured
  in `.claude/handoffs/rekordbox-adapter.md` §8, incl. the verified result that
  Rekordbox accepts Konduktor-written rows when USNs are maintained. Remaining:
  metadata/playlist writes, then the hand-written cue store and ANLZ grid store.
- ⬜ Serato adapter; ⬜ export/conversion
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
  `delete_hotcue` removes a slot. (The adapter exposes these generically as
  `set_cue`/`set_cue_type`/`delete_cue`, taking a `cue_type` STRING — `cue`,
  `fade_in`, `fade_out`, `load`, `loop` — which the Traktor adapter maps to the
  integers below.) Hotcue commands **refuse a slot held by a grid
  marker's companion cue** (see "Beatgrid"). Beatgrid commands are marker-level:
  `add_grid_marker` (inherits the governing tempo when no BPM is given),
  `move_grid_marker` (clamped between its neighbours, drags the companion),
  `set_grid_marker_bpm` (also serves ×2 / ÷2, which retempo the governing marker
  like every other tempo control), `delete_grid_marker`, `replace_grid` (Auto
  Grid and the deck's Reset), `delete_grid` (markers **and** companions; keeps
  `TEMPO`), `place_grid_companion`. `set_lock(track_id, locked)` toggles `LOCK`.
  Invariants D/E/I/J prove hotcue + grid edits are localized and round-trip.
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
  `POST/PATCH/DELETE /api/tracks/grid/marker`,
  `PUT /api/tracks/grid` (replace), `DELETE /api/tracks/grid`,
  `PATCH /api/tracks/lock` (prep); `POST /api/save`.
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
  band, the beatgrid (per-segment white beats/brighter downbeats, plus marker
  lines — gold for the one governing the playhead, accent otherwise, with the
  active segment tinted), and the floating CUE point (gold). The main waveform's
  playhead is red; both waveforms share these.
- `beatgrid.ts` — the `BeatGrid` model (`buildBeatGrid` → `BeatGrid | null`).
  Unit-tested (`npm run test`, vitest); `beatgrid.test.ts` pins the property that
  a **single-marker grid is algebraically identical to the old single-BPM math**,
  which is what protects the 99.98% of tracks with a constant tempo.
- Prep edits go straight to the prep endpoints above; after each edit the
  endpoint refreshes the read model (`service.replace_track()`) so cue/grid
  counts update live, and the frontend invalidates the relevant queries.
