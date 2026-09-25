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
    - `audio_tags.py`, `pathmap.py` — format-agnostic helpers.
    - `structure.py` + `auto_hotcues.py` — **Auto Hotcues** (the deck's ✨ Auto
      button → `AutoCueDialog` → `POST /api/tracks/cue/auto`). The user binds each
      slot to an EVENT (`first_beat`, `intro_end`, `build_n`, `drop_n`,
      `breakdown_n` for n ≤ 3, `outro`, `last_beat`) plus an offset in BEATS;
      `structure.analyse` finds the events, `auto_hotcues.plan` resolves the
      template and reports an outcome per slot (`placed` / `not_found` /
      `out_of_range` / `occupied` / `protected` / `duplicate`) — "this track has
      no third drop" is an answer, not a silent gap. **One cue per beat**: a slot
      landing on a beat that already has a cue — one KEPT in the bank (the grid
      cue, a hand-placed cue, within a quarter beat) or a new one in a lower
      slot — is a `duplicate` of that slot. A replaced cue frees its beat only
      if its replacement is placed, which is circular, so `plan` iterates to a
      fixpoint. How it finds them:
      - **Everything is per bar ON THE TRACK'S GRID**, bar 1 = the first marker
        (Traktor's convention). Bar-1 detection from audio was tried: it never
        beat that rule, because DJ tracks start on a downbeat.
      - Per-bar kick / bass / loudness / brightness + MFCC, then an **exact DP
        segmentation whose boundary price depends on bar position** (cheap on
        ×8, dearer on ×4, very dear elsewhere) — the phrase bias lives in the
        objective, not in a snap afterwards.
      - **A drop is a high section entered from a low one**, not a loud one: a
        house intro is often as loud as its drop. A breakdown is where a drop
        ends when another drop follows; the end of the last one is the outro's.
      - Kick is the grid detector's sharp low-band onset, NOT on- vs off-beat
        energy (a house bassline lives on the off-beat), and "high" does not
        require a kick (drum & bass has none on every beat).
      Measured against the phrase analysis Rekordbox stores for 12 local
      "High"-mood tracks (PSSI; the previous detector's cues scored below a cue
      every 16 bars): drops 81% on the exact bar (22/27), breakdowns 80%
      (12/15), outro 42% within 4 bars — `backend/bench_structure.py` reruns
      it. Rekordbox's own "Up" spans the whole pre-drop groove where a DJ's
      build is the last 8–16 bars, so build scores against it are not
      meaningful. Halftime tracks inherit the grid's octave: at 174 a "bar" is
      half a musical bar.
    - `grid_detect.py` — **beatgrid detection** (the deck's Analyze button,
      `POST /api/tracks/grid/auto`). Fits ONE constant tempo + anchor to the whole
      track rather than tracking beats: `librosa.beat.beat_track`, which it
      replaced, quantises tempo to 23 ms frames and can only return 117.45 /
      123.05 / 129.20 / 136.00 between 115 and 140 BPM. Timing comes from a
      1–8 kHz attack band, and which attack is the beat from whether a kick's
      low end follows it — each band alone lost a real track to an off-beat hat
      or a syncopated bassline. Positions are in the **decoded audio's** time
      base (libsndfile = CoreAudio to the sample on MP3). **Rekordbox's grids sit
      a constant ~25 ms later on lossy files** and at 0 on WAV — a platform
      time-base difference, deliberately NOT corrected here; Traktor's is
      unmeasured. `backend/bench_grid_detect.py` scores it against any library's
      single-marker grids and reports that constant separately from detection
      error (29 Rekordbox references: old 1/29 BPMs, new 27/29).
    - `places.py` — where a person keeps things on THIS computer: the user's own
      folders (via `platformdirs` — hardcoding is wrong on every OS differently:
      `~/Videos` is `Movies` on macOS, Linux's are user-configurable localised
      XDG dirs, and Windows Known Folders are routinely relocated by OneDrive)
      and mounted volumes. The mount scanner lives here rather than in the
      OneLibrary adapter, where it grew: "where does this OS mount things" is
      not adapter knowledge, the file browser needs the same answer, and two
      scanners would drift. `is_boot_volume()` compares **st_dev against `/`**,
      not the name, because macOS lists the startup disk in `/Volumes` next to
      real removable media and `Macintosh HD` is only its default name.
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
  - `adapters/rekordbox/` — **track metadata, playlists and hot cues writable;
    the beatgrid still read-only** (milestones 2 and 3a). Also home to
    `anlz_writer.py`, which BUILDS analysis files from nothing — Pioneer's format,
    not one product's, so the OneLibrary exporter borrows it the same way it
    already borrows `beatgrid`.
    `master.db` is SQLCipher-encrypted SQLite, read via `pyrekordbox`. Every
    mutating command raises `Unsupported` and `capabilities()` advertises nothing
    as editable, so the UI never offers an edit. Three things differ from Traktor
    and are the reason this adapter is not a copy of it:
    - **The beatgrid is NOT in the database, and is written to ONE tag of ONE
      file.** There is no grid table at all; it
      lives in per-track ANLZ analysis files as a `PQTZ` tag listing EVERY BEAT.
      `beatgrid.py` collapses those into the generic marker list on read and
      expands them back on write. `write_pqtz()` rebuilds the entry list
      directly — `pyrekordbox`'s own setters refuse to change the NUMBER of
      beats, which every real edit does — then calls the tag's `update_len()`.
      **Only `.DAT`'s `PQTZ` is written.** `.EXT` holds a second "extended" grid
      (`PQT2`) whose per-beat byte `pyrekordbox` itself calls `unkown`, so
      writing it would mean guessing at bytes in data with no version history.
      Verified in Rekordbox 7: it reads the grid AND the deck's BPM from `PQTZ`,
      leaves a Konduktor-written `.DAT` untouched, and is unbothered by the stale
      `.EXT` (which would matter only for CDJ export — out of scope). It does
      **not** reconcile `djmdContent.BPM`, so the adapter maintains that column
      itself, mirroring the first marker exactly as Traktor's `<TEMPO>` does.
      Grid edits are **buffered until `save()`** so ANLZ file writes obey the same
      "edit in memory, Save writes to disk" contract as everything else; `save()`
      writes the files BEFORE committing the database, so a failed file write
      leaves nothing committed. `store.anlz_grid()` parses them **lazily** —
      ~1.4 ms each, i.e. ~12 s for a library of 8,500 if done at open — so
      `Track.grid_marker_count` carries a documented approximation (1 when the
      track has a BPM) that `track_cues()` corrects.
    - **Cues live in two places** that must agree: `djmdCue` rows and a JSON
      mirror in `contentCue.Cues` (+ `rb_cue_count`). Rekordbox stamps
      `rb_local_usn` on `contentCue`, never on the cue rows. Every cue write ends
      in `_sync_content_cue()`, which rebuilds the mirror from the rows — leaving
      it stale is this platform's version of Traktor's companion-cue desync.
      Field conventions are read off a real library, not guessed:
      `InFrame`/`OutFrame` are the position in frames at **150 fps**, a cue's
      `ContentUUID` and its mirror row's `ID` are both the **track's UUID**, an
      uncoloured cue is `Color=-1, ColorTableIndex=NULL`, and a loop is
      `Color=255, ColorTableIndex=0` with `BeatLoopSize = (beats << 16) | 1`.
      **`Kind` is 1-based AND skips 4** — the bank is `1,2,3,5,6,7,8,9` for pads
      A–H. Measured by writing a cue at every `Kind` 1–8 and reading the deck: a
      cue at `Kind=4` lands on no pad and Rekordbox shows it as a memory cue.
      Get this wrong and cues silently move to the wrong pad or vanish from the
      bank. Point cues AND loops both write; `cue_types` owns the translation.
      **Memory cues stay preserved-but-uneditable** (Rekordbox is the only
      platform with them — the two-platform promotion rule).
    - **Saving warns but does not block when Rekordbox is running.**
      `pyrekordbox.commit()` refuses outright, and its check is process-wide
      rather than per-file, so `RekordboxStore._commit()` suppresses that veto
      and logs instead — otherwise a temp copy could not be written while the
      user had a different library open.
    - **`djmdCue.Kind` is an OPAQUE slot id**: 0 = memory cue, N = hot cue slot N.
      Pads A and C store 1 and 3, but the 6th pad stored 7 — so the slot→letter
      mapping is NOT a plain index and is deliberately left to the UI.
    Writes are refused outright on a **cloud-synced** library (detected by a
    server-issued `usn` on any row): a bad sync state would propagate to the
    user's other machines, and there is no version history here to undo it.
    Every command passes through `_require_writable()` so a command added later
    cannot forget that check.
    Edits accumulate in the SQLAlchemy session and `save()` commits — which maps
    onto Konduktor's "edit in memory, Save writes to disk" model exactly, since
    `pyrekordbox`'s `commit()` is what assigns Rekordbox's USNs. A real Rekordbox
    7 was verified to accept a library written this way, display the edit, leave
    the row alone, and continue from the counter it was left at.
    `artist`/`album`/`genre`/`label`/`remixer` are **foreign keys into lookup
    tables**, so a write is find-or-create (pyrekordbox's `add_*` RAISES on an
    existing name) followed by a **flush + expire** — without which the ORM
    re-reads the stale relationship and a successful edit projects as a no-op.
    `producer`/`mix` are absent from `editable_fields`: Rekordbox has no column
    for them and its Composer is a different field.
    `close()` disposes the SQLAlchemy engine, not just the session — otherwise
    the OS file handle stays open and `master.db` cannot be replaced. `AppState`
    closes the previous adapter when opening a new library.
  - `adapters/onelibrary/` — **read-only.** The cross-vendor USB export format
    (AlphaTheta + Algoriddim + Native Instruments), read by CDJ-class hardware.
    Built as the source half of a future "import a stick into Traktor" feature.
    Full research, incl. the schema, is in
    [.claude/handoffs/onelibrary-adapter.md](.claude/handoffs/onelibrary-adapter.md);
    the four things that make it unlike the Rekordbox adapter:
    - **The library is a REMOVABLE DRIVE, not a file in a known place.** So
      `discovery.py` scans mount points rather than probing fixed paths, and
      `layout.py` owns the on-drive layout (`PIONEER/rekordbox/exportLibrary.db`,
      analysis under `PIONEER/USBANLZ/P0<xx>/<8 hex>/`). Every stored path is
      **drive-relative with a leading slash** — `/Contents/…/x.mp3` — which is
      NOT an absolute host path: joining it naively discards the mount point and
      silently yields `/Contents/…`, which looks like an empty drive rather than
      a path bug. `DriveLayout.resolve()` is the only place that join happens.
      A track's id is that drive-relative path, not `content_id`, which rekordbox
      renumbers from 1 on every re-export.
    - **Cues are NOT in the `cue` table**, which rekordbox exports empty; they are
      in the ANLZ files. `PCOB` is **split by pad** (`.DAT` holds 1–3, `.EXT` holds
      4 and up — reading only the `.DAT` silently loses pads D–H), and `PCO2` in
      the `.EXT` holds the complete set plus RGB colour and comments. `PCO2` wins,
      `PCOB` is the fallback, both merge across files first.
    - **The slot numbering differs from `master.db`.** `djmdCue.Kind` is a sparse
      bank that skips 4; ANLZ `hot_cue` is a **dense 1-based index** (pad D is
      `Kind` 5 but `hot_cue` 4), and 0 means memory cue. Measured by aligning the
      same five cues in both representations. Reuse the Rekordbox mapping here and
      every cue from pad D on lands one pad too far along. Times are **ms**, not
      150 fps frames; `loop_time` is `0xFFFFFFFF` (not 0) when there is no loop.
    - **Analysis files are parsed lazily** — `.DAT` ~2 ms but `.EXT` ~23 ms, so
      eager parsing would cost ~23 s on a 1,000-track drive. Cue and marker counts
      are therefore approximate in the library table and corrected by
      `track_cues()`. The `PQTZ` beatgrid is the same tag as Rekordbox's, and the
      collapse rule is **shared by import** rather than copied — two definitions of
      "when does a tempo change start a marker" would drift silently.
  - `app_state.py` — the one loaded library, and the **version-history commit**.
    History is app-level: the adapter returns the bytes it wrote plus a summary,
    and `AppState.save()` versions them. Every write path must go through it.
  - `library_id.py` — a **stable identity for a library that survives it being
    moved**. Everything else keys off the OS path (`history` hashes it, `prefs`
    stores it), which is fine for things a user shrugs at losing and NOT fine for
    export sets, which are curated work holding references into one library. Two
    halves, only the first authoritative: a hidden `.konduktor-library.json`
    **sidecar beside the library**, which moves with it, plus a disposable
    `libraries.json` index in app-data. The id is **random** — deriving it from
    the path defeats the point and from the contents would change on every save.
    The sidecar is keyed **by filename**, because one folder can hold two
    libraries and a single id per folder would hand both the same identity.
    A **removable** library gets no sidecar (writing to a user's USB stick for
    Konduktor's bookkeeping is not a trade worth making) and falls back to a
    path-derived id that `is_durable()` reports honestly. Writing beside the
    library is not a new intrusion — saves already create `backups/` there.
  - `exports.py` — **export sets**: a named, persisted slice of the library bound
    to a destination. Konduktor's OWN data, never written into the user's library
    (a Traktor collection has nowhere to put it, and it would live in a file
    Traktor rewrites). Three rules shape it: references are **LIVE** (a set
    stores playlist *ids*, resolved at export time, so `resolve()` is computed on
    every call and NEVER cached); a playlist is **removable only as a whole**
    (a live reference plus per-track removal needs an exclusion list, i.e. hidden
    state silently deciding what a future export contains); and it is **keyed by
    `library_id`**, not path. `resolve()` dedupes across playlists and loose
    tracks — one track, one file copy — and SURFACES what is gone rather than
    dropping it, because a gig stick quietly missing four tracks is this
    feature's worst failure. `retarget()` follows ids through a path remap, which
    `/api/library/remap-paths` calls with a before/after snapshot.
  - `prefs.py` — persisted user prefs (`userprefs.json` in the per-OS app-data
    dir). Last-opened collection (global AND per-platform), library column
    layout, prep-deck zoom, import destination. Best-effort.
  - `schemas.py` — HTTP request bodies + envelopes; re-exports `core.model`.
  - `main.py` — thin FastAPI routes (all under `/api`), talking only to the
    adapter. Starts **unloaded**; the library is chosen at runtime via
    `POST /api/library/open` (data routes 409 until then).
    `GET /api/capabilities` feeds the UI's gating; `GET /api/fs/list` powers the
    file browser; `GET`/`PATCH /api/prefs` persist UI prefs. Setting
    `KONDUKTOR_NML` auto-loads on startup (dev/tests).
    The picker's three routes are all **scoped by `?platform=`**, because it
    asks which platform first: `/api/platforms` (the pre-load equivalent of
    capabilities — reports `selects`, `removable` and `found`, sorted so a menu
    does not reshuffle between launches), `/api/library/options` (that
    platform's detections + its own last-opened) and `/api/fs/list` (that
    platform's suffixes; a directory-selecting platform contributes none, so its
    browse shows folders only) and `/api/fs/places` (the browser's sidebar —
    the user's folders, then drives, plus that platform's own location when it
    keeps one at a fixed path; a place that does not exist is **omitted, never
    disabled**, since a greyed-out Music folder reads as breakage where its
    absence reads as an accurate description of the machine).
    `/api/library/open` checks `exists()`, **not
    `is_file()`** — which shapes are valid is `can_open()`'s business, and a
    OneLibrary library is a drive. `prefs` keeps last-opened **per platform**:
    offering a Rekordbox `master.db` to someone who just chose Traktor is an
    offer that cannot be taken.
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
  - `components/` — `FileBrowser` (**the one file browser**: places rail + path
    bar + listing, `mode: 'file' | 'directory'`. Deliberately NOT the chrome or
    the confirm footer — its two callers frame it very differently, one as a step
    inside a full-screen card and one as a modal over the import dialog, and a
    shared wrapper would put a modal inside a modal. `path` is controlled,
    because the caller needs to know where you are standing to label its footer;
    `useFsListing()` is exported so the caller reads the RESOLVED path through
    the same query key rather than being told it by an effect. The rail
    **navigates, never selects** — one click there must not be able to open a
    library or commit an import destination. It polls, because drives come and
    go while a dialog is open, and the volatile Drives group is LAST so an
    appearing drive cannot shift the rows above a moving cursor),
    `ExportsSection` + `ExportDialog` (**exports in the sidebar** — each a
    named, persisted slice of the library bound to a destination. An export
    expands to show its referenced playlists, whose counts come from `contents`
    recomputed on every read, because the references are LIVE. Two deliberate
    omissions: a referenced playlist has **no per-track remove** — that would
    need an exclusion list, i.e. hidden state deciding future exports — and an
    export's views pass `TrackTable` **no `onRemove` / `reorder`** — in a
    playlist those act on the user's REAL playlist: same pixels, opposite
    meaning. Removal from an export is on the context menu, and only on its root
    view. Adding is not gated
    on the library being writable, since an export set is Konduktor's own data.
    `ExportDialog` shows unsupported targets **disabled with a reason** rather
    than hiding them), `CollectionPicker` (**two steps: which PLATFORM, then which
    library** — Automatic / Open last / Find manually, the last revealing a file
    browser. The platform comes first because every later answer depends on it:
    asked the other way round, "Automatic" had to guess ACROSS platforms and did
    it by adapter registration order, so a plugged-in stick could be opened as
    the user's collection. Scoping removes that by construction rather than by
    ranking candidates better. Nothing is remembered between launches — the
    platform list is where every session starts. `selects` decides whether the
    browser offers files or a folder to confirm, because a library is a file on
    some platforms and a DIRECTORY on others), `Sidebar` (a header naming the
    open library, which is also the only way to switch to another one — the
    picker used to be a one-way door with `forcePicker` never set, so changing
    library meant restarting; switching confirms first when there are unsaved
    edits, since the adapter holds them in a native model that opening another
    library replaces — plus the playlist tree +
    create/rename/delete), `SaveBar` (+ the settings gear beside it), `Toolbar` (search/filters; columns are chosen by right-clicking the table header),
    `TrackTable` (**the one track list** — All Tracks, playlists, exports and
    devices. TanStack Table + **virtualized** grid; per-row play button,
    configurable columns, header sorting, inline double-click editing, and
    **Finder-style row selection** — click / Cmd-Ctrl+click / Shift+click over
    the SORTED rows, ↑/↓ (Shift extends), Cmd/Ctrl+A, Esc; shortcuts stand down
    while an `aria-modal` dialog or `role="menu"` is open, so new dialogs need
    `aria-modal="true"`. Right-click acts on the whole selection; single-track
    items are hidden for a multi-selection. A view with a manual order passes
    `reorder` (drag rows — the whole selection moves as a block — with an
    insertion line and edge auto-scroll); `enabled` is false while a sort or
    filter is active, so a partial or re-sorted order can never overwrite the
    real entry list. `onRemove` wires Delete/Backspace; `positions` makes the #
    column show playlist positions. Playlists gate both on the node's own
    `can_reorder`, so smart playlists get neither. **Row reorder is hand-rolled,
    not dnd-kit sortable**: a SortableContext around the virtualizer loops on its
    flushSync-driven measurement — which is why playlists were once a separate,
    non-virtualized `PlaylistTable`. Fixed 44 px rows make the drop index just
    pointer-y / ROW_HEIGHT. dnd-kit still reorders the header's columns.)
    `AutoCueDialog` (the Auto Hotcues slot template: event + beat offset per
    slot, a per-slot Replace tick for occupied slots — never remembered, since
    overwriting is a decision about THIS track — and the template itself
    persisted as `autoCueTemplate` in userprefs),
    `ContextMenu` (supports `▸` submenus, section headings, separators and
    checkbox items that toggle without closing — the header row's right-click
    column chooser uses those; "Add to" lists
    playlists + exports and acts on the whole selection) + `EditTagsDialog`
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
  - **`capabilities.writable`** is the library-level gate, and it is deliberately
    NOT the same as every per-feature flag being false: "the platform has no such
    feature" and "this library cannot be written at all" look identical to a UI
    that only sees per-feature flags, and silently inert controls read as a bug.
    `readonly_cause` says which kind (`platform_incomplete` / `cloud_synced`) and
    `platformCopy.readOnlyNotice()` words it — the two need very different
    sentences, since one is a roadmap gap and the other a permanent refusal that
    protects the user's other machines. Gated on it: `SaveBar` (replaces the save
    button with the reason), `App`'s `onEditField` (inline edit + click-to-rate
    go inert), the "Edit Tags…" context item, `Sidebar`'s new-playlist button,
    and all 12 `PrepStrip` edit handlers (which report the reason rather than
    firing a request the adapter would refuse — the deck stays fully usable for
    listening, and shows a "Read-only" badge).
  - Capability-gated today: hotcue bank size (`HotcueBar`, the auto-cue guard and
  the digit shortcuts all read `cues.hotcue_slots`), the cue-type dropdown
  (`cues.types`), the rating scale (`tracks.rating_max`), the grid Lock button
  (`grid.lockable`). Per-node playlist flags (`can_rename`, `can_delete`,
  `can_add_tracks`) gate the sidebar and the context menu's "Add to" — each on its OWN flag,
  since a platform may allow renaming but not deleting. Carried but deliberately unused until a second adapter
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
  leaves no companion debris (I); **companion cues are ordinary editable
  hotcues** — move/retype/delete work and never touch the grid marker (J); and
  real flexible grids in the collection project correctly (K).
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
- `test_rekordbox_fidelity.py` — the **row-level analogue of the byte diff**: a
  full table dump before/after, compared row by row (a byte diff is meaningless
  against SQLite). Asserts a no-op save changes zero rows; a one-field edit
  changes exactly two — the track and `agentRegistry.localUpdateCount` — with the
  counter up by one and the edited row stamped with it; edits are invisible on
  disk until save; and playlist create/fill/rename/delete round-trip.
- `test_onelibrary_adapter.py` — the third adapter against the same contract.
  Unlike the Rekordbox tests it needs **nothing installed and nothing plugged
  in**: it runs against `fixtures/onelibrary/`, a real rekordbox 7 export trimmed
  to fixture size (its README says what was removed; the database is untouched).
  So the expected values are checked against bytes *rekordbox* wrote, not bytes
  Konduktor wrote. Pins the drive-relative path resolution, the `PCOB`/`PCO2`
  merge, and — cross-checked against the same cues in `master.db` — the dense
  ANLZ slot numbering, which is the thing most likely to be silently wrong.
- `test_import.py` — the import feature end to end: a drive's tracks into a temp
  copy of the real collection. Asserts the prep survives (hot cues keep their
  PAD, memory cues fill spare ones, the flexible grid crosses intact, no
  companion cue is invented), that the existing 8,485 entries render
  byte-identically, that the job registry runs/fails/cancels, and that a
  cancelled import leaves **no orphaned audio and an untouched collection**.
- `test_grid_detect.py` — beatgrid detection on **synthetic** audio, so the
  tempo and first beat are known rather than borrowed from another analyser:
  exact integer and non-integer BPMs, the 125 BPM the old detector could not
  return, octave choice, and the loud off-beat hat and syncopated bassline that
  each fooled one band on real music. Accuracy on REAL music is
  `bench_grid_detect.py`'s job (not in `run_tests.sh`: it needs audio).
- `test_auto_hotcues.py` — Auto Hotcues in three layers: `structure.analyse`
  on SYNTHETIC audio with known sections (the intro with a kick is not a drop;
  no breakdown after the last drop), `plan`'s outcomes and beat-counted offsets
  (across a tempo change), and the ROUTE end to end on a temp copy of the real
  collection — the previous implementation's route returned 500 on every call
  for a week while its helper's tests passed.
- `test_grid_batch.py` — **batch grid analysis** (context menu → Analyze Grid &
  BPM → `POST /api/tracks/grid/auto-batch`, a `jobs.py` job shown in the status
  bar). Pins the batch-only decisions: locked grids are skipped always, existing
  grids unless `replace_existing`, a failing track is reported not fatal, one
  run at a time, a cancel KEEPS finished tracks (unsaved edits like any other)
  and still returns its result, and opening another library cancels the run.
  Shares `_analyse_grid` with the deck's single-track Analyze.
- `test_picker.py` — the picker's routes. Pins the scoping, because the bug it
  replaced was invisible: `/api/library/options` flattened every driver's
  detections and returned `[0]`, so a plugged-in USB stick could be offered as
  the user's collection and nothing failed — it just opened the wrong library.
  Also pins that a drive is named the same whichever of its two valid paths was
  opened, that every offered place exists, that the boot volume is not offered
  as a drive, and that the adapter and the browser share ONE mount scanner. Needs nothing installed: every assertion is about the SHAPE of the
  answer, and prefs are redirected to a temp file so a test can never rewrite
  the user's last-opened library.
- `test_exports.py` — export sets against a temp copy of the real collection.
  Pins the things that would fail SILENTLY: a live reference picks up a playlist
  edited AFTER curation; contents dedupe across playlists and loose tracks; a
  deleted playlist or track is surfaced, not dropped; sets are per-library; two
  sets sharing a destination are caught (an export clears its destination, so the
  second would wipe the first); and deleting a set never touches its folder.
- `test_export.py` — writing a Traktor collection from NOTHING. There is no
  original here, so the guarantee differs from `test_save_fidelity.py`'s: the
  result must be a collection Traktor would have written. Pins the skeleton, and
  above all that **`<LOCATION>` is volume-relative** — an export writing host
  paths would work perfectly on the machine that made it and resolve to nothing
  at the gig. Also that a flexible multi-marker grid and hot-cue PADS survive,
  grids are not locked, loose tracks get their "Other" playlist, and re-exporting
  into the same folder replaces rather than appends.
  It then runs a real export end to end (the source library is built with the
  exporter itself, so its LOCATIONs point at audio that exists) and pins the
  safety rules with teeth: a folder Konduktor did NOT write is refused, and
  `run()` refuses a blocked plan even when called directly; a re-export replaces
  its own files and leaves the user's alone; a cancel leaves no library file and
  **no audio at all**, including the file being written; a missing source file is
  skipped rather than fatal.
- `test_export_pioneer.py` — the two Pioneer targets against the same contract.
  Harder than Traktor's in a way that test cannot cover: each writes a database
  PLUS per-track analysis files, so "the library" is no longer one path, and the
  prep survives a real change of representation rather than a re-render. Pins the
  three crossings that fail SILENTLY — the key is CONVERTED not copied, hot cues
  keep their PAD across the dense-vs-sparse slot difference, and a flexible
  multi-tempo grid survives expansion to per-beat and collapse back. Both are read
  back with Konduktor's own adapters, which is the strongest check short of the
  hardware: the reader was written against real rekordbox output.
- `test_library_id.py` — the property no other suite covers: **a library that
  moves keeps its identity**. Also that two libraries in one folder stay two, a
  removable library is never written beside, an unwritable location falls back
  rather than failing, and `paths.write_json` is atomic (curated user work, so
  `prefs.py`'s best-effort "any I/O error degrades to no prefs" is NOT adequate).
- `test_layering.py` — `core/` imports nothing platform-specific, and no adapter
  imports another platform's library (checked on real imports via AST, so merely
  naming a platform in a comment is fine). Rekordbox and OneLibrary deliberately
  **share** `pyrekordbox`: the rule is "no adapter reaches for a rival vendor's
  library", and a shared dependency is not a breach.

Also validate the backend interactively at `http://localhost:8000/docs` and the
frontend at `http://localhost:5173`.

## Critical gotchas — read these

1. **`traktor-nml-utils` MUST come from GitHub, not PyPI.** PyPI's `3.1.0` is
   stale and **cannot parse Traktor Pro 4 (NML v20)** files — its strict parser
   dies on the v4 `<GRID>` element inside `CUE_V2`. `requirements.txt` pins the
   GitHub `master` (v4.0.0). Do not "simplify" this to a plain PyPI pin.
   **`pyrekordbox` is now the same situation**: PyPI's latest (0.4.4) has no
   `devicelib_plus`, which is the OneLibrary (`exportLibrary.db`) reader, so
   `requirements.txt` pins the GitHub master. That upgrade renamed `db6` →
   `masterdb` and `tables` → `models` and moved `BLOB` out of `db6.database`;
   the Rekordbox adapter uses the new names. `db6` still works as a deprecated
   alias and is **removed in 0.6.0**.
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
    `place_grid_companion`, used only by Auto Grid). They are **not locked**:
    Traktor 4 keeps a beatgrid without one, so a companion is an ordinary hotcue
    the user may move, retype or delete — doing so simply ends the pairing
    (it is no longer a white cue on a marker), and the grid is untouched. Grid
    edits still drag or delete a companion that is still paired. The `#FFFFFF` test is
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
- 🟡 Rekordbox adapter — **read + metadata/playlist/hot-cue/beatgrid writes
  done** (milestones 1–3). A Rekordbox library opens, projects, browses and
  accepts track metadata, playlist, hot cue and beatgrid edits — including
  flexible multi-tempo grids, confirmed rendering correctly in Rekordbox 7 —
  verified row-by-row by
  `test_rekordbox_fidelity.py`, end to end through the API, and in Rekordbox 7
  itself. Research is captured in `.claude/handoffs/rekordbox-adapter.md` §8,
  incl. the verified result that Rekordbox accepts Konduktor-written rows when
  USNs are maintained. Remaining gap: **cover art**. **No version history on Rekordbox** — accepted scope decision,
  see handoff §11.
- ✅ **Import a OneLibrary stick into Traktor** — done end to end. A plugged-in
  drive appears under **Devices** in the sidebar, browses in the ordinary table
  and deck (audio streams off the stick so a track can be auditioned before
  importing), and imports into the loaded collection: audio copied, cues and
  flexible beatgrids translated, playlists recreated in a folder named after the
  drive. Verified in Traktor 4.5 itself. Three pieces worth knowing:
  - **`add_tracks` is the protocol's only CREATING verb** (`core/adapter.py`,
    `NewTrack`). Everywhere else the write target was parsed from a real file,
    which is what makes the byte-fidelity guarantee hold; here the rule becomes
    "an added entry must not perturb any existing one", which `test_import.py`
    checks on rendered bytes — exactly one line changes, the one carrying
    `<COLLECTION ENTRIES="N">`, whose count the store now maintains. The store
    creates a bare `ENTRY` and the cues/grid are written by **replaying the
    ordinary commands**, so an import inherits `replace_grid`'s companion
    handling and tests instead of growing a second implementation.
    `AUDIO_ID` and `LOUDNESS` are left empty — they are Traktor's own analysis
    outputs and cannot be computed; **verified that Traktor loads and plays such
    entries** and re-analyses them.
  - **`jobs.py` + `importer.py`** — the app's first long-running operation.
    Thread per job, polling, cooperative cancel; deliberately not asyncio or SSE.
    **Ordering is the safety property**: audio copies first and the library is
    written last, so a cancel leaves the collection untouched and removes what it
    copied (including the file being written — cleaning up only COMPLETED copies
    left debris the retry then suffixed around). Export will reuse this.
  - **The UI gates itself through capabilities, not conditions.** Browsing a
    device swaps the `CapabilitiesContext` for the device's own read-only set, so
    inline editing, Edit Tags, rating stars and all 12 deck handlers go inert
    with **no component learning what a device is**. The Sidebar sits outside
    that swap so `SaveBar` still saves collection edits. While a device's
    capabilities load the fallback is read-only, never the collection's —
    guessing "writable" for one frame would offer an edit bound for the wrong
    library. Lossiness follows the settled rule: memory cues become hot cues in
    spare pads (earliest first), cue colour is dropped (Traktor derives it from
    type).
- 🟡 OneLibrary adapter — **read-only, done**. A OneLibrary USB drive opens,
  projects, browses and searches: tracks, playlists, hot cues, memory cues, loops
  and flexible beatgrids. Built as the readable SOURCE half of a future
  **"import a OneLibrary stick into a Traktor library"** feature, which is the
  motivating use case. Everything unknown about the format is on the write side
  (the `cue` table's MPEG seek columns, the waveform tags a CDJ draws from, the
  update counters), so read-only is a deliberate boundary rather than an
  unfinished one. All research — including the schema, which has no public spec —
  is in [.claude/handoffs/onelibrary-adapter.md](.claude/handoffs/onelibrary-adapter.md).
- 🟡 **Export/conversion** — **in progress.** Both the design AND the user flow
  are settled; see
  [the export discussion doc](.claude/discussions/discuss-export-implementation-2026-09-22.md),
  whose 2026-09-23 section is the current one, plus
  [.claude/handoffs/export.md](.claude/handoffs/export.md) for the landmines.
  An **export** is a named, persisted object in the sidebar carrying its name,
  target platform and destination; tracks and playlists are added to it; an
  Export button copies the audio and writes the library. Settled: **Traktor-only
  target** for v1, source is whatever library is loaded, **mirror the source
  folder structure** (so filename collisions cannot happen), playlists are **live
  links removable only as a whole**, re-export **clears the destination first**,
  cancel **rolls back**, key notation **mirrors the source**, grids are **not
  locked**, no whole-library export.
  Verified against the real collection: Traktor stores `VOLUME="Hardy"` plus a
  **volume-relative** `DIR`, never a host path — so a USB export is portable by
  construction, and the `.nml` cannot be written until the destination is known.
  Also settled: loose tracks land in an **"Other" playlist**, playlist folders
  are preserved, and the export is a **standalone `collection.nml`** the user
  swaps into a Traktor install — so it needs the full skeleton, verified from the
  real file: `<NML VERSION="20">`, `<HEAD>`, `<COLLECTION ENTRIES>`,
  `<SETS ENTRIES="0">`, `<PLAYLISTS>` wrapping a `$ROOT` FOLDER node with a
  `SUBNODES COUNT`, and `<INDEXING><SORTING_INFO PATH="$COLLECTION">`. There is
  **no `<MUSICFOLDERS>` element**, so it is not required.
  **Safety**: "clear the destination first" plus "the user picks the destination"
  is a folder-deletion hazard, so an export writes a `.konduktor-export.json`
  manifest and **refuses to clear any folder that lacks one** — Konduktor only
  ever deletes what Konduktor wrote.
  **All three targets are now writable from nothing** (2026-09-23). The UI needed
  **no change at all** — `/api/export-targets` reads the exporter registry, so
  registering one is what makes a platform selectable. Three things were measured
  rather than assumed, each contradicting the handoff's expectation:
  - **ANLZ analysis files CAN be created from scratch.** The handoff called this
    unexplored and a likely blocker. `adapters/rekordbox/anlz_writer.py` builds
    them; both Pioneer targets need one per track. Two traps: `AnlzTag.content`
    is a construct **Switch** (pass the structure, not bytes), and the cue-entry
    structs `AnlzCuePoint`/`AnlzCuePoint2` **cannot be built at all** — each
    declares `"type"` twice, once as a magic `Const` and once as the cue kind, so
    parsing works (the second overwrites) and building cannot. Cue entries are
    therefore packed by hand, and the whole FILE is assembled here too, because
    `AnlzFile.build()` re-builds every tag through those same structs.
  - **`Base.metadata.create_all()` does NOT reproduce Rekordbox's schema.** The
    ORM models 37 tables where a real `master.db` has 47, and marks columns NOT
    NULL that Rekordbox leaves nullable (`agentRegistry.id_1` caught it). So
    `fixtures/rekordbox/schema.sql` is the real DDL, plus `seed.sql` for the
    scaffolding rows `add_content` requires — `djmdDevice`/`djmdProperty` are
    deliberately NOT in the fixture, since they carry the machine's name and
    UUIDs; those are generated fresh per export.
  - **The two Pioneer key blobs are different.** `masterdb`'s key and
    `devicelib_plus`'s are not the same string; mixing them up yields a database
    Rekordbox cannot open.
  `adapters/onelibrary/export.py` writes a whole drive (`PIONEER/rekordbox/
  exportLibrary.db` from the checked-in DDL, plus ANLZ under `PIONEER/USBANLZ/`,
  paths drive-relative). `adapters/rekordbox/export.py` writes a `master.db` and
  **replays cues through the ordinary `RekordboxAdapter.set_cue`**, so it inherits
  `_sync_content_cue`, the sparse `Kind` bank and their tests rather than
  redefining them; its ANLZ files go under a **`share/` directory beside
  `master.db`**, which is what `AnalysisDataPath` is rooted at — write them at the
  library root and the grid silently reads back empty.
  **Key notation crosses via the wheel**, never by copying the string:
  `projection.render_key()` is the inverse of `parse_key`, shared by both Pioneer
  targets, so Traktor's `"10m"` becomes `"Cm"` rather than a literal `"10m"` in a
  Pioneer library. **Caveat**: `djmdContent.FolderPath` is an ABSOLUTE host path,
  so unlike Traktor and OneLibrary a Rekordbox export is not portable by copying
  the folder. Still unwritten and documented as unknown: OneLibrary's waveform
  tags and the `cue` table's MPEG seek columns.
  **Step 5 done**: `exporter.py` — plan, mirrored copy, manifest, rollback — on
  `jobs.py`, plus `POST /api/exports/{id}/preview` and `/run`, and the
  `ExportRunDialog`. Three properties it is built around: audio is copied FIRST
  and the library written LAST, so a cancel leaves **no** library file and its
  absence is what marks an export unfinished; rollback registers each file
  BEFORE writing it, so the in-flight copy is cleaned up too; and the **manifest**
  (`.konduktor-export.json`) means clearing removes exactly what the last export
  wrote, so a file the user put in the folder survives a re-export. Audio mirrors
  the source tree **relative to `common_dir_prefix`** — mirroring absolute paths
  would put the user's home directory on the stick, and relative paths make
  filename collisions impossible. A destination Konduktor did not write is
  **refused, not confirmed**: there is no "do it anyway".
  **Step 4 done**: `core/export.py` (the `LibraryExporter` protocol, its own
  registry, and the strictly-generic `ExportPayload` — so a Rekordbox collection
  exports to Traktor with no exporter knowing Rekordbox exists) and
  `adapters/traktor/export.py`. The writer **creates then replays**: it writes a
  minimal valid NML skeleton, opens a `TraktorAdapter` on it and populates it
  with the ordinary commands. That amends the original design note, which
  rejected create-then-replay on the grounds that the write target must always be
  a native model parsed from a real file — here it literally is, a skeleton
  Konduktor just wrote, and the alternative was a second definition of "Track +
  cues + grid → ENTRY" that would drift from `add_entry` and inherit none of its
  tests. Exporters declare **static** capabilities: no instance, no path, because
  the library being described does not exist yet.
  **Steps 1–3 done**: `library_id.py`; `exports.py` + 10 routes + `api.ts`; and
  the UI (`ExportsSection`, `ExportDialog`, export views in `App`, "Add to" on
  the track context menu and each sidebar playlist row).
  Next: `core/exporter.py` + the Traktor writer, and the export run itself on top
  of `jobs.py` and `importer.py`'s copy machinery — **there is no Export button
  yet**, since nothing can be written. Note the OneLibrary work already demonstrated
  creating an `exportLibrary.db` from nothing, and `fixtures/onelibrary/schema.sql`
  is the DDL to do it with.
- ⬜ Serato adapter
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
  integers below.) A grid marker's companion cue is an ordinary hotcue to these
  commands (see "Beatgrid"). Beatgrid commands are marker-level:
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
  `GET /api/tracks/cues`, `POST/PATCH/DELETE /api/tracks/cue`,
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
