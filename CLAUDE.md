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
    - `relocate.py` — **auto path remapping's search** (see `relocation.py`).
      Fixes ONE thing: a stored prefix changed and the layout under it did not
      (a collection built on Windows with the SSD as `X:`, opened on a Mac where
      it is `/Volumes/MiniSSD` — the real case, 8,359 of 8,365 found). It
      SAMPLES ~50 tracks per group against each candidate root (drives, home,
      Music), dropping leading folders at the shallowest level that hits, and
      verifies the winner over the WHOLE group — it never walks a drive. At
      least one folder must survive the drop (a bare filename at a drive root
      is a coincidence), and a runner-up resolving ≥50% of the winner's count
      makes the group **ambiguous** — a drive and its backup clone are a
      question, not a ranking. Which groups to search is the adapter's call
      (`unresolved_path_groups()`): only volumes in which **NO** track resolves,
      so a few deleted files never prompt.
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
      base (libsndfile = CoreAudio to the sample on MP3). **Rekordbox's and
      Traktor's clocks both differ from it, per FILE** (an MP3's header, an
      M4A's edit list), and are deliberately NOT corrected here but at each
      adapter boundary — `adapters/rekordbox/timebase.py`,
      `adapters/traktor/timebase.py`.
      `backend/bench_grid_detect.py` scores it against any library's
      single-marker grids and reports that constant separately from detection
      error (29 Rekordbox references: old 1/29 BPMs, new 27/29).
    - `stem_file.py` + `tag_copy.py` — **writing a native-instruments STEM
      file** (Convert to Stems). An `ipod` MP4 with five AAC stereo 44.1 kHz
      streams (mix, drums, bass, synths, vox), streams 1-4 `disposition` 0,
      muxed IN LOCKSTEP, plus a `moov/udta/stem` JSON box appended in place
      (moov is last, so only two sizes change). The separation is a CALLBACK —
      the backend never imports torch; the engine (below) supplies it, the
      tests a fake. Measured traps: FFmpeg's resampler upmixes mono at -3 dB,
      so mono is duplicated by hand; resampling does not move time; both
      encoders (`aac_at` on macOS, `aac` elsewhere) are timing-exact but decode
      up to ~2 frames LONGER, so length checks allow 0..2048 samples and the
      duration reported is the source's; AAC pre-echo fools threshold onsets,
      so alignment is checked by cross-correlation. **Two JSON literals**: the
      box exactly as the commercial files hold it and `<STEMS>` exactly as
      Traktor writes it — they differ in key order AND float digits (Traktor's
      16 digits do not round-trip the same double; both are the same float32),
      so neither is derived from the other. `tag_copy` carries every canonical
      tag (ID3 / Vorbis / MP4 -> MP4, incl. `tmpo` and freeform `initialkey`,
      the atoms the user's commercial stems use) and the cover, opening the
      target as `MP4` explicitly because the batch writes `*.konduktor-partial`
      files that `audio_tags.write_cover` would refuse by suffix.
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
      `is_os_housekeeping()` names what an OS writes into a folder by itself
      (`.Spotlight-V100`, `.Trashes`, `._*`, `System Volume Information`, …), so
      the export's "is this folder empty?" check does not refuse a stick's root
      just because a Mac has mounted it once. `drives()` splits drives into
      `local` (the boot disk at `/`, never its `/Volumes` alias, plus internal
      drives per `diskutil`, cached per mount) and `external` — **anything
      uncertain is external**, so a stick is never presented as a safe place to
      reference tracks from; mounted `.dmg`s are dropped. `is_hidden()` reads
      macOS's hidden FLAG (as Finder does), not a list of names.
    - `folder.py` — **a folder of loose audio files read as a read-only
      source** (`FolderSource`), for the sidebar's Files trees. NOT a
      registered driver: `can_open("any directory")` would make every folder a
      library to the picker. **This folder only** (the tree reaches
      subfolders), **track id = absolute path**, tags read once per folder
      mtime by `FolderScanner` — which is also the audio route's gatekeeper:
      only a file a scan found is streamed, so `/api/folder/tracks/audio` cannot
      read an arbitrary path. Tagged keys are deliberately dropped (every
      notation there is). The listed suffixes are the collection's
      `tracks.audio_formats` capability. A stem file lists as
      `media_kind: "stem"` (its `stem` box, read at scan time).
  - `adapters/traktor/` — everything that knows NML exists.
    - `store.py` (`TraktorStore`) — the retained native model: owns the parsed
      dataclass NML, applies every edit, renders + saves. See "Write path".
      **`apply_stem_swaps`** is Convert to Stems' library side (generic
      `StemSwap` / `StemSwapResult` in `core/adapter.py`, gated on
      `tracks.stem_convertible`; Rekordbox/OneLibrary refuse): all swaps are
      validated before any applies. "repoint" moves the entry to the stem file
      (id changes; playlists follow as `TYPE="STEM"`); "add" deep-copies it
      (plus its pending tag fields and staged art, so Save still writes them to
      the new file). Either sets `<STEMS>` (the literal from `core/stem_file`),
      INFO bitrate/playtime/playtime_float/filesize(KB), VOLUMEID only where it
      still names the volume, and KEEPS AUDIO_ID (`_KEEP_AUDIO_ID_ON_STEM`:
      measured in Traktor 4.5 — cleared, Traktor re-analyses on load and
      re-measures a hand-set grid's BPM; kept, it touches nothing). **Every START shifts by the time-base
      difference**, the old offset read from `StemSwap.original_audio` — the
      original's bytes where they are NOW (parked, under a non-audio name, so
      `timebase.offset_ms(..., suffix=)` takes the original's suffix). Read
      from the old path it would be 0 and every cue ~51 ms late
      (`test_stem_swap.py` pins that failure). An anchor pushed below 0 moves
      forward by whole beats with its companion; a hotcue there clamps to 0 and
      is reported. The journal records scope `stem` (not `track/add`, which
      would exclude the entry from the tag sync).
    - `adapter.py` (`TraktorAdapter`) — owns the store **and** the `TrackIndex`
      built from it, so projection and native model cannot drift. Translates the
      generic cue vocabulary to Traktor's integers (the only place that mapping
      exists). **Every mutating command returns its refreshed projection** — a
      forgotten refresh would be a silently stale UI no byte test would catch.
    - `projection.py` — native → generic (`to_track`, `to_track_cues`).
    - `driver.py`/`discovery.py` — `can_open`, default-location detection, restore.
    - `beatgrid.py`, `locations.py`, `capabilities.py` — Traktor's grid/companion
      rules, LOCATION ↔ OS path conversion, and its capability set.
    - `timebase.py` — **Traktor's clock vs the decoded audio.** Traktor decodes
      MP3 naively (ignores the LAME gapless fields, decodes the Xing/Info
      header frame as audio), so on an MP3 WITH a header every stored position
      sits `samples_per_frame + encoder_delay + 529` samples (2257 = ~51 ms at
      44.1 kHz, 47 ms at 48 kHz) later than the decoded audio; header-less
      MP3s and every other format are 0. Measured in Traktor 4.5 with blinded
      cues in both directions (kit 4, in the stem-conversion discussion log).
      The store ADDS it at its four seconds→START writes (`time_offset_ms`),
      `projection.to_track_cues` SUBTRACTS it, and a read is **never clamped
      at 0**: Traktor anchors grids inside the header frame, and clamping would
      move them on the next write-back (`buildBeatGrid` keeps negative starts
      for the same reason). A file that cannot be read gets 0 and is not
      cached. The header itself is read by `core/mp3_gapless.py`, which skips
      the ID3v2 tag first — a check that did not once misread every real MP3
      as header-less.
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
      **Hot cue colour is writable** (`set_cue_color`, `PATCH
      /api/tracks/cue/color`) from `palette.SWATCHES` — the 15 MEASURED swatches
      of rekordbox's 16 (the teal-green is left out, not guessed); a pick must
      be an exact swatch (`swatch_code`), unlike the exporter's nearest-hue
      `code_for`. Not a breach of the promotion rule: Rekordbox, OneLibrary and
      Serato all colour cues — Traktor is the odd one out, by type only, so it
      reports `color="none"`. **The colour belongs to the PAD**: `set_cue` with
      no colour keeps the slot's existing code, so rename, type change and an
      Auto Hotcues Replace keep it; only Delete or "Default" clear it. Both
      columns go through `RekordboxStore._apply_color` (coloured: code +
      `Color=-1`; uncoloured loop: `255/0`; uncoloured cue: `-1/NULL`). An
      unknown code projects as no colour but stays on the row. Uncoloured cues
      still DRAW in Konduktor's type colours, not rekordbox's green/orange
      defaults (decided 2026-10-01: those defaults are unmeasured).
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
  - `adapters/onelibrary/` — **editable when opened as THE library; read-only
    when browsed from the sidebar's Devices** (`read_only=True`, cause `browsing`).
    The cross-vendor USB export format (AlphaTheta + Algoriddim + Native
    Instruments), read by CDJ-class hardware. Editing is landing in steps toward
    parity with the Rekordbox adapter — track metadata and playlists done; cues,
    grid, adding/removing tracks and the `export.pdb` rebuild next — decided in
    [the editing discussion](.claude/discussions/discuss-onelibrary-editing-2026-10-01.md),
    which also holds what rekordbox 7 was MEASURED writing when it edits a stick
    (no update counter moves, the `cue` table stays empty, a new playlist goes on
    top). **The store never opens the database ON the drive**: it works on a copy
    in app-data and Save writes it back whole — fingerprint check (refuses if
    another app wrote the stick since open), backup to app-data, temp file beside
    the database, stale `-wal`/`-shm` deleted, rename — then writes edited text
    fields into the audio files' tags as rekordbox does (not the rating). Cue tags
    are read by Konduktor's own `rekordbox/anlz_file.py`, NOT pyrekordbox: rekordbox
    writes COMPACT cue entries when it edits (48 / 44 bytes, export: 88), and
    pyrekordbox's parser raised on the 44-byte one and lost the whole `.EXT`.
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
  - **A stem file ADDED to a Traktor collection** (folder add, import, the
    Traktor exporter — all `TraktorStore.add_entry`) gets `<STEMS>` rendered
    from its OWN box by `stem_file.traktor_stems_json` (keys sorted, compact,
    whole floats as integers, else 16 significant digits) — measured to give
    Traktor's value for every local stem file and to re-render all 3,678 in the
    big collection unchanged, though every one of those is NI's default
    layout. Without it the entry read as plain audio (Type column, playlist
    `TYPE="TRACK"`) while the deck, which reads the file, played stems.
    Rekordbox and OneLibrary have no such element, so their projections take
    `media_kind` from the FILE (`stem_file.is_stem_file`, only `.m4a`/`.mp4`
    opened, ~0.7 ms each, cached per path in `RekordboxStore.is_stem`) — the
    same mismatch otherwise: Type said audio while the deck played stems.
  - `importer.py` also serves the folder add: `reference=True` adds each file
    where it is (nothing to copy or roll back), and every planned file the
    destination already points at (matched on `audio_path`, never the display
    `filepath`) maps to its EXISTING id instead of being added again.
    **`PrepStrip` takes `origin`** (`collection`/`device`/`folder`), recorded
    with the deck's track in `App` — the deck follows its TRACK's library, not
    the view's, so browsing elsewhere cannot point it at endpoints that do not
    know the track. `trackAudioUrl`/`trackCuesFor` in `api.ts` are the one place
    that choice is made.
  - `stems/` — Convert to Stems around the file itself. `engine_manager.py`:
    the engine version THIS build needs (`engine.json`) and the pinned weights
    (`weights.json`, a copy of `engine/weights.json` a test keeps equal).
    Installs under app-data (Windows: a SHORT `%LOCALAPPDATA%\Konduktor\engine`
    for the 260-char limit); an install counts only after every part's hash,
    the archive's, AND the unpacked engine starting and naming the expected
    version (`probe`) — `installed.json` is written last. One engine at a time;
    CUDA offered only for driver r580+ and capability 7.5+ (`nvidia-smi`).
    Side-loading (offline) verifies against the release manifest, cached once
    seen. `download.py`: resumable (`Range`, restarts if ignored), SHA-verified,
    certifi TLS (a frozen macOS Python has no CA store). `engine_process.py`:
    one `serve` per batch — progress, cancel-by-message, crash reported with
    the engine's stderr tail, raw work files deleted as read, a Windows Job
    Object and `keep_awake` (caffeinate / SetThreadExecutionState). Routes:
    `GET /api/stems/engine`, `POST .../install` (a `stem-engine-install` job in
    bytes, engine + weights), `POST .../sideload`, `DELETE`.
    **`convert.py` — the batch** (`POST /api/tracks/stems/preview` and
    `/convert`, a `stem-conversion` job in `BATCH_JOBS`, exclusive with the
    engine install and import). The collection is UNTOUCHED while tracks
    convert (hours, on a big batch): each becomes a verified
    `<target>.konduktor-partial`, so edits made meanwhile carry over and Cancel
    only deletes new files. One short END STEP under `STATE.mutation` then
    publishes each (a rename that refuses to overwrite: link+unlink on POSIX),
    parks each original in Replace mode (`.<name>.<ext>.konduktor-parked`,
    same folder, hidden on Windows, retried while Windows holds it open) and
    swaps every entry at once. Skips (reported, not failed): already a stem file
    BY CONTENT, missing file, target exists (never overwritten), two sources
    onto one (case-folded) target, a target the collection names; disk space is
    preflighted per volume (both copies coexist until Save) and for the work
    folder. Replace implies repoint; destination mode mirrors the tree (full
    path, volume first, when the tracks share no folder) and may repoint or add
    (+ existing / new playlist). **`pending.py` — the ledger** of conversions
    awaiting Save (per library id, atomic): `AppState._save` COMMITS (deletes
    parked originals, retargets export sets — inside `_save`, so import and
    remap saves count too), `discard()` and opening another library RESTORE,
    and `_open` RECOVERS a crash before the missing-files check (parked
    originals would look missing): each item decided by whether the SAVED
    library names the stem file. Every step works from what is on disk, never
    the recorded state alone; only a stem file the batch PUBLISHED is ever
    deleted; an uncommitted verified stem file is kept under its partial name
    and REUSED by the next batch converting the same (unchanged) source.
    While conversions are pending or running, remap-paths, path mapping,
    relocation, import, folder add, history restore and reload answer 409
    (`_require_no_pending_stems`): each would save or drop the swap.
    `/api/state` reports `pending_stems`, `stem_job`, `stem_recovery`.
    **`stream_cache.py` — stem PLAYBACK.** A browser decodes only an MP4's
    FIRST audio stream (the mix), so each stem is served as a file of its own:
    the three audio routes (collection / `source` / `folder`) take
    `?stem=0..3`, and `/api/…/tracks/stems` report `[{name, color}]` read from
    the FILE (`stem_file.stem_layout`: the box, and enough streams — never the
    library's `<STEMS>`, which plain MP3s carry). `stem_file.extract_stream`
    COPIES packets into an `ipod` MP4 — measured bit-identical to decoding the
    stream in place, edit list and priming (1024 / 2112) intact, on commercial
    stems too, so stems stay sample-locked to the mix and the cues. Cached in
    app-data keyed by path + size + mtime, LRU under 2 GB, written partial →
    rename; ~0.1 s per stem uncached.
  - `app_state.py` — the one loaded library, and the **version-history commit**.
    **`discard()`** (`POST /api/discard`, SaveBar's "Discard changes…", the
    quit prompt's Discard) drops every unsaved edit through the adapter's own
    `reload`, NOT `open` — reopening would re-run the missing-files check and
    forget this session's relocation answers. The route stops (and WAITS for)
    running batches first, as library open does (`_stop_batches`), or a batch
    would keep writing into the reloaded library. Rekordbox's reload is a real
    discard (`RekordboxStore.discard`): session rolled back and closed with its
    engine, buffered ANLZ grids and journal cleared — before, it opened a second
    connection and stayed "dirty". A reentrant `mutation` lock serialises
    open / save / discard (and, next, a stem batch's swap step).
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
    to a destination and **one or more target platforms** (`targets`, never
    empty; a set saved with the old single `target` loads as a one-item list).
    The targets share the destination and ONE copy of the audio — their layouts
    (`collection.nml`; `PIONEER/…`; `master.db` + `share/…`) do not collide. Konduktor's OWN data, never written into the user's library
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
  - `relocation.py` — the **open-time missing-files check**. `AppState.open()`
    starts a `RelocationCheck` thread (after the saved mapping is applied, so
    a volume it fixes is not asked about); `GET /api/library/relocation`
    serves the proposals until `POST` answers them (no mappings = Not now),
    then serves nothing — "ask once per open" lives here, not in the UI,
    which would re-ask on every refetch. Answers are **session-only**
    (`set_session_mappings`, applied in `TraktorStore._resolve` BENEATH the
    saved mapping and only for a file not found as stored): never written to
    the library, which would change every Traktor track id, nor to prefs, so
    a reopen asks again. Only a mapping the search proposed is accepted.
    **Traktor only for now**; Rekordbox/OneLibrary return no groups.
    `RelocateDialog` always asks — found volumes ticked, ambiguous ones a
    radio with nothing preselected, missing ones "Not connected" with a
    "Map by hand…" link to `PathMappingDialog`; no backdrop-click dismiss.
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
- **`engine/`** — the **stem engine**, a separate program (`konduktor_engine`,
  Python 3.12, torch 2.14 + demucs 4.1 pinned in `engine/requirements.txt`)
  that imports nothing from `konduktor`, built and released on its OWN channel.
  - `core.py` loads htdemucs_ft from a LOCAL weights folder — never online: the
    weights' licence rules out shipping them, so the app downloads them, pinned
    by revision + SHA-256 in `engine/weights.json` (`fetch_weights.py` for
    CI/dev). Seeded — and demucs draws its random shift from PYTHON's `random`,
    not torch's, so both are seeded or no two runs match.
  - `serve.py`: one process per batch, the model loaded once; JSON requests on
    stdin, JSON-line replies on a private copy of stdout (`sys.stdout` points
    at stderr, since torch prints), one lock for writes from two threads.
    A reader THREAD ends the process the moment stdin closes — the backend dies
    without killing children, and torch keeps the main thread busy. `cancel`
    raises from the progress callback and keeps the model loaded; CUDA OOM
    falls back to CPU for that track; the process lowers its own priority.
    `info` reports the CUDA card, capability and the build's arch list, which
    is how the app decides whether to offer the CUDA engine.
  - Frozen as a PyInstaller **onedir** (`konduktor-engine.spec`; onefile would
    re-extract ~500 MB per launch), `torch/include` and the HF client dropped:
    macOS arm64 = 164 MB download / 550 MB unpacked, runs after a tar round
    trip (ad-hoc signature intact). `test_engine.py` pins the protocol
    (reproducible seed, cancel, stdin-EOF exit) and, with `--frozen`, that the
    frozen build separates BIT-FOR-BIT like the unfrozen code (CPU).
  - `package.py` → tar.gz (zip loses exec bits on macOS), split under GitHub's
    2 GiB asset cap, per-part SHA-256; symlinks are archived as links and not
    double-counted. `.github/workflows/engine.yml` builds macos-arm64,
    windows-x64-cpu and windows-x64-cuda (torch cu130: Turing–Blackwell,
    driver r580+; older cards get the CPU engine), runs the golden test on CPU
    (runners have no GPU), checks Windows path length, and on an `engine-v*`
    tag publishes `prerelease` / NOT latest with an `engine-manifest.json`.
  - `backend/dev_stems.py` (dev only) converts files with the real writer
    through an unfrozen engine, and can swap them into a collection.
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
    than hiding them), `DevicesSection` (**every drive**, polled from
    `/api/fs/drives`: external ones under Devices, the computer's own under an
    expandable Local Storage. A drive carrying a OneLibrary library opens as a
    device as before, and EVERY drive has a lazily-fetched **Files** tree;
    clicking a folder is the `folder` source kind — its audio in the ordinary
    table, read-only via `readonly_cause: 'not_in_library'`, with files the
    collection already holds checked off in the # column (`TrackTable`'s
    `marked`). Capped at 40% of the sidebar and self-scrolling so a deep tree
    gives way before the playlists do) + `AddFilesDialog` (copy vs. leave in
    place is **asked every time with nothing preselected** — the answer depends
    on the drive — and warns that a referenced file on an external drive goes
    missing on unplug. A playlist/export target adds to the collection first
    and says so; it runs through `importer.run(reference=…, into_playlist=…)`),
    `CollectionPicker` (**two steps: which PLATFORM, then which
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
    library replaces — plus the playlist tree. **Creating is right-click
    only**: a folder's menu offers New Playlist / New Folder inside it, empty
    space below the rows creates at the top level, and the new node is named
    in place (Esc or an empty name creates nothing). Rename/Delete are on the
    same menu and the row's hover buttons, which share one set of actions.
    A Traktor folder's id is its PATH of names, so the `PATCH`/`DELETE`
    playlist routes take a `:path` param, a folder name may not contain `/`,
    and renaming onto a sibling folder's name is refused), `QuitGuard` (mounted in `main.tsx`: the desktop shell PREVENTS window close /
    Cmd+Q and emits `konduktor://quit-requested`; this acks it (`quit_ack` — the
    shell quits anyway after ~2 s without one, so a hung page cannot trap the
    user), quits (`quit_now`) when nothing is unsaved, else asks Save · Discard
    · Cancel; the sidecar is killed only at `RunEvent::Exit`, after the answer;
    browser dev gets `beforeunload`), `SaveBar` (+ "Discard changes…" when
    dirty, and the settings gear beside it, whose menu opens `ShortcutsDialog` — the shortcut list is written out BY HAND, so a new or changed shortcut in `PrepStrip` / `TrackTable` must be added there too), `Toolbar` (search/filters; columns are chosen by right-clicking the table header),
    `TrackTable` (**the one track list** — All Tracks, playlists, exports and
    devices. TanStack Table + **virtualized** grid; per-row play button,
    configurable columns, header sorting, inline double-click editing, and
    **Finder-style row selection** — click / Cmd-Ctrl+click / Shift+click over
    the SORTED rows, ↑/↓ (Shift extends), Cmd/Ctrl+A, Esc, Enter (load + play
    the highlighted row, winning over a focused button); shortcuts stand down
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
    `ConvertStemsDialog` (**Convert to Stems**, one dialog — decided: Where =
    Replace the originals (its option STATES that Save deletes them, since
    the choice is remembered and may be preselected) or Save to a folder; In
    the collection = point at the stem file / add as new tracks + playlist
    (folder mode only); a live preview from `POST /api/tracks/stems/preview`
    — N to convert, skipped ▸ with reasons, a time estimate from measured
    real-time factors, space; the engine download INLINE, offering the GPU
    engine when `nvidia` is set, which keeps running if the dialog closes
    (App shows it in the status bar). Every option persists as `stemConvert`),
    `StemReportDialog` (the Details behind a finished run's toast — every
    skipped/failed track with its reason), `StemsSettings` (Settings → Stems:
    engine status / download / install from a folder / remove, compute device
    `stemDevice`, the remembered defaults, originals awaiting Save). App
    follows a finished run's `{old: new}` ids with the deck and the selection,
    picks a running conversion back up from `/api/state.stem_job` after a
    reload, and reports `stem_recovery` once. The menu item is shown DISABLED
    with a reason (`MenuItem.title` — rendered `aria-disabled`, not
    `disabled`, since browsers show no tooltip on a disabled button) where the
    platform cannot play stem files as stems. SaveBar adds "Saving deletes N
    original files" while conversions are pending (decided: the consequence is
    shown where Save is pressed). `Toast` takes an optional `action`;
    `StatusJob.unit: 'bytes'` shows a download in MB. Every background job's status-bar entry reads label + count · a bar SPLIT horizontally when the job reports per-item progress (top = the current item's `fraction`, 0-100 %; bottom = the whole job, (done + fraction) / total, so 99 % into the first of two tracks reads ~50 %) — a single bar otherwise; two colours in one bar was tried and read as confusing · its `status` ("separating 41 %") at its natural width · the item's name LAST (the only part that truncates; full name on hover). The section is a FIXED width (560 px; the view name on the left gives way instead) and everything BEFORE the bar is fixed (count sized for its largest value, "cancelling…" as a status, not a label), so neither a long name nor a phase change moves the bar. The status deliberately has no fixed slot: it is after the bar, and a slot left a gap before the name. `Job.fraction`/`status` come from `JobHandle.progress(fraction=, status=)` and reset whenever `done` advances; a stem conversion maps decode / separate / encode onto a track's 0-1 by measured shares.
    `AutoCueDialog` (the Auto Hotcues slot template: event + beat offset per
    slot, a per-slot Replace tick for occupied slots — never remembered, since
    overwriting is a decision about THIS track — and the template itself
    persisted as `autoCueTemplate` in userprefs. Also has a **batch mode** for
    the context menu: same template, but it starts a job via `onRun`, and each
    tick reads "Replace" and applies to every track in the batch. A Replace
    RENAMES the slot's cue — `set_hotcue` keeps the old name only when called
    without one, which is what a hand move does),
    `ContextMenu` (supports `▸` submenus, section headings, separators and
    checkbox items that toggle without closing — the header row's right-click
    column chooser uses those; "Add to" lists
    playlists + exports and acts on the whole selection; "Remove ▸" holds
    From this playlist / From this export / Grids / Hotcues / From collection,
    each behind a `ConfirmDialog` — as is the playlist Delete key. Enter
    confirms unless a button has focus, so Tab-to-Cancel + Enter cancels) + `EditTagsDialog`
    (right-click → multi-field metadata + album-art edit), `StatusBar`,
    `RatingStars` (read-only, or click-to-set when given `onChange`), `Toast`,
    `UpdateDialog` (`UpdateCheck`, mounted in `main.tsx` beside `App` so it shows
    even on the picker screen: on startup lists recent GitHub releases and takes the
    newest non-draft, non-prerelease `v…` tag — NOT `/releases/latest`, since the
    stem engine publishes `engine-v*` releases in the same repo — and,
    if its tag's **semver part** is newer than `__APP_VERSION__` — build-number
    `-b<N>` bumps don't count — shows an update dialog with the release's
    "What's Changed" bullets; Download opens the release page in the system
    browser via `@tauri-apps/plugin-shell` `open` — falls back to `window.open`
    outside Tauri; "Skip this version" persists `skippedUpdateVersion` to
    userprefs via `/api/prefs`; all failures are silent).
  - **Stem playback** (decided 2026-09-30): a stem FILE always loads its four
    stems (`trackStemsFor` / `trackStemUrl` in `api.ts`) and the deck plays
    their SUM, as Traktor's stem deck does — the mix stream is never decoded,
    which also saves a full-length buffer. `PlaybackEngine.load` takes several
    buffers: one source + gain each, all started at ONE scheduled context
    time (so they stay sample-locked through seeks and loops), mute/solo as
    ramped gains, never a restart. `ScratchEngine.loadStems` keeps ONE summed
    copy of the audible stems (not four), re-summed in slices on a mute change.
    `analyzeStems` returns the overview's three-band columns (of the sum) plus
    one amplitude lane per stem, each normalised to its own 99.5th percentile
    but never below 5% of the loudest (a silent stem stays silent).
    `MainWaveform` draws the lanes in the file's own colours with a mute
    button at each lane's left edge (click = mute, Alt/Option-click = solo);
    Q W E R mute stems 1-4, Shift+ solos. **Solo is not a mode**: it SETS the
    mutes (every other stem muted), so each stem stays freely mutable after
    it; soloing the one stem still playing unmutes all. Mutes reset per track. Memory is
    the cost: ~140 MB per stem for 6 minutes, i.e. the same four buffers as
    the file holds streams, and no more.
  - **Prep strip** (DJ deck across the top of the window): `PrepStrip` owns it,
    in three rows. **Header**: cover, title/artist · album · genre, a Read-only
    badge, the readout well (ELAPSED / REMAIN to tenths, KEY, and `BpmReadout` —
    click to type an exact BPM; with no grid, a typed or tapped BPM CREATES one at the playhead, and
    Set grid on a track with no BPM opens the readout to ask rather than failing), SNAP, Analyze, Auto hotcues. **Waveforms**:
    `MainWaveform` (scrolling; zoom +/− in its corner and Cmd/Ctrl+scroll, owned
    by `PrepStrip` so it survives track switches and persists) over
    `OverviewWaveform`, which frames the main view's visible range around the
    playhead (it takes `secPerView` for exactly that). **Controls**: play, CUE, `LoopControls`, then EITHER
    `HotcueBar` OR — in **Grid mode** — `GridEditStrip`, then `TempoControls`
    (±0.01 / ±0.25, ÷2 ×2, tap, lock) and the Grid toggle. The row is a
    **container** and gives way in stages instead of overlapping: each pad
    queries its own width and drops its name below 4rem (the bank never goes
    below `PAD_MIN` per pad), then the tempo group folds into `TempoFold`'s
    portalled popover, then Grid keeps only its icon. The two row breakpoints
    are measured widths — move them if controls are added to the row. Every grid control acts
    on the marker **governing the playhead**, since a beatgrid is a marker list
    and there is no separate marker selection; see "Beatgrid" below.
    - **One size for loops AND beat jump** (`beatSize`, 1/32–32, persisted as
      `beatSize`; the old `beatJumpBeats` pref is read once on upgrade).
      `LoopControls` has a BEAT / MAN switch: BEAT shows `− size +` (the size
      key sets or clears a loop of that length; −/+ halve/double, resizing an
      engaged beat loop from its locked start), MAN shows IN / OUT / toggle.
      The armed IN point is React STATE (`loopInPoint`), not a ref, because the
      IN button (lit "armed") and the waveform (`drawLoopIn`: a green IN line
      plus a dashed band to the playhead — the loop OUT would make now) both
      show it. OUT closes an armed IN, or with a manual loop already set moves
      its end to the playhead. A loop that is switched off stays drawn as a dim
      outline (`drawLoop(…, idle)`), so the toggle shows what it would restore.
      **‹ › (and ←/→) MOVE an engaged loop by its own length**, Traktor-style,
      and only jump the playhead when no loop is engaged. The move sets the new
      loop BEFORE seeking: `PlaybackEngine.seek` honours the loop in force, so
      seeking first wraps the playhead back into the old loop.
    - **Hotcue edits live on the pad's right-click menu** (type as one-click
      choices, a colour swatch grid + Default where `cues.color` is `palette` —
      `ContextMenu`'s `swatches` item, Rename…, Delete); there is no standalone
      type dropdown or delete button. Rename re-sets the slot with its own start/type/length plus the
      new name, since `set_cue` replaces a slot. A non-editable cue gets the
      same menu disabled, not no menu.
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
  (`cues.types`), the cue colour swatches (`cues.color === 'palette'` +
  `cues.palette`; a `free` platform gets no picker yet), the rating scale
  (`tracks.rating_max`), the grid Lock button (`grid.lockable`). Per-node playlist flags (`can_rename`, `can_delete`,
  `can_add_tracks`) gate the sidebar and the context menu's "Add to" — each on its OWN flag,
  since a platform may allow renaming but not deleting. Carried but deliberately unused until a second adapter
  exists: `slot_labels: 'letter'`, `loops: 'separate_bank'`, and
  memory-cue *editing* (one-platform features stay preserved-but-uneditable).
- `lib/trackColumns.tsx` — single source of truth for the library table's
    columns (defs, default widths/visibility/order, the Columns-menu list, the
    inline `InlineEdit` cell, and the `TableMeta.onEditField` augmentation).
  - Data flow: the whole library is fetched once (`/api/tracks?limit=20000`);
    filtering and sorting happen **client-side** for instant interaction. Edits
    apply in-memory (server holds them) and the sidebar **Save to Traktor**
    button flushes to disk. Styling is Tailwind v4 with tokens in `src/index.css`
    — see "Theme" below.
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
  real flexible grids in the collection project correctly (K); and
  **removing a track from the collection** drops its `<ENTRY>` and its
  PRIMARYKEYs from exactly the playlists that held it — the only ADDED lines are
  the `ENTRIES=` recounts, every other playlist byte-identical (L); and a
  **path remap** rewrites locations + playlist keys localized (H1/H2), re-keys
  the entry index so earlier tag edits still reach the file (H3), and is
  **refused with nothing changed** when it would give two ENTRYs one primary key
  — onto a staying track's path, or two differently-keyed entries for one file
  — which the preview reports first, while a chain (A onto B's old path, B
  onward) and a pre-existing duplicate are allowed, each track's staged art
  and edits following the TRACK (H4).
  The guard that catches serialization regressions like the lxml reformatting bug.
- `test_phase3.py` — full create/add/reorder/rename/delete/save cycle stays
  Traktor-valid, backup-first, COLLECTION byte-identical, original untouched.
- `test_timebase.py` — Traktor's MP3 time base: `mp3_gapless` on generated
  MP3s (header found after a 1 MB ID3 tag; header-less has none), the offset
  in samples (2257 at 44.1 and 48 kHz, 0 header-less / WAV / missing), every
  write path adding it and the projection removing it, a grid anchored inside
  the header frame reading negative and round-tripping byte for byte, and —
  when kit 4 is on the machine — Traktor's own hand-placed cues reading back
  onto the kick. Then the same for rekordbox's clock: the per-file offsets
  (MP3 header, header-less, 48 kHz, FFmpeg and Apple AAC priming, the
  unreadable fallback), negative beats dropped with bar numbering kept, and
  rekordbox's own hand-placed kit-4 cues read back from `master.db`.
- `test_stem_file.py` — the stem writer on generated sources with a FAKE
  engine: both JSON literals byte-exact against the user's commercial files and
  collection (skipped when absent) and float32-equal; mono not 3 dB quieter;
  48 kHz resampling does not move time; MP3 and FLAC through both encoders give
  five streams with only the mix enabled, no shift (cross-correlation), the
  bitrate rule (256 floor, lossless 320), every tag + cover; `verify` refuses a
  shifted, truncated or box-less file; a raising checkpoint cancels.
- `test_stem_swap.py` — `apply_stem_swaps` on a temp copy of the real
  collection with a generated MP3 (with header) and a real stem file: prep
  reads back at the same decoded second while its START moves by the 2257-
  sample offset read from the PARKED file (and the 51 ms error reading the old
  path would cause); outside the ENTRY only its PRIMARYKEYs change (TYPE STEM);
  save round-trip; anchor-before-0 moves a whole beat with its companion, a
  hotcue clamps and is reported; "add" appends, keeps the original, joins a
  playlist, carries pending tag fields; clashes refused with nothing changed.
- `test_engine_manager.py` — the engine manager and process against a LOCAL
  Range-aware server and a FAKE engine script (no network, no torch): resume,
  restart when Range is ignored, checksum refusal, cancel keeps the `.part`;
  install from split parts with byte progress and a probe; a wrong-version
  engine or corrupt part leaves nothing installed; side-load and weights
  verified; the CUDA offer's driver/capability floors; the process relays
  progress, cancels without dying, reports a crash with its reason, keeps the
  Mac awake; the routes (install job in bytes, 409 while one runs, remove).
- `test_stems_batch.py` — the Convert to Stems batch through the routes with
  the FAKE engine (`stem_test_support.py`, shared with
  `test_engine_manager.py`), on temp collections pointed at generated MP3s:
  skips with reasons (an existing target is never overwritten); Replace parks,
  swaps at the end, leaves the saved file untouched, keeps prep in decoded time
  and 409s the blocked routes; Save deletes parked originals and retargets an
  export set; Discard and switching library restore; Cancel leaves no file;
  one bad track fails alone; destination+add mirrors into a new playlist;
  crash recovery restores (or finishes a committed save) and a re-run REUSES
  the kept stem files without calling the engine.
- `test_stem_playback.py` — stem playback's backend half on a generated stem
  file (fake stems = distinct scalings of the mix): the layout is read from
  the FILE (an MP3 whose entry carries `<STEMS>` has none); each served stem
  is its own MP4, bit-identical to that stream in place and the RIGHT stem;
  out-of-range / non-stem requests 404; the cache reuses, follows an edited
  file, evicts LRU but never what it just wrote; the folder origin serves too
  and lists the file as a stem track; a stem file ADDED from a folder gets
  `<STEMS>` and a `TYPE="STEM"` playlist key, the MP3 beside it neither. On a
  Rekordbox copy, an added stem file projects as a stem track (also after a
  reopen) and the deck is offered its stems.
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
  disk until save; and playlist create/fill/rename/delete round-trip. And
  **removing a track** deletes exactly the rows Rekordbox 7 deletes (measured
  2026-10-01 by diffing a removal made in Rekordbox itself: `djmdContent`,
  `djmdCue`, `contentCue`, `contentFile`, `djmdMixerParam`, `djmdSongPlaylist`
  — DELETED, never `rb_local_deleted`; the playlist row untouched, later
  entries renumbered) plus, after the commit, its ANLZ files and their emptied
  folder. Artwork is left (unmeasured: `_m`/`_s` sizes are not in
  `contentFile`), and a track in a table that was empty when measured (Sampler,
  History, Tag List, My Tag, …) is refused with nothing changed. And **adding
  tracks** (J) on generated kick-track MP3s: a raising `checkpoint` cancels
  before the library is touched; nothing reaches disk before save; afterwards
  only INSERTs (plus the counter) into the tables Rekordbox writes for a new
  track, the row filled from the file and stamped with the library's device,
  one `contentFile` per analysis file + `artwork.jpg` whose Hash is the MD5;
  a plain file gets Konduktor's grid ON the kick, a source's grid, hot cue
  (pad, colour), loop and memory cue cross as themselves; and removing it
  deletes its analysis files. `test_folders.py` drives the same through
  `/api/folder/add` on a Rekordbox copy, and a non-addable library 422s.
- `test_onelibrary_adapter.py` — the third adapter against the same contract.
  Unlike the Rekordbox tests it needs **nothing installed and nothing plugged
  in**: it runs against `fixtures/onelibrary/`, a real rekordbox 7 export trimmed
  to fixture size (its README says what was removed; the database is untouched).
  So the expected values are checked against bytes *rekordbox* wrote, not bytes
  Konduktor wrote. Pins the drive-relative path resolution, the `PCOB`/`PCO2`
  merge, and — cross-checked against the same cues in `master.db` — the dense
  ANLZ slot numbering, which is the thing most likely to be silently wrong.
  Also reads `fixtures/onelibrary/rekordbox-edited/` (a stick after rekordbox
  itself edited it: compact cue entries) and pins browsing vs editable mode. It
  opens a temp COPY of the fixture — a stray save once rewrote the checked-in one.
- `test_onelibrary_fidelity.py` — the row-level diff for OneLibrary writes, on a
  temp copy of the fixture: nothing reaches the drive before Save, a no-op save
  changes zero rows, a title edit exactly `content.title` (no counter, as
  rekordbox), dates as `YYYY-MM-DD` text, lookups found-or-created with
  `nameForSearch` NULL, a new playlist on top with siblings shifted, entries
  from 1; and the stick hazards — another app's write refused, an unplugged
  drive keeps the edits, stale `-shm` deleted, the backup holds the old rows,
  the drive never held open, file tags written (title yes, rating no).
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
- `test_grid_batch.py` — **batch analysis** from the context menu, both
  `jobs.py` jobs shown in the status bar: Analyze Grid & BPM
  (`POST /api/tracks/grid/auto-batch`) and Auto Hotcues…
  (`POST /api/tracks/cue/auto-batch`, one template for every track). Pins the
  batch-only decisions: locked grids are skipped always, existing grids unless
  `replace_existing`; a hotcue batch analyses a grid FIRST where there is none
  and never touches an existing one, and a ticked Replace applies to every
  track; a failing track is reported not fatal; **one batch at a time ACROSS
  both kinds** (a grid run would move the beats a hotcue run is placing cues
  on); a cancel KEEPS finished tracks (unsaved edits like any other) and still
  returns its result; opening another library cancels the run. Each shares its
  helper (`_analyse_grid`, `_place_auto_cues`) with the deck's single-track
  button.
- `test_bulk_remove.py` — the context menu's **Remove ▸** routes
  (`/api/tracks/grid/clear`, `/api/tracks/cue/clear`, `/api/tracks/remove`):
  locked grids are kept, clearing hotcues empties the whole bank (loops and the
  grid's beat-1 cue too) but never the grid, a removed track leaves the
  projection AND its playlists while its audio file is untouched, removal is
  gated on `tracks.removable`, and all three are refused while a batch
  analysis runs. `remove_tracks` is the protocol's only DESTROYING verb for
  tracks; Traktor and Rekordbox implement it, OneLibrary (read-only) refuses.
  The journal records it as `track/remove`, NOT an edit — otherwise history
  would read "edited 40 tracks".
- `test_folders.py` — the Files trees and adding loose files, through the
  routes, on generated FLACs and a temp copy of the collection: a folder lists
  only the visible audio DIRECTLY in it; the audio route refuses anything a
  scan did not find; **a file the collection already points at is never added
  twice** (on Traktor that would be two ENTRYs sharing a primary key — a corrupt
  collection) but still reaches the playlist/export it was headed for; "copy"
  points the entry at the copy and "reference" at the original.
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
  skipped rather than fatal. And the incremental re-export: an unchanged
  re-export copies and decodes NOTHING (same inodes), an edited source and a
  copy tampered with on the stick are each re-copied alone, a moved shared root
  renames kept copies instead of copying, a cancelled re-export keeps them and
  the folder stays ours, and the analysis cache round-trips exactly.
- `test_export_pioneer.py` — the two Pioneer targets against the same contract.
  Harder than Traktor's in a way that test cannot cover: each writes a database
  PLUS per-track analysis files, so "the library" is no longer one path, and the
  prep survives a real change of representation rather than a re-render. Pins the
  three crossings that fail SILENTLY — the key is CONVERTED not copied, hot cues
  keep their PAD across the dense-vs-sparse slot difference, and a flexible
  multi-tempo grid survives expansion to per-beat and collapse back. Both are read
  back with Konduktor's own adapters, which is the strongest check short of the
  hardware: the reader was written against real rekordbox output.
- `test_relocate.py` — auto path remapping: `relocate.search` on temp folders
  (drive-letter change, clone = ambiguous, partial copy loses, bare filename is
  no match, dropped home folders), then the Traktor path through the routes on
  a collection re-homed to `X:`/`E:`: a resolving volume is never asked about,
  nothing applies before the answer and only a proposed mapping does, the
  collection and prefs are untouched, a reopen asks again, and a saved manual
  mapping's volume is left out.
- `test_library_id.py` — the property no other suite covers: **a library that
  moves keeps its identity**. Also that two libraries in one folder stay two, a
  removable library is never written beside, an unwritable location falls back
  rather than failing, and `paths.write_json` is atomic (curated user work, so
  `prefs.py`'s best-effort "any I/O error degrades to no prefs" is NOT adequate).
- `test_discard.py` — discarding unsaved edits through the route: the edit is
  gone, the file and version history untouched, relocation answers and the
  saved path mapping survive, the adapter object is the same (a reload, not a
  reopen), and a running batch has stopped by the time discard returns; on
  Rekordbox (temp copy, skipped when absent) the library is clean afterwards and
  a later save writes nothing that was thrown away; read-only libraries 409.
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
  utility classes with the `ink-*`/`well`/`accent`/`gold`/`mint`/`pink` tokens
  and the material classes from `index.css` (see "Theme"). Icons come from
  `lib/icons.tsx`, never emoji; a platform's own mark comes from
  `lib/platformIcons.tsx` (`<PlatformIcon platform=…>` — single-colour marks
  redrawn from each platform's app icon, masks keyed by `useId` since one page
  can show a mark twice). That file is a lookup of display ASSETS by platform
  id, not platform branching; an unknown id falls back to a generic mark, so a
  new adapter needs no change there to appear. Keep `api.ts` types aligned with `schemas.py`.
- **Never `window.confirm` / `alert` / `prompt`.** They cannot be themed, they
  block the event loop (a playing deck stutters), and under Tauri they read as a
  web page. Use `await askConfirm({ title, body, confirmLabel, tone? })` from
  `lib/confirm.tsx` — same one-line shape as `confirm()`, rendered as the themed
  `ConfirmDialog` by the `ConfirmHost` mounted once in `main.tsx`. `tone` is
  `danger` (default, pink) or `primary`. Components that already hold dialog
  state may still render `ConfirmDialog` directly.

## Theme — "dark liquid glass"

Floating frosted panels (12 px gutters) over a drifting wash of colour sampled
from the loaded track's cover art. Colour is reserved for MEANING — cue types,
key, playhead, the one primary action; chrome stays neutral. The rules that are
easy to break:

- **`ink-900`…`ink-600` are translucent WHITE overlays, not greys**, so they
  read on any panel. Only `ink-950` is opaque: it is the dark text on light
  buttons and the page base. Recessed things (inputs, waveforms, readouts,
  progress tracks) use `well` / the `.well` class — never `bg-ink-950`.
- **Materials are classes in `index.css`**: `.glass` (panel), `.glass-overlay`
  (menus, dialogs, toasts — denser), `.well`, `.btn-glass`, `.btn-primary`
  (white→accent pill, one per view), `.is-selected`. The rim light is a
  `::before`, so these set `position: relative`; Tailwind's `fixed`/`absolute`
  utilities win because utilities outrank the components layer.
- **Never put `backdrop-filter` (or `filter`/`transform`) on an element that can
  contain a popup.** It makes that element the containing block for every
  `position: fixed` descendant, so a context menu or dialog opened inside it is
  placed relative to the panel, not the window — the sidebar's playlist menu
  opened far from the cursor. `.glass`'s blur and tint therefore live on its
  `::after` (z-index -1 under `isolation: isolate`), not on the panel.
- **Every overlay renders through `createPortal(…, document.body)`** — all
  dialogs, `ContextMenu` and `Toast`. Each `.glass` panel is a stacking context
  (`isolation`), so an overlay rendered INSIDE one can never rise above a
  neighbouring panel, whatever its z-index: New Export (from the sidebar) sat
  behind the library, and the deck's Auto Hotcues dialog behind both. One
  z scale, set on the overlay's root: dialogs `z-50`, a dialog opened from a
  dialog (`FolderPicker`) `z-[60]`, menus `z-[70]`, toasts `z-[80]`. A new
  overlay that skips the portal will look fine when opened from `App` and break
  the first time a panel opens it. Small popovers anchored to a control inside a
  scrolling list should open the shared `ContextMenu` instead (as the playlist
  row's "Add to export" does) — a popover inside the list is clipped by it.
- **A `.glass` / `.glass-overlay` box must never be the element that scrolls.**
  Its tint, blur and rim are pseudo-elements sized to the box, so they scroll
  away with the first screenful and leave later content on bare background (the
  column chooser's last items did). Give the glass frame `overflow-hidden` and
  scroll an inner element — as every dialog and `ContextMenu` do.
- **Keep backdrop blurs on SHORT elements small** (the table's sticky header uses
  8 px). Chrome silently skips a large backdrop blur on a thin strip — 12 px and
  up left the rows under the ~33 px header perfectly sharp, with the computed
  style still reporting the blur, so nothing looks wrong in DevTools.
- **The accent is `oklch(0.8 0.11 var(--accent-hue))`** — the cover's hue at a
  FIXED lightness and chroma, so dark text on it keeps its contrast whatever is
  loaded. `lib/ambient.ts` sets `--accent-hue` and `--amb-1..4` on `<html>`;
  they are `@property`-registered so a track change glides, and nothing in React
  state depends on them (no re-render on a track change). The art is fetched as
  a blob → `createImageBitmap`, because a cross-origin `<img>` (the API under
  Tauri) taints the canvas. A device track, or greyscale art, gets the default.
- `AmbientBackdrop` is mounted in `main.tsx`, behind the picker too; so no root
  container may set an opaque background, or the glass has nothing behind it.
- **Canvas drawing glows instead of outlining** (`cues.ts`): a black outline on
  glass reads as a gap in the waveform. The playhead is `drawPlayhead`, shared
  by both waveforms.
- The table's Cues column draws one dot per occupied slot from
  `Track.hotcues` (`HotcueChip`: slot, type, colour), which every adapter
  projects with the SAME slot test its `track_cues()` uses, so the dots cannot
  disagree with the pads. OneLibrary fills it lazily, like `hotcue_count`.
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
  shortcuts (Space = play/pause, 1–8 = hotcues, Shift+1–8 = delete, Cmd/Ctrl+↑/↓ =
  loop/jump size). See "Prep engine".
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
  USNs are maintained. Remaining gap: **writing cover art** (reading works: the file's embedded art,
  else Rekordbox's own `ImagePath` copy under `share/`). **No version history on Rekordbox** — accepted scope decision,
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
    **Rekordbox implements it too** (2026-10-01; gated on `tracks.addable`,
    which the folder menu, the folder header and the device Import button read
    from the COLLECTION's capabilities). Unlike Traktor it must ANALYSE: a
    Rekordbox grid and waveform live in the ANLZ files, without which Rekordbox
    shows neither grid nor cues. So `add_tracks` decodes each file once
    (`waveform.decode` → `analyse_samples` + `grid_detect`), in a first phase
    that may be cancelled through `checkpoint` before the library is touched;
    a file with no grid of its own gets Konduktor's (decided: arrive ready to
    prep). Then `RekordboxStore.add_track` writes the row the way Rekordbox's
    own add was MEASURED to (file facts, `DateCreated` from the file's birth
    time, this library's device) and the grid/cues replay through
    `replace_grid`/`set_cue`/`add_memory_cue` — memory cues and colours cross
    as themselves, a hot cue with no free pad becomes a memory cue. Its ANLZ
    files and artwork (`NewTrack.art`, else the file's embedded cover) are
    held until save and written BEFORE the grids and commit, then
    `contentFile` rows hash what was written (`_m`/`_s` unlisted, as
    Rekordbox does); a failed save removes them. Key, mixer gain and phrases
    are Rekordbox's own analysis and are not written. The row and file
    builders live in `adapters/rekordbox/new_content.py`, shared with the
    Rekordbox Library exporter.
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
- 🟡 OneLibrary adapter — **reading done; editing in progress** toward parity
  with Rekordbox (2026-10-01, [decisions](.claude/discussions/discuss-onelibrary-editing-2026-10-01.md)).
  A OneLibrary USB drive opens, projects, browses and searches: tracks,
  playlists, hot cues, memory cues, loops and flexible beatgrids. Done: track
  metadata (+ file tags) and playlists, Save via a working copy. Next: cues and
  grid in the ANLZ files (rekordbox blanks `PQT2` on a grid edit — measured),
  the `export.pdb` rebuild on save, adding/removing tracks, "Open for editing…"
  on a Devices row. All format research — including the schema, which has no
  public spec — is in [.claude/handoffs/onelibrary-adapter.md](.claude/handoffs/onelibrary-adapter.md).
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
  paths drive-relative). **What rekordbox needs was MEASURED** — by stripping
  tags from a rekordbox-written stick one at a time and loading each track in
  rekordbox 7 (`goober`, 2026-09-27): a `.DAT` must carry **`PVBR` AND the preview
  waveform (`PWAV`/`PWV2`)** or rekordbox shows NEITHER the grid NOR the hot
  cues, even when those tags are rekordbox's own bytes. It follows the
  `analysisDataFilePath` column (the `P0xx/xxxxxxxx` names need not match its
  own path hash, which is no standard hash of the path), and the `.EXT`
  waveforms and whole `.2EX` only affect drawing. So every export decodes each
  track once (`core/waveform.py`, ~1.5 s each via librosa — CoreAudio for AAC
  on macOS; an undecodable file gets a flat preview) and `PVBR` is 400 zeros
  plus the MP3's length in samples. Both Pioneer exporters write the full
  seven-tag `.DAT`; the `master.db` target's need is inferred, not measured.
  Progress and cancel reach the writer through `ExportPayload.checkpoint`.
  **pyrekordbox accepting an ANLZ file proves nothing**: its parser ignores
  `len_header` and the constants (a 12-byte `PCPT` header round-tripped
  perfectly; real: 28). The cue tags are therefore pinned **byte-for-byte** against the fixture's rekordbox-written
  files (`test_export_pioneer.py`): both files always, every list always (hot +
  memory `PCOB` in each, hot + memory `PCO2` in the `.EXT`, empty ones
  included), hot cues highest pad first, the `1000`/`1` constants in `PCP2`.
  `fixtures/onelibrary/seed.sql` holds rekordbox's browse scaffolding
  (`menuItem`/`category`/`sort`/`color`), like the Rekordbox target's.
  `fileType` follows the suffix (a `.stem.m4a` is 4, M4A), bitrate is written in
  kbps — **the generic `Track.bitrate` is bits per second** (the table divides by
  1000; the Pioneer readers multiply) — and a stale `exportLibrary.db-wal`/`-shm`
  is deleted before writing, because Rekordbox leaves both on a stick it mounted
  and SQLite replays a leftover WAL into a new database. **A Pioneer grid can
  start mid-bar** (Motorola opens on beat 3): `markers_from_beats` puts the
  first marker on the first DOWNBEAT — the generic first marker is bar 1 — and
  `beats_from_markers` writes the lead-in beats back, numbered as they were;
  before this every bar of such a track landed two beats early, on import to
  Traktor as well as on export. **Rekordbox's clock runs behind the decoded
  audio by the start padding it does not trim, which depends on the FILE**:
  an MP3 with a Xing/Info header 1105 samples (~25 ms; rekordbox skips the
  header frame, unlike Traktor), a header-less MP3 0, an M4A its edit list's
  priming (1024 from FFmpeg, 2112 from Apple — rekordbox ignores the edit
  list), lossless 0. Measured with blinded cues in rekordbox 7 (kit 4,
  2026-09-30); it replaced a fixed 1105 that was 25 ms wrong on header-less
  MP3s and 22 ms on Apple AAC. The file is read (`core/mp3_gapless.py`,
  `core/mp4_edit.py`); one that cannot be read falls back to 1105. A position
  before the decoded audio (a Traktor grid anchored in the header frame)
  clamps to 0 on write, and `beats_from_markers` drops beats before 0 while
  keeping the rest's bar numbering. The OneLibrary fixture's demo MP3s are
  12-byte stubs, so the fixture tests exercise only the fallback —
  `test_timebase.py` covers the per-file rule. The generic model is
  the decoded time base, so `adapters/rekordbox/timebase.py` is applied at EVERY
  Pioneer boundary: both exporters add it, the OneLibrary reader and the
  Rekordbox adapter subtract it on read and add it on write (`set_cue`,
  `set_cue_type`, grid save). Without it every cue sat 25 ms early in rekordbox.
  **Drawn waveforms** (`.EXT` `PWV3`/`PWV5`, `.2EX` `PWV7`/`PWV6`/`PWVC`) are
  generated from one decode (`core/waveform.py`: 150 frames/s, bands split at
  200 Hz / 2.5 kHz) with constants fitted to rekordbox's own files for 12 local
  tracks and checked on 5 held-out ones (per-band correlation 0.77-0.96).
  **Every column split goes through `waveform.even_slices`, never
  `np.array_split`**: the latter gives the first `n % k` chunks an extra frame,
  so overview column k held LATER audio than its position — up to ~1.8 s by
  column 420 of 1200, back to 0 at the end. Rekordbox drew such an overview a
  bar ahead of the playhead mid-track (`test_export_pioneer.py` pins a burst
  landing in its own column).
  **rekordbox 7's SONG LIST draws `PWV6` — and only when `content.contentLink`
  is set.** Both measured by bisecting a rekordbox-written stick: removing a
  track's `PWV6` emptied its row (removing `PWV4` changed nothing), and blanking
  its `contentLink` alone turned the row plain blue with a "?" beside CUE — what
  every Konduktor-exported row showed while the exporter left it NULL. It now
  writes rekordbox's 788224 (beside `analysedBits` 41; meaning unknown).
  `masterDbId`/`masterContentId` do NOT matter, so no link to a rekordbox
  collection is claimed. `PWV4` is still written (every rekordbox file has it);
  its low/mid/high bytes cannot be matched per column (b4/b5 are sampled
  at one instant: column-to-column correlation 0.05/0.16), so each byte is a
  QUANTILE MAP fitted to rekordbox's distribution (`_PWV4_MAPS`); smoothed, it
  tracks rekordbox's at 0.5-0.99. A very loud master draws smaller than in
  rekordbox, whose PWV4 keeps some absolute level. rekordbox reads 200+ covers
  from one `Artwork/00001` folder (verified on a 207-track export). Decoding is
  **PyAV** first (FFmpeg in its wheel: AAC on every OS; measured to start on the
  same sample as librosa) with librosa as fallback. **Cue colour on export** is
  what Konduktor SHOWS (`core/cue_colors.py`, mirroring `lib/cues.ts`): a stored
  colour, else the type's — blue cue, green loop — because "unset" makes
  rekordbox draw its own defaults (green/orange). **rekordbox draws a hot cue
  from its palette CODE** (`PCP2` byte 44, before the RGB; `djmdCue.ColorTableIndex`
  in master.db), not the RGB — `adapters/rekordbox/palette.py` holds the 16-swatch
  table MEASURED from a rekordbox 7 export (one swatch, ~0x12 teal-green, still
  unmeasured). Type colours map by intent (cue -> light blue 0x05, loop -> green
  0x16), anything else to the nearest hue; the palette has NO white, so a grid
  companion is code 0 + RGB white (what rekordbox draws for it: unverified). The
  **Artwork** (`adapters/rekordbox/artwork.py`, Pillow), measured on a rekordbox 7
  export: `PIONEER/Artwork/00001/{a,b}<n>.jpg` at 80 px and `_m` at 240 px, `a`
  byte-identical to `b`, baseline JPEG q85 4:2:0, non-square art LETTERBOXED onto
  black (a crop or stretch is visibly wrong); `image.path` names the `b` file and
  `content.image_id` points at it; one image per track, never shared. The runner
  carries each cover as `ExportTrack.art` from `LibraryAdapter.cover_art` — not
  re-read from the copied file, since a library's art is not always embedded.
  The OneLibrary READER now serves `cover_art` too (the `_m` file). How rekordbox
  itself splits a large library across `Artwork/000NN` folders is unknown; one
  folder of 207 was verified to load in full.
  **The `master.db` target was opened in a real Rekordbox 7** (2026-09-27, by
  swapping an export into `~/Library/Pioneer/rekordbox` with a full backup):
  it accepts the library, keeps our tracks/cues/analysis untouched, and shows
  grids, cues and the playlist tree. Two things it needed: a
  `masterPlaylists6.xml` beside `master.db` (written as a skeleton BEFORE the DB
  is opened, so pyrekordbox registers each playlist in it), and
  `djmdContent.ContentLink` — a bit field: NULL = blue song-list preview + "?",
  any value = coloured, 0x200000 clears the "?", and the real library's
  0x3D060E adds a badge claiming analysis we do not write, so the exporter uses
  0x2C060E. **Parity with the OneLibrary target is done and verified in
  Rekordbox 7**: cue colours (palette code in `djmdCue.ColorTableIndex`,
  `Color` -1 — coloured loops included), memory cues (`store.add_memory_cue`,
  export-only: the adapter still refuses memory-cue edits), and artwork
  (`share/PIONEER/Artwork/<uuid[:3]>/<uuid[3:]>/artwork.jpg` fit to 800 px plus
  `_m` 240 / `_s` 80 letterboxed, `ImagePath` -> artwork.jpg). The exporter writes
  cues through `store.set_cue`/`add_memory_cue`, which share `_write_cue` with
  every adapter edit, so the `contentCue` mirror stays in step.
  **Two Rekordbox targets, named for what they are:** "Rekordbox Library"
  (`export.py`, a COMPUTER `master.db` — Rekordbox never reads one from a stick)
  and **"Rekordbox Export"** (`device_export.py`, platform `rekordbox_export`: a
  stick's legacy **Device Library**, `PIONEER/rekordbox/export.pdb` +
  `exportExt.pdb`, what Rekordbox lists as "Device Library" and pre-OneLibrary
  players read). `pdb.py` reads and writes the DeviceSQL format — no public spec;
  the layout is crate-digger's, CONFIRMED byte for byte against a real rekordbox 7
  device export (every row re-encodes identically; a data page rebuilt from its
  rows is identical). It writes from rekordbox's OWN empty device library
  (`fixtures/rekordbox/device/`, the fixed colour/column/menu tables) and follows
  rekordbox's page allocation (a table's first data page is its reserved
  candidate; further pages and the new candidate come from next-unused). The
  track row's `bitmask` is 0xC0700 = OneLibrary's `contentLink`; `u3`/`u4` are
  the halves of rekordbox's `masterDbId` and stay 0. **A table's HEADER page is
  not empty: it indexes its data** (first data page; for tracks, playlist tree
  and history an entry per data page, `page << 3 | 3` if it has room) and the
  history row counts the tracks — left as in the empty template, Rekordbox 7
  said "Device library is corrupted". It shares the stick's
  analysis files and `a<n>.jpg` artwork with the OneLibrary target — the same
  paths, and whichever target runs second REUSES what the first just wrote, so a
  two-format stick costs one decode per track. A target with no reader is listed
  by `/api/export-targets` from the exporter registry; `LibraryExporter.
  display_name` names a target where one platform has two. `adapters/rekordbox/export.py` writes a `master.db` and
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
  `ExportRunDialog`. **Multi-target** (2026-09-27): audio is copied once, then
  each target's `write()` runs in turn over it; ANY failure undoes everything,
  earlier targets' libraries included — and files a failing writer never got
  to report, via a snapshot of the destination taken before the first write.
  The manifest lists `libraries` (plural; the old `library` key is still
  cleared). Audio lives under **`Contents/`**, so a music folder with a
  top-level `PIONEER/` or a `master.db` cannot land on a library's path. An
  exporter's static `drive_root` (OneLibrary: a CDJ looks for `PIONEER/` only
  at a stick's root) surfaces as `PlatformOption.drive_root` and the preview's
  `not_drive_root` — a **warning, not a block**, since the other targets are
  valid in any folder. Three properties it is built around: audio is copied FIRST
  and the library written LAST, so a cancel leaves **no** library file and its
  absence is what marks an export unfinished; rollback registers each file
  BEFORE writing it, so the in-flight copy is cleaned up too; and the **manifest**
  (`.konduktor-export.json`) means clearing removes exactly what the last export
  wrote, so a file the user put in the folder survives a re-export. Audio mirrors
  the source tree **relative to `common_dir_prefix`** — mirroring absolute paths
  would put the user's home directory on the stick, and relative paths make
  filename collisions impossible. A destination Konduktor did not write is
  **refused, not confirmed**: there is no "do it anyway".
  **Incremental re-export** (2026-09-28): the manifest's `audio` map records,
  per copy, the SOURCE's path/size/mtime and the COPY's size/mtime as read
  back (FAT32 rounds mtimes, so they are never assumed equal). A copy is kept
  when all four match — **no checksums**: hashing the copy means reading it
  back over USB, about the cost of copying it. Copies are matched by SOURCE
  path, since adding a track from another folder moves `common_dir_prefix`
  and every destination path with it; a kept copy that moved is RENAMED
  (two-phase via `.konduktor-moving/`, since one's new path can be another's
  old one). Order: old libraries and unkept files go, kept copies move, a
  manifest listing every path the run may leave is written (crash-safe), new
  copies land as `*.konduktor-partial` and are renamed in, libraries last. A
  failed run with kept files REWRITES the manifest around them rather than
  deleting it — else the folder would be refused as not ours. Pioneer
  waveform decoding is cached on the drive too (`core/analysis_cache.py`,
  `.konduktor-cache/`, keyed by the same source facts + the lead +
  `waveform.ANALYSIS_VERSION` — **bump that when `analyse()` changes**);
  exporters reach it through `ExportTrack.waveform()`, never
  `waveform.analyse()` directly. The Rekordbox Library target's ANLZ paths are
  a fresh UUID per export, which is why the MEASUREMENT is cached rather than
  old ANLZ files reused by path.
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
  release_date, comment, comment2, rating (0–5 stars → `RANKING = stars*51`).
  **`comment2` is Traktor's "Comment 2", stored in `INFO@RATING`** — free
  text despite the name, unrelated to the stars — and it is collection-only:
  Traktor writes no file tag for it, so `_sync_file_tags` skips it too. Path, BPM and
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
  splits into 3 bands via `OfflineAudioContext` biquads, and paints them as
  **three layers**, not one blended colour per column: bass (orange) first and
  tallest, then mids (violet) and highs (cyan) with a `screen` blend — no green,
  matching Traktor's Spectrum. Each band is normalised to its own 99.5th
  percentile and then EXPANDED (bass steepest): dance music holds its bass near
  full scale for whole sections, so a linear map draws a solid orange wall
  instead of the kick pulse. The main view draws 2 px bars with a 1 px gap,
  **binned in track space** so bars scroll rather than flicker.
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
