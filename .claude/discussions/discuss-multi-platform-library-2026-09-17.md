# Discussion: Multi-platform library support (Traktor / Rekordbox / Serato)
Date: 2026-09-17
Status: Concluded

## Context
Konduktor currently targets Traktor only (NML v20 via `traktor-nml-utils`), with a
save path whose central guarantee is byte-exact fidelity: a no-op save is
byte-identical, and only edited objects diff (`test_save_fidelity.py`).

Ben wants to extend to Rekordbox and Serato. Proposed shape:
1. A generic library data structure that supports **all** features of **all**
   platforms ("full support is critical").
2. Load any platform's library into that structure.
3. Edit/prepare with Konduktor's existing features.
4. Save back to the **original** library format.
5. Ultimate goal: **convert** a library from one platform to another.

## Open Questions
- [ ] **Command vocabulary for grids** (now evidenced): `set_grid(bpm, anchor_sec)`
      cannot express per-marker edits. Needs marker-level commands — add / move /
      set-BPM / delete marker — plus marker selection in `GridControls`. Design these.
- [ ] Verification tests: write the per-platform "localized change" analogues —
      Rekordbox row-level table-dump diff, Serato byte diff excluding the patched GEOB
      frame + whole-file diff of crates.
- [ ] Rekordbox `rb_local_usn` / update-sequence + cloud-sync columns: writing rows
      without maintaining these may corrupt RB's own sync state. Needs investigation.

## Decisions Made

### Write scope: full read/write for all three platforms
- **Decision**: Konduktor must edit and save back into each platform's own live
  library — Traktor NML, Rekordbox (`master.db`), and Serato (binary crates +
  in-file GEOB tags). Not read-only, not export-only.
- **Rationale**: The user's existing library on their chosen platform is the
  canonical source; Konduktor is a management tool for it, so it must be able to
  write back wherever the user actually works.
- **Alternatives considered**: Traktor-write-only with read+export for the others
  (smaller, safer v1); Traktor+Serato write with Rekordbox read-only (avoids the
  encrypted/unsupported RB DB); read-all/export-only (no in-place editing at all).
  All rejected as too limited.
- **Consequence**: Rekordbox and Serato writes carry materially higher risk than
  NML — both formats are reverse-engineered and neither has a fidelity guarantee
  equivalent to the current byte-exact NML save. Per-platform backup + verification
  strategy becomes a first-class requirement.

### End goal: canonical-library management + converted exports
- **Decision**: The user's main platform library remains the canonical source of
  truth. Konduktor manages and edits it in place, and *additionally* can produce
  converted exports for other platforms.
- **Rationale**: Keeps the user's existing workflow intact — they keep using their
  DJ software normally — while making conversion a deliberate, separate output.
- **Alternatives considered**: one-shot migration only (too narrow); two-way sync
  between libraries (rejected — requires cross-platform track identity, change
  detection and conflict resolution; much larger project); Konduktor owning its own
  canonical library format with platforms as import/export only (rejected — changes
  the product's identity and displaces the user's real library).
- **Consequence**: Two distinct write paths must coexist: a high-fidelity in-place
  *edit* path per platform, and a lossy-by-design *export* path. They have opposite
  requirements and should not share an implementation.

### Track identity: not needed — export always generates a fresh target library
- **Decision**: An Export is a one-time conversion that generates a **fresh** target
  library. It never merges into an existing library on the target platform. The
  originally-loaded library remains canonical and is the only thing edited in place.
- **Rationale**: Keeps the canonical-source model clean — edits go back to the source
  library, exports are disposable output. Removes the need for any cross-platform
  track-matching mechanism.
- **Alternatives considered**: path-only matching (fails on relocation); path with
  artist/title/duration tag fallback (false positives); audio content hashing (robust,
  survives moves, but requires a full library scan); acoustic fingerprinting
  (Chromaprint/AcoustID — heavy, solves a problem this product doesn't have). All
  rejected as unnecessary given fresh-export-only.
- **Consequence**: The existing `volume + dir + file` primary key remains sufficient.
  Cross-library identity is deferred indefinitely; it would only return if merge-style
  export were ever wanted.

### Serato backup: GEOB-frame sidecar, not full file copies
- **Decision**: Before patching a Serato GEOB frame, snapshot the original frame bytes
  to a sidecar restore file. Do not copy whole audio files. Crate files are copied
  wholesale (tiny).
- **Rationale**: Kilobytes instead of gigabytes per save, and a complete restore for
  everything Konduktor actually touches, since Konduktor only ever rewrites those frames.
- **Alternatives considered**: full audio-file backups — rejected on cost (~1 GB for a
  100-track save).
- **Risk accepted**: does not protect against the tag library corrupting an unrelated
  region of the audio file. This exposure already exists via `file_tags.py` cover-art
  writes; Serato support widens it from occasional to routine.

### Export = a new self-contained library, always including copied audio
- **Decision**: An export/conversion always produces a whole new library at a
  user-chosen destination, complete with copies of the music files — for every target
  platform, not just Serato. Reference-in-place is not offered.
- **Audio folder layout**: a **global, persistent export setting** with two options —
  **flat single folder (default)** or **mirror source directory structure**.
  *Rationale*: flat is predictable and simple for the common case; mirroring is there
  for users who want their original structure preserved. *Alternatives considered*:
  organised Artist/Album from metadata (clean but depends on metadata quality and needs
  sanitising); mirror playlist structure (legible on USB, but duplicates tracks that
  appear in multiple playlists).
- **Rationale**: The export is self-contained and portable (USB drive, another machine,
  handing it to someone else). Consistent behaviour across targets.
- **Alternatives considered**: copy only where the format forces it (Serato), letting
  Traktor/Rekordbox exports reference originals in place for a near-instant export;
  offering it as a per-export user choice. Both rejected in favour of one predictable
  behaviour.

### Export Sets: user-curated export scope
- **Decision**: Introduce an **Export Set** (not "Crate" — see naming decision) — a named, persisted list of track and playlist
  *references* defining what a given export includes. Enables partial exports. Each
  crate belongs to a specific library, and crate data is persisted in Konduktor's own
  storage (not in the platform library).
- **Flow**: open library → prep tracks → save back to the canonical library → create a
  crate → drag tracks/playlists into it → export crate (choose target platform +
  destination) → Konduktor writes the target library files and copies the audio.
- **Rationale**: Users rarely want to convert an entire library; export scope needs to
  be curated and re-usable across repeat exports.
- **Naming**: called **Export Set**, not "Crate". *Rationale*: "Crate" is Serato's own
  term for a playlist and Konduktor imports Serato crates — the collision would be
  confusing in a multi-platform tool. *Alternatives considered*: keeping "Crate"
  (familiar DJ vocabulary, but ambiguous); "Bundle"; "Package".
- **Reference semantics: live**. An Export Set stores pointers to playlists; export
  resolves them at export time, so later additions to a playlist are picked up.
  *Alternatives considered*: snapshot-at-add (frozen and auditable, but stale);
  live-with-optional-freeze (more flexible, more UI + model complexity).
  *Consequence*: the export preview must be computed at export time, never stored.
- **Storage: own store, keyed by stable library ID**. A dedicated Export Sets store,
  separate from `userprefs.json`, with each library assigned a stable generated ID so
  that moving or renaming the collection file does not orphan its export sets.
  *Alternatives considered*: keying by collection file path (simple, orphans on move);
  storing inside `userprefs.json` (best-effort I/O, no backup — inadequate for curated
  user work); a sidecar file next to the user's library (travels automatically, but
  writes Konduktor files into the user's Traktor/Serato folders).

### Export mechanics
- **Flat-layout filename collisions: counter suffix.** Second and later collisions
  become `Xtal-2.mp3`, `Xtal-3.mp3`. *Rationale*: human-readable and predictable.
  *Alternatives considered*: metadata-based disambiguation (`Xtal (Aphex Twin).mp3` —
  most legible and stable, but depends on metadata quality and needs sanitising); short
  hash suffix (stable across re-exports regardless of order, but ugly).
  *Known consequence*: numbering depends on processing order, so repeat exports may
  assign different suffixes to the same tracks.
- **Playlist folder structure: preserve where supported, flatten with path names
  otherwise.** Keep the tree on Traktor and Rekordbox; on flat targets, flatten to names
  encoding the path (e.g. `House / Peak Time`). *Rationale*: retains the structural
  information in readable form on every target and avoids name collisions between
  same-named playlists in different folders. *Alternatives considered*: flatten to bare
  names (cleaner, but collides); always flatten (uniform, but discards structure the
  target could represent).
- **Re-export to an occupied destination: warn, then overwrite on confirm.** *Rationale*:
  the destination stays the single current copy, which is what re-export usually means,
  with a guard against accidental loss. *Alternatives considered*: always write a new
  timestamped folder (safe, but fills the drive with copies of a large collection);
  refuse and require an empty destination (tedious for a routine operation); incremental
  update in place (fast, but reintroduces target-library matching/diffing, which the
  fresh-export decision deliberately avoided).
- **Whole-library export: an explicit "export entire library" action.** A separate
  action that skips export-set creation. *Rationale*: wholesale migration is a common
  real use case and shouldn't require busywork. *Alternatives considered*: export sets
  only (uniform, but forces a select-everything set); a select-all shortcut inside the
  export set editor (single code path, but less discoverable).

### Export set resolution and export UX
- **Playlist resolution: auto-include, deduplicate, copy once.** Every track referenced
  by a playlist in the export set is included automatically; a track appearing in
  multiple playlists, or also added individually, is copied to disk exactly once.
  *Alternatives considered*: auto-include but surface the implied tracks in the UI (more
  transparent, more UI); require explicit inclusion (precise, but surprising).
- **Missing audio files: pre-scan, report, let the user decide.** Verify every file
  exists before copying anything; present what's missing and let the user cancel or
  proceed without them. *Rationale*: fails fast rather than halfway through a multi-hour
  copy. *Alternatives considered*: skip silently (incomplete export the user never
  learns about — worse given the loss report was deferred); abort entirely (one
  unmounted drive blocks an otherwise fine export).
- **Export UX: pre-check space, progress with cancel.** Estimate total size and verify
  the destination has room before starting; show per-file progress with a working
  cancel. *Alternatives considered*: cancel with rollback (destination never left
  half-finished, but deleting files from a user-chosen location needs care);
  progress-only with no pre-check (discovers a full drive after an hour of copying).
  *Known consequence*: cancelling leaves a partial export that must be cleaned up or
  clearly marked incomplete.

### Serato export copies the audio files
- **Decision**: Exporting to Serato copies the original audio files to the export
  destination and writes the GEOB frames into the **copies**, leaving the canonical
  library's files untouched.
- **Rationale**: Serato's prep data lives inside the audio files, so a Serato export
  cannot reference the originals without mutating them — and the originals belong to
  the canonical library.
- **Open consequence**: creates an asymmetry with Traktor/Rekordbox exports, whose data
  lives in a library file and could reference originals in place. See Open Questions.

### Rekordbox open-app handling: warn and proceed
- **Decision**: If Rekordbox appears to be running when the user saves, Konduktor
  **warns but allows the save to proceed** — it does not hard-refuse.
- **Rationale**: Consistent with the existing Traktor behaviour (the user is warned to
  close Traktor before saving, not blocked).
- **Alternatives considered**: detect-and-refuse (safer, but blocks the user on a
  detection heuristic that may be wrong).
- **Risk accepted**: Rekordbox may hold `master.db` open and runs cloud sync, so a
  concurrent write is riskier than Traktor's overwrite-on-exit behaviour. The backup
  taken before the write is the mitigation.

### Backup/verification: same invariant, per-platform mechanism
- **Decision**: Keep the existing invariant — *an edit changes only what it was
  supposed to change* — and implement a per-platform comparison mechanism for it:
  Traktor byte diff (existing), Rekordbox full table dump with row-level diff, Serato
  byte diff of the audio file excluding the patched GEOB frame plus whole-file diff of
  crate files. Backups: copy `collection.nml` / copy `master.db` (+ WAL/SHM) /
  Serato strategy TBD (see Open Questions).
- **Rationale**: Generalizes `test_save_fidelity.py`'s guarantee to formats where a
  byte-identical whole-file comparison is meaningless.
- **Alternatives considered**: dropping the fidelity guarantee for the non-Traktor
  platforms — rejected, it is the project's core safety property.

### Cue model: rich core cue, adapters collapse on write
- **Decision**: Model a cue richly enough to cover all three platforms:
  `{ position_sec, length_sec?, role: hotcue|memory, slot?, type: cue|fade_in|fade_out|
  load|loop, color?, name? }`. Adapters collapse it per platform — Traktor rejects
  `role: memory`; Rekordbox and Serato have no `fade_in`/`fade_out`/`load` types;
  Serato keeps loops in a separate saved-loop bank rather than consuming a cue slot.
- **Platform differences captured**: Traktor = 8 typed hotcue slots (loop is a cue
  *type*); Rekordbox = 8 hotcues A–H plus unlimited memory cues, colour per cue;
  Serato = 8 cues plus a separate saved-loop bank, colour per cue.
- **Correction noted during discussion**: Traktor's `CueV2Type` *does* carry a `color`
  field, so NML supports cue colours — an earlier assumption to the contrary was wrong.
  **Lesson: establish capabilities empirically per format rather than assuming them.**

### Lossiness policy: degrade where sensible, drop otherwise
- **Decision**: On export, data the target cannot represent is **degraded to the
  nearest equivalent where one exists, and dropped only where none does** — e.g. a
  Traktor fade-in cue becomes a plain cue in Serato; Rekordbox memory cues fill spare
  hotcue slots in Traktor until they run out.
- **Rationale**: The project aims for as much feature parity across platforms as
  possible; silently discarding representable data works against that.
- **Alternatives considered**: drop silently (clean output, but the user only discovers
  the loss on the gear); refuse to export when data would be lost (too rigid).
- **No loss report for now**: an export summary of what was degraded or dropped was
  explicitly deferred ("not for now"). The adapters will know what they collapsed, so
  it remains cheap to add later.

### Proposed sequencing (not yet confirmed)
1. Fix the flexible-grid bug standalone (small, independent, fixes a present-day data
   risk). Requires verifying the NML representation first.
2. Refactor to generic model + adapter interface with **Traktor as the only adapter** —
   no new formats, all existing tests must still pass, so `test_save_fidelity.py` proves
   the projection+command split broke nothing. Takes the architectural risk where it is
   verifiable.
3. Add the second platform — where a one-adapter abstraction gets tested for real.
4. Export — only meaningful once two platforms exist.

### Sequencing: grid fix → Traktor-only refactor → Rekordbox → export
- **Decision**:
  1. Fix the flexible-beatgrid handling as a **standalone change first** (small,
     independent, and currently writing inconsistent grids to real collections).
  2. Refactor to the generic model + adapter interface with **Traktor as the only
     adapter** — no new formats, every existing test must still pass, so
     `test_save_fidelity.py` proves the projection+command split changed nothing.
  3. Add **Rekordbox** as the second platform.
  4. Build the **export** path last (it only becomes meaningful once two platforms
     exist).
- **Rationale**: takes the architectural risk where it is verifiable against existing
  invariants, before any unverifiable format is introduced. A one-adapter abstraction is
  always wrong in ways that only surface with the second adapter.
- **Second platform — Rekordbox over Serato**: chosen despite the riskier write target
  (SQLCipher-encrypted DB, cloud sync, version-fragile schema) because `pyrekordbox` is
  the more mature library. *Alternative considered*: Serato second — no library-file
  risk, but writes go into users' audio files and the format is entirely
  reverse-engineered.
- **Tauri packaging: dropped from scope for now.**
- **CDJ/USB export (`export.pdb` + ANLZ files): explicitly OUT OF SCOPE.** Raised in a
  forked conversation and rejected as impractical to implement. Recorded here only so
  it is not rediscovered and re-proposed.
- **Bulk metadata editing: dropped from scope for now.** Not built before the refactor;
  revisit after the adapter architecture exists so it only has to be written once.

### One library open at a time
- **Decision**: `AppState` continues to hold exactly one loaded library. Export always
  reads the currently-open library.
- **Rationale**: Simplest plumbing, no ambiguity about which library an edit or export
  targets, and the existing collection-picker flow is unchanged.
- **Alternatives considered**: multiple loaded with one active (faster switching, but
  dirty-flag/save-bar/unsaved-changes state all become per-library); fully concurrent
  multi-library (powerful, but multiplies UI and state complexity and invites the
  merge/sync expectations already ruled out).

### Capability detection: version first, probe as fallback
- **Decision**: Derive capabilities from the library's declared version where one
  exists (NML version, Rekordbox schema version); where no version is stamped — Serato
  — infer capabilities from the data actually present.
- **Rationale**: Accurate per-library capability reporting without assuming a floor that
  denies users features their software actually has.
- **Alternatives considered**: probe-only (uniform and empirical, but an empty or simple
  library under-reports); assume-newest-and-degrade-on-failure (least work, risks
  writing data older software can't read); static per-platform floor (trivial, but
  penalises users on current versions).
- **Consequence**: each adapter needs its own version-detection story.
- **Serato exception**: Serato has no version-stamped library file, so rather than
  probing the data, Konduktor simply **assumes the latest Serato version** and reports
  the full capability set. *Rationale*: keeps it simple, and probing under-reports on
  small or fresh libraries. *Risk accepted*: a user on an older Serato build could be
  written prep data their install cannot read; mitigated by Serato's aggressive
  auto-update and the relative stability of the GEOB tag format.

### Field mapping: normalise, but keep the native value alongside
- **Implementation requirement**: this demands **per-field dirty tracking**. The rule
  "reuse the native value unless the user edited this field" needs to know *which
  fields* changed, not just which tracks — `PlaylistStore` currently tracks dirtiness at
  track level. It must exist from the start: retrofitted, it cannot distinguish
  edited-to-the-same-value from untouched. It pairs well with projection+command, since
  a command already names the field it changes, so the flag falls out of the command
  rather than requiring a diff.
- **Decision**: Ratings, keys, colours and comments are stored in canonical form in the
  generic model (rating 0–5, key as canonical pitch-class + mode, colour as RGB), and
  **each field also retains its untouched native value**. On write-back the native value
  is reused unless the user actually edited that field, so untouched tracks round-trip
  exactly and only genuine edits go through conversion.
- **Rationale**: Gets a clean, convertible canonical model without introducing
  round-trip drift on data the user never touched — consistent with the project's
  fidelity-first posture.
- **Alternatives considered**: normalise-in/denormalise-out only (clean model, but
  round-tripping through a lossy scale shifts values); preserve native and convert only
  on export (zero round-trip risk, but every UI consumer must interpret
  platform-specific values).

### Prep UI: generic model with capability-gated controls
- **Decision**: The prep UI (waveform, cues, grid) works purely off the generic model,
  with capability flags disabling or hiding the individual controls the loaded platform
  cannot persist (e.g. no memory-cue control on Traktor). No platform branching inside
  components.
- **Rationale**: One UI to maintain, and users are never offered an edit that will
  silently vanish on save.
- **Alternatives considered**: no gating, letting adapters silently collapse on write
  (simplest, but is precisely the failure mode the capability system exists to prevent);
  gate-plus-warn with controls left enabled (more discoverable, but risks nagging during
  normal prep work).

## Findings

### VERIFIED: Traktor flexible-grid representation in NML
Checked against `All I Need` (Shiba San, Tim Baresko) in
`/Users/ben/Documents/Native Instruments/Traktor 4.5.0/collection.nml`. A flexible grid
is stored as **multiple `CUE_V2 TYPE="4"` markers, each with its own `<GRID BPM>`
child**:

```
TEMPO BPM="124.999908"
CUE_V2 TYPE="4" NAME="AutoGrid"    START="71.815535"     GRID BPM="124.999908"
CUE_V2 TYPE="4" NAME="Beat Marker" START="46151.849285"  GRID BPM="62.499954"
CUE_V2 TYPE="4" NAME="Beat Marker" START="76871.871785"  GRID BPM="249.999817"
```

- The first marker is named `AutoGrid`; subsequent ones are `Beat Marker`.
- `TEMPO BPM` mirrors the **first** marker's BPM.
- The half/double BPMs (62.5 / 250 against a base of 125) are genuine half-time and
  double-time sections.
- A non-flexible track (`Cash Money`) has exactly one `TYPE="4"` marker — so the same
  structure covers both cases; a constant grid is just a marker list of length one.
- Separately, tracks carry a `TYPE="0"` cue named `AutoGrid` in **hotcue slot 0** with
  `COLOR="#FFFFFF"`, positioned at almost (not exactly) the type-4 anchor's START.
- `COLOR` is populated in practice, confirming Traktor does carry per-cue colour.

### Current `set_grid` is incorrect for flexible beatgrids (present-day bug risk)
`PlaylistStore._grid_marker()` ([backend/konduktor/playlist_store.py:395](../../backend/konduktor/playlist_store.py#L395))
returns only the **first** `CUE_V2` with a `grid` child. Consequences on a track with
a flexible (multi-marker) grid:
- `set_grid(bpm=X)` updates `TEMPO` and only the *first* marker's BPM, leaving
  subsequent markers at their old BPM → internally inconsistent grid written to disk.
- `set_grid(anchor_sec=X)` moves only the first marker.
- `delete_grid()` removes all grid markers, but **leaves the companion `TYPE="0"`
  `AutoGrid` hotcue-slot-0 cue behind** as debris, since it only strips cues with a
  `grid` child.
- Concretely on `All I Need`: `set_grid(bpm=130)` would set `TEMPO` and the `AutoGrid`
  marker to 130 and leave the Beat Markers at 62.5 and 250 — a destroyed grid.
- The frontend beatgrid drawing (`cues.ts`) is built around a single BPM + anchor, so
  a flexible grid would also render incorrectly.

CLAUDE.md describes the grid as "the grid marker (`CUE_V2` type 4 with a `<GRID BPM>`
child)" — singular — so this assumption is baked into the docs as well as the code.

**Implication for step 1**: this is not a small bug fix. `set_grid(bpm, anchor_sec)`
structurally cannot express per-marker edits, so the fix requires a new marker-level
command vocabulary and marker selection in the `GridControls` UI.

## Next Steps
1. **Design the grid marker command vocabulary** (plan mode, with
   `backend/konduktor/playlist_store.py` and `frontend/src/components/GridControls.tsx`
   open). `set_grid(bpm, anchor_sec)` must be replaced by marker-level commands, and
   `GridControls` needs marker selection.
2. **Fix flexible-beatgrid handling** as a standalone change, including the
   `delete_grid` debris bug (the companion `TYPE="0"` `AutoGrid` hotcue-0 cue is left
   behind). Add a test fixture based on `All I Need` (three grid markers at 125 / 62.5 /
   250 BPM). Update CLAUDE.md, which documents the grid as singular.
3. **Refactor to generic model + adapter interface, Traktor-only.** All existing tests
   must still pass unchanged — `test_save_fidelity.py` is the proof that the
   projection+command split changed nothing.
4. **Add the Rekordbox adapter.** Research `rb_local_usn` / cloud-sync columns first.
   Write the Rekordbox verification test (row-level table-dump diff).
5. **Add the Serato adapter.** GEOB-frame sidecar backups; write the Serato verification
   test (byte diff excluding the patched frame, plus whole-file crate diff).
6. **Build the export path.** Only meaningful once two platforms exist.

### Command vocabulary: strictly generic, no adapter escape hatches
- **Decision**: Every command is defined in generic-model terms and each adapter
  translates it. Adapters may not expose platform-specific commands.
- **Rationale**: Keeps the UI fully platform-agnostic and forces genuine abstraction
  rather than letting platform specifics leak upward.
- **Alternatives considered**: a generic core plus adapter escape hatches (pragmatic,
  but the hatch gets used and generic coverage quietly erodes); starting generic and
  adding hatches only on demonstrated need.
- **Two-platform promotion rule** (resolves the single-platform-feature problem):
  - A feature that exists on only **one** platform is **preserved but not editable** —
    no command, no generic concept, no editing UI. Under projection+command it survives
    round-trip intact for free, because unmodelled native data never leaves the native
    model. The strictly-generic vocabulary therefore constrains what Konduktor can
    **edit**, not what it **preserves**.
  - A concept is **promoted into the core vocabulary once at least two platforms have
    it**, at which point there is real evidence of what the shared abstraction should
    be, rather than a concept invented to accommodate a single format.
  - *Rationale*: avoids both failure modes — inventing a fake abstraction to house a
    one-platform feature (e.g. a "performance sequence" concept only Serato ever uses),
    and opening an escape hatch that quietly erodes generic coverage.
  - *UI affordance*: show preserved-but-uneditable data as read-only, so users can see
    that e.g. their Serato Flip data is intact rather than assuming Konduktor discarded
    it. Makes the limitation honest instead of invisible.
  - *Consequence accepted*: conversion coverage is capped at what two or more platforms
    share — which matches the stated parity goal rather than compromising it.

### Save model: projection + command replay
- **Decision**: The generic model is a **read projection**. Edits are expressed as
  commands and replayed by a per-platform adapter onto the **retained native model**,
  which remains the write target. Export is a separate, lossy path reading from the
  projection.
- **Rationale**: It is the only model that matches how all three storage formats
  actually behave — Rekordbox `master.db` requires row `UPDATE`s (cannot be
  regenerated: internal IDs, FKs, ANLZ pointers, `rb_local_usn` sync columns), Serato
  requires patching individual GEOB frames inside audio files, and Traktor already
  deliberately avoids regeneration. It also preserves the byte-exact NML guarantee and
  `test_save_fidelity.py`, makes unknown native fields survive **for free** (they are
  never lifted out of the native model), and keeps edit *intent* explicit. Critically,
  Konduktor already implements this shape for Traktor: `CollectionService` is the read
  projection, `PlaylistStore` holds the native model with command methods, the HTTP API
  is already a command bus, and `replace_track()` is the projection refresh. This is a
  generalization, not a rewrite.
- **Alternatives considered**: *Generic model as source of truth* — rejected: would
  lose unmodelled fields on real irreplaceable data, discards the byte-exact guarantee,
  cannot work for `master.db`, and its headline benefit (free conversion) is illusory
  since per-target serializers are needed either way; its only real prize is simpler UI
  mutation code. *Source of truth + diff-based writeback* — rejected: diffing destroys
  edit intent exactly where platforms disagree (grid phase-shift vs. retempo is
  indistinguishable in a marker-position diff), ordered-list diffs are fiddly, and it
  needs a full deep snapshot at load.
- **Cost accepted**: an N×M adapter matrix (~15 commands × 3 platforms), plus careful
  choice of command altitude.

### Model shape: rich core + capability system
- **Decision**: Model the union of *concepts* at full fidelity in the generic model —
  beatgrid is always a marker list, a cue always carries position/type/colour/role/slot
  — and build a per-platform **capability system** so the UI never offers an edit the
  loaded platform cannot persist.
- **Rationale**: The conversion goal requires one shared vocabulary; anything parked in
  a platform-specific extension namespace cannot be converted. Ben confirmed
  willingness to build the capability UI.
- **Alternatives considered**: *Union superset* — rejected, forces every UI consumer to
  branch on the loaded platform. *Core + per-platform extensions* — rejected, because
  the data that would land in extensions (Rekordbox memory cues, Serato saved loops) is
  central rather than exotic, and extensions do not convert.
- **Note from Ben**: Traktor 3.4+ supports **flexible beatgrids**, so the marker-list
  model is right for all three platforms rather than being a Traktor-side compromise.
  This also means the current single-marker assumption in the codebase is already wrong
  (see Findings).

### Undo/redo: deferred but not precluded
- **Decision**: Do not implement undo/redo now, but design the command layer so
  inverse commands can be added later.
- **Rationale**: Keeps the adapter surface smaller for the initial build while
  retaining the option; the command model makes it cheap to add.
- **Alternatives considered**: designing for inverses from day one (more discipline in
  every adapter method); ruling it out entirely (loses a strong safety feature).

### Format layers: reuse mature libraries
- **Decision**: Use existing libraries for the Rekordbox and Serato format layers
  (e.g. `pyrekordbox` for `master.db`/SQLCipher/XML/ANLZ, an existing Serato
  crate+GEOB library), wrapped behind Konduktor's adapter interface.
- **Rationale**: Fastest path to coverage; the project already accepts this class of
  dependency risk with `traktor-nml-utils`.
- **Alternatives considered**: reuse for reading but own all write paths (more control
  over surgical patching and fidelity, more work); build everything in-house (maximum
  control, very large reverse-engineering effort); spike first and decide per platform.

## Agent Onboarding

### Summary
Konduktor is today a Traktor-only tool whose defining property is **save fidelity**: it
never regenerates `collection.nml`, it surgically patches a parsed dataclass model and
renders through the library's own pipeline, so a no-op save is byte-identical and only
edited objects diff (`test_save_fidelity.py`, CLAUDE.md gotcha 3).

Ben wants to extend it to Rekordbox and Serato, with library conversion as the ultimate
payoff. His opening proposal was a generic data structure that all platforms load into,
are edited through, and are saved back out of.

The central tension explored was that **"load into a generic model and serialize back
out" would destroy the fidelity guarantee**. The discussion resolved it by establishing
that for two of the three platforms, whole-model serialization is not even a coherent
operation: Rekordbox's `master.db` is SQLite with internal IDs, FKs, ANLZ pointers and
sync columns (you `UPDATE` rows, you cannot regenerate), and Serato's prep data lives as
binary GEOB frames inside the audio files themselves (you patch a frame). Both are
inherently mutation-oriented. This made **projection + command replay** the only model
that generalizes — and, critically, Konduktor already implements exactly that shape for
Traktor (`CollectionService` = read projection, `PlaylistStore` = native model + command
methods, the HTTP API = command bus, `replace_track()` = projection refresh). The work is
a generalization, not a rewrite.

A second thread established that the generic model should be **rich** (model the union
of concepts at full fidelity, adapters collapse on write) rather than
lowest-common-denominator or core-plus-extensions, because anything parked in a
platform-specific namespace cannot be converted. This makes a **capability system**
mandatory so the UI never offers an edit that silently won't persist.

A third thread covered **exports**: always a fresh, self-contained target library with
copied audio, scoped by a user-curated **Export Set**. Because exports never merge into
an existing target, cross-platform track identity is not needed at all — the existing
path-based primary key suffices.

Along the way the discussion surfaced a **present-day bug**: Ben noted Traktor 3.4+
supports flexible beatgrids, and verification against his real collection confirmed
Traktor stores them as multiple `CUE_V2 TYPE="4"` markers each with its own `<GRID BPM>`.
`PlaylistStore._grid_marker()` only ever reads the first, so `set_grid(bpm=X)` writes
internally inconsistent grids to real collections today. This became step 1 of the plan,
and it is bigger than a bug fix because the `set_grid(bpm, anchor_sec)` signature
structurally cannot express per-marker edits.

Note: a "CDJ/USB export" requirement appeared in this log mid-discussion from a forked
conversation. Ben confirmed it was rejected as impractical and had it removed. **Do not
re-propose it.**

### If Continuing This Discussion
All design questions are resolved. Three items remain, none of them decisions:
- Grid marker command vocabulary — a design task for plan mode, not an open question.
- Per-platform verification tests — implementation work landing with each adapter.
- Rekordbox `rb_local_usn` / cloud-sync columns — research for the Rekordbox phase.

Deliberately deferred, do not reopen without Ben raising them: the export loss report
("not for now"), bulk metadata editing, Tauri packaging, and CDJ/USB export (rejected).

### If Moving to Planning/Implementation

**Requirements**

1. **Save model — projection + command replay.** The generic model is a *read
   projection*. Edits are commands replayed by a per-platform adapter onto the
   **retained native model**, which stays the write target. Never serialize the generic
   model back over a native library. This preserves byte-exact NML saves and means
   unmodelled native fields survive for free.
2. **Command vocabulary — strictly generic.** All commands are defined in generic-model
   terms; adapters translate. **No platform-specific escape hatches.** Single-platform
   features are *preserved but not editable*; a concept is promoted into the core
   vocabulary only once **two or more platforms have it**. Surface preserved-but-
   uneditable data as read-only in the UI.
3. **Generic model — rich core.** Model the union of concepts at full fidelity.
   Beatgrid is **always a marker list** (a constant grid is a list of length one — this
   is correct for all three platforms, not a compromise). Cue shape:
   `{ position_sec, length_sec?, role: hotcue|memory, slot?, type: cue|fade_in|fade_out|
   load|loop, color?, name? }`.
4. **Capability system — mandatory.** Capabilities are `f(platform, version)`, derived
   from the declared version where one exists; **Serato assumes the latest version** for
   simplicity. The prep UI works purely off the generic model with capability-gated
   controls — no platform branching inside components.
5. **Field mapping — normalise, keep native alongside.** Canonical forms in the model
   (rating 0–5, canonical key, RGB colour) *plus* the untouched native value. On
   write-back, reuse the native value unless the user actually edited that field.
   **This requires per-field dirty tracking** — `PlaylistStore` is track-level today, and
   it must exist from the start because it cannot be retrofitted (edited-to-same-value
   becomes indistinguishable from untouched). It falls out of the command, which already
   names the field it changes.
6. **Write scope — full read/write for all three platforms**, in place, to the user's
   canonical library. One library open at a time (`AppState` keeps holding exactly one).
7. **Safety — same invariant, per-platform mechanism.** "An edit changes only what it
   was supposed to change." Traktor: byte diff (exists). Rekordbox: full table dump,
   row-level diff; back up `master.db` + WAL/SHM; **warn but proceed** if Rekordbox is
   running. Serato: byte diff of the audio file excluding the patched GEOB frame, plus
   whole-file crate diff; **back up GEOB frames to a sidecar, not whole audio files**
   (KB not GB; accepted risk: no protection if the tag library mangles an unrelated
   region — an exposure `file_tags.py` already carries).
8. **Export — always a fresh, self-contained library** at a user-chosen destination,
   **including copied audio, for every target** (not just Serato). Never merges into an
   existing target library, so no cross-platform track identity is required.
   - Scoped by an **Export Set**: a named, persisted list of track and playlist
     references. **Live references** — resolved at export time, so the preview must be
     computed then, never stored. Playlist tracks auto-included, deduplicated, copied
     once.
   - Stored in Konduktor's own store (not `userprefs.json`), keyed by a stable library
     ID held in a **sidecar file next to the user's collection**.
   - Audio layout: a **global persistent setting** — flat single folder (**default**) or
     mirror source structure. Flat collisions get a **counter suffix** (`Xtal-2.mp3`).
   - Playlist folders preserved where the target supports them, otherwise flattened to
     **path-encoding names** (`House / Peak Time`).
   - **Pre-scan for missing audio files**, report, let the user decide. **Pre-check disk
     space**; progress with a working cancel (cancel leaves a partial export — clean up
     or mark clearly).
   - Re-export to an occupied destination: **warn, overwrite on confirm**.
   - Provide an explicit **"export entire library"** action that skips export-set creation.
9. **Lossiness — degrade where sensible, drop otherwise.** Goal is maximum feature
   parity. No loss report for now (deferred, cheap to add later since adapters know what
   they collapsed).
10. **Format layers — reuse mature libraries** (`pyrekordbox` for master.db/SQLCipher/
    XML/ANLZ; an existing Serato crate+GEOB library) behind Konduktor's adapter
    interface, consistent with the existing `traktor-nml-utils` dependency posture.
11. **Undo/redo — not now, but do not preclude it.** Design commands so inverses can be
    added later.

**Sequencing** (see Next Steps for the actionable breakdown)
Grid fix → Traktor-only refactor → Rekordbox → Serato → export. Rekordbox was chosen as
the second platform over Serato because `pyrekordbox` is the more mature library, despite
the riskier write target.

**Out of scope**: Tauri packaging, bulk metadata editing, CDJ/USB export
(`export.pdb` + ANLZ — rejected as impractical), two-way sync, Konduktor owning its own
canonical library format.

**Rejected alternatives worth not relitigating**
- Generic model as source of truth, and source-of-truth + diff-based writeback — both
  rejected; see the save model decision. The claimed "free conversion" benefit is
  illusory (per-target serializers are needed either way), and diffing destroys edit
  intent exactly where platforms disagree.
- Union superset and core-plus-extensions model shapes.
- Cross-platform track matching by path/tags/content-hash/fingerprint — unnecessary
  given fresh-export-only.
- Full audio-file backups for Serato writes.
- Reference-in-place exports for non-Serato targets.

**Standing lesson from this discussion**: establish format capabilities **empirically**
against real files rather than assuming them. An assumption that Traktor could not store
cue colours was wrong (`CueV2Type` has a `color` field, populated in practice), and the
flexible-grid assumption was only settled by reading Ben's actual collection.
