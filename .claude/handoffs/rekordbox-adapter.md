# Handoff: the Rekordbox adapter

**For**: an agent picking up Konduktor with no prior context.
**Task**: add Rekordbox as the second platform — step 4 of the multi-platform plan.
**Status of the plan**: steps 1–3 are done and committed (`08c9b81`, `c9e85bb`, `6e24f5e`).

All *design* decisions for this work were settled in
[`.claude/discussions/discuss-multi-platform-library-2026-09-17.md`](../discussions/discuss-multi-platform-library-2026-09-17.md).
Read its **"If Moving to Planning/Implementation"** section before designing
anything. This handoff is the *current-state* companion to it: what exists now,
what to do next, and what will bite you. Where the two disagree, the discussion
doc wins on intent and this one wins on facts about the code.

---

## 1. Where the project is

Konduktor is a library-management and track-prep tool for DJ software. Until
recently it was Traktor-only. It now has a **generic model + per-platform adapter**
architecture with Traktor as the only adapter, and the generic shape reaches all
the way to the UI — nothing above `adapters/traktor/` knows Traktor exists.

**The architecture is projection + command replay.** The generic model is a *read
projection*. Edits are strictly-generic commands that an adapter replays onto its
**retained native model**, which stays the write target. The generic model is
**never** serialized back over a library. That is what preserves byte-exact saves,
and it is the only model that works for a format you cannot regenerate — which is
exactly Rekordbox's situation.

```
backend/konduktor/
  core/                      generic. MUST NOT import a native library type.
    model.py                 Track, CuePoint, GridMarker, TrackCues, PlaylistNode…
    adapter.py               LibraryAdapter / LibraryDriver protocols + error tree
    capabilities.py          Capabilities — what a loaded library can persist
    query.py                 TrackIndex: query/filter/sort/facets/stats
    edit_journal.py          EditJournal: every edit at field resolution
    registry.py              adapter selection by can_open() probe
    audio_tags.py pathmap.py auto_hotcues.py
  adapters/traktor/          the reference implementation — read this first
    store.py                 retained native model (TraktorStore)
    adapter.py               TraktorAdapter: owns store + TrackIndex
    projection.py            native -> generic
    driver.py capabilities.py beatgrid.py locations.py cue_types.py
  app_state.py               the one loaded library + version-history commits
  main.py                    thin routes; talks only to the adapter
```

Frontend (`frontend/src/`) is already generic: `api.ts` carries string cue types,
`lib/capabilities.tsx` exposes `useCaps()`, `lib/platformCopy.ts` composes
per-platform wording from adapter-supplied *facts*. No component branches on the
platform name.

**Tests**: `cd backend && ./run_tests.sh` → 146 assertions across
`test_layering.py`, `test_save_fidelity.py`, `test_traktor_adapter.py`,
`test_phase3.py`, `test_history.py`. Frontend: `npm run build && npm run test`
(25 vitest assertions on `lib/beatgrid.ts`).

---

## 2. Start here: research before code

The discussion doc lists one genuinely open question, and it gates the whole
write path:

> **Rekordbox `rb_local_usn` / update-sequence + cloud-sync columns**: writing rows
> without maintaining these may corrupt Rekordbox's own sync state.

Settle this **first**, empirically, against a real `master.db` — do not design the
write path around an assumption. Specifically find out:

- What `rb_local_usn` is per row, what the per-table/global sequence is, and what
  Rekordbox does on next launch when it sees a row it did not write.
- Whether `pyrekordbox` maintains these for you on write, or whether that is the
  caller's job. (Assume nothing; read its source.)
- What `USN`/`rb_data_status`/`rb_local_deleted` and the other `rb_*` columns mean.
- What happens on a cloud-synced library specifically, since that is the failure
  mode that could lose data on *other* machines — which is worse than a local
  corruption the version history can undo.

Write the findings up as a **Findings** section in this file (the discussion doc
is Concluded; don't edit it). If the answer turns out to be "Konduktor cannot
safely write a cloud-synced library", that is a legitimate result and should be
surfaced to Ben as a scope decision, not worked around silently.

**Standing lesson from the discussion, which has already been proved right twice**:
establish format capabilities empirically against real files rather than assuming
them. Both previous assumptions that got checked (Traktor cue colours, flexible
grid marker naming) turned out to be wrong.

---

## 3. What to build

### 3.1 The adapter

Create `backend/konduktor/adapters/rekordbox/` mirroring the Traktor package, and
register it in `adapters/__init__.py` (one line; the registry does the rest).

Implement the two protocols in `core/adapter.py`. `LibraryDriver` is small:
`can_open` / `open` / `detect` / `describe` / `restore`. `LibraryAdapter` is the
real surface — 38 methods, all already defined in generic terms:

```
capabilities reload
tracks track query_tracks facets stats
playlist_tree playlist_entries playlist_tracks track_cues
create_playlist rename_playlist delete_playlist set_playlist_entries
set_track_metadata set_cover_art cover_art
set_cue set_cue_type delete_cue place_cues
add_grid_marker move_grid_marker set_grid_marker_bpm delete_grid_marker
replace_grid set_analysed_grid delete_grid set_grid_lock
audio_path set_path_mapping path_prefix_suggestions remap_preview remap_locations
dirty save snapshot
```

Two rules the Traktor adapter follows and yours must too:

1. **Every mutating command returns its refreshed projection.** This makes the
   refresh an adapter-internal invariant rather than something a route has to
   remember. A forgotten refresh is a silently stale UI that no test would catch.
2. **`core/` stays clean.** `test_layering.py` fails the build if anything in
   `core/` imports a native type. If you find yourself wanting to widen a `core/`
   model for a Rekordbox-only reason, that is the two-platform promotion rule
   telling you not to — see §4.

`can_open` must not be extension-based: Rekordbox's library is `master.db` and
Serato's is a *directory*. Probe cheaply (Traktor's reads 4 KB and looks for the
NML header; yours will need to recognise a SQLCipher file without decrypting it
twice).

### 3.2 Capabilities

`core/capabilities.py` already has the fields. Rekordbox's answers, from the
discussion doc's platform notes — **verify each against a real library**:

| Field | Expected for Rekordbox | Note |
|---|---|---|
| `cues.hotcue_slots` | 8 | |
| `cues.slot_labels` | `'letter'` | **First real customer.** The field exists but no UI has exercised it — expect to find bugs. |
| `cues.memory_cues` | `true` | See §4: still *not editable*. |
| `cues.types` | `['cue','loop']` | No fade_in/fade_out/load — those are Traktor-only. |
| `cues.color` | `'free'` or `'palette'` | Determine empirically; if palette, populate it. |
| `grid.flexible` | likely `true` | Verify. |
| `grid.lockable` | ? | Rekordbox's analysis lock is a *different concept* from Traktor's LOCK — check before mapping it. |
| `save.overwrite_risk` | `'while_running'` | Rekordbox holds the DB open and cloud-syncs. The copy already exists in `lib/platformCopy.ts`. |
| `save.library_label` | `'master.db'` | |

### 3.3 Verification — non-negotiable

The project's core safety property is: **an edit changes only what it was supposed
to change.** For Traktor that is a byte diff. A byte diff is meaningless against
SQLite, so build the agreed analogue:

- **`test_rekordbox_fidelity.py`** — full table dump before/after, row-level diff.
  A no-op save must produce zero row changes. A single metadata edit must change
  exactly one row (plus whatever the USN research says *must* also change).
- **`test_rekordbox_adapter.py`** — the generic-layer test, modelled on
  `test_traktor_adapter.py`: one parse per open, the projection refreshing after
  each command family, cue-type translation, capabilities, `Unsupported` where the
  platform cannot represent an edit.
- Add both to `run_tests.sh`.
- **Back up `master.db` *and* its `-wal`/`-shm`** before any write. A `.db` copied
  without its WAL is not a consistent snapshot.

**Never test against Ben's real library.** Every existing test copies to a temp
dir first. Do the same.

---

## 4. Landmines (earned, not speculative)

**`history.py` assumes a single tracked file.** It commits one blob named after the
library file into a git repo keyed by a hash of the library's OS path. Neither
assumption survives `master.db` + WAL/SHM. There is a read-side fallback chain for
the blob name, but the *multi-artefact* case is genuinely unimplemented. Decide
early whether Rekordbox history commits a snapshot set or is disabled for this
platform — and if disabled, `capabilities.save.history` must say so, because the
UI reads it.

**Four spots in the adapter protocol are over-fitted to Traktor.** These were
recorded during the refactor precisely so you would not rediscover them. Expect to
revise the protocol; v1 was never frozen:

- `GridMarker.companion` — a Traktor convention (a white cue paired with a grid
  marker, occupying a real hotcue slot). Should be `null` on Rekordbox.
- **Hotcue `slot` as a plain `int`** — memory cues have no slot. `CuePoint.slot` is
  already nullable, but every *command* is slot-addressed. This is the most likely
  thing to need changing, and the frontend design considered (and deferred) opaque
  cue IDs as the fix. Revisit that decision with real evidence.
- **Grid marker `index` as identity** — fine for a list-shaped grid, untested
  against rows with their own primary keys.
- `set_analysed_grid` — invented for one caller (Auto Grid), to let Traktor write
  the companion cue without leaking the concept upward. Check it still earns its
  keep with two platforms.

**Memory cues stay preserved-but-uneditable even after this lands.** The
two-platform promotion rule promotes a concept once **two or more** platforms have
it. Rekordbox is the *only* platform with memory cues (Traktor has none, Serato has
a saved-loop bank instead). So: project them, show them read-only, do **not** build
a memory-cue editing UI. This will feel wrong — build it anyway, the rule exists to
stop one-platform concepts becoming fake abstractions.

**Per-cue colour, by contrast, *is* now promotable.** Traktor and Rekordbox both
have it, which is two platforms. `cueColor()` in `frontend/src/lib/cues.ts` already
prefers a stored colour over the type palette, and `capabilities.cues.color` is
already plumbed — but there is **no colour-editing control yet**. Adding one is
legitimately in scope for this step.

**`slot_labels: 'letter'` has never run.** Rekordbox labels hotcues A–H. The field
is threaded through `HotcueBar`, `drawCues` and the keyboard map, but nothing has
ever set it to `'letter'`. Assume it is subtly broken.

---

## 5. Working agreements

- **`./run_tests.sh` must be green at the end of every step.** The Traktor refactor
  was done in 18 steps with the suite green at each one; that discipline is why a
  1,500-line change to the save path landed without a fidelity regression.
- **`schemas.py` and `api.ts` land in the same commit, always.** They are one
  contract in two languages.
- **The collection is real, irreplaceable data.** Point `KONDUKTOR_NML` (or the
  Rekordbox equivalent) at a **copy** when testing writes. See CLAUDE.md gotcha 2.
- Read `CLAUDE.md` in full before starting — its Architecture and "Write path"
  sections describe the current state accurately and its "Critical gotchas" are
  all still live.

## 6. Do not relitigate

Settled in the discussion doc; reopening these wastes a session:

- Projection + command replay as the save model (and why generic-model-as-source-of-truth
  and diff-based writeback were both rejected).
- Strictly-generic command vocabulary, **no adapter escape hatches**.
- Rich core model + mandatory capability system.
- Rekordbox before Serato (`pyrekordbox` is the more mature library, despite the
  riskier write target).
- **Warn but proceed** if Rekordbox is running — do not hard-refuse.
- Export is step 6, after Serato. Export Sets, fresh-target-only, copied audio.
- **CDJ/USB export (`export.pdb` + ANLZ) is rejected as impractical.** It has been
  re-proposed once already. Do not raise it again.
- Deferred, do not reopen without Ben raising them: the export loss report, Tauri
  packaging, two-way sync.

**Bulk metadata editing** was deferred *to* this point specifically so it would
only have to be written once, against the adapter architecture. It is now
unblocked, and is a much smaller piece of work than the Rekordbox adapter. If Ben
wants something shippable sooner, that is the alternative — confirm with him rather
than assuming.

---

## 7. One outstanding verification from step 3

**The React UI has never been rendered in a browser.** The generic wire format was
verified end-to-end through the Vite proxy (every endpoint, full write round trip,
byte-fidelity diff), but no browser driver was available on the dev machine —
no `chromium-cli`, no Playwright, and `npx` refused to install one.

Before building on top of the frontend, open `http://localhost:5173` (`./dev.sh`)
and click through the prep deck, the library table and the playlist tree. If you
can get a browser driver working, capturing the launch recipe via
`/run-skill-generator` would be worth it — the app needs `KONDUKTOR_NML` and
`KONDUKTOR_DATA_DIR` pointed at a temp copy to be driven safely, which is exactly
the kind of thing a project skill should hold.

---

## 8. Findings — Rekordbox research spike (2026-09-17)

Answers to §2, established empirically against a real Rekordbox 7 library
(`~/Library/Pioneer/rekordbox/master.db`, 52 tracks, 23 cues, 1 playlist) with
`pyrekordbox` 0.4.4. **Every experiment ran against a copy in a scratch dir; the
real library was only ever read.** Rekordbox was not running.

Each finding is marked **VERIFIED** (observed directly), **INFERRED** (strongly
implied but not directly exercised) or **UNTESTED**.

### 8.1 The USN question is answered — and it is not a blocker

**VERIFIED — the sequence model.** There is one global counter and one per-row
stamp:

- `agentRegistry` row `registry_id='localUpdateCount'`, column `int_1` — the
  global local USN. Observed at **1489**, with the highest `rb_local_usn` found
  anywhere in the library at 1487. The global counter is the high-water mark.
- Every syncable table carries `rb_local_usn`. One edit = increment the global
  counter by one, stamp the changed row with the new value.

**VERIFIED — `pyrekordbox` maintains this for you.** It is not the caller's job.
`RekordboxAgentRegistry` hooks SQLAlchemy's update/create/delete events into an
update buffer, and `db.commit()` (default `autoinc=True`) calls
`autoincrement_local_update_count(set_row_usn=True)`, which walks the buffer,
increments the counter once per change and stamps each affected row
(`db6/registry.py:311-347`). Konduktor should **use `commit()` and not touch USNs
by hand.**

**VERIFIED — a single metadata edit is exactly as localized as the Traktor
invariant demands.** Full 47-table row-level dump before/after a one-field Title
edit produced exactly two changed rows:

```
agentRegistry   localUpdateCount  UPDATE  int_1: 1489->1490
djmdContent     231764591         UPDATE  Title: 'NOISE'->'...', rb_local_usn: 210->1490
```

A no-op open+close, and an open + `commit()` with no edits, both produce **zero
row changes**. Invariants A and C from `test_save_fidelity.py` therefore have
direct, achievable Rekordbox analogues.

**VERIFIED — the cloud columns are inert on a local library.** `usn` (the
*server* sequence number, distinct from `rb_local_usn`) is **NULL on every row of
every table**, and `rb_data_status`, `rb_local_data_status`, `rb_local_deleted`
and `rb_local_synced` are **0 on every row**. This library has a signed-in
account (`agentRegistry.agentCredentials` is populated) but Cloud Library Sync is
not enabled for the collection.

**UNTESTED — the cloud-synced case.** Because nothing in this library is synced,
the spike could not observe what the server does with a locally-bumped USN it did
not issue. The risk in the handoff stands unrefuted for that configuration. Two
mitigations are available and cheap, and both should be built:

1. Detect sync state on open — `usn` non-NULL anywhere, or a populated
   `djmdCloudProperty`/`cloudAgentRegistry` — and surface it.
2. Gate writes on a cloud-synced library behind an explicit confirmation, or
   report `save.overwrite_risk` accordingly. **Recommend raising this with Ben as
   a scope decision before the write path is built**, per the handoff.

**VERIFIED — detecting a running Rekordbox is already solved**:
`pyrekordbox.utils.get_rekordbox_pid()`. That is all "warn but proceed" needs.

### 8.2 The real surprise: the write surface is three artefacts, not one

This is the finding that should change the plan. Konduktor's prep features do not
all live in `master.db`.

**VERIFIED — the beatgrid is NOT in the database.** There is no beatgrid, beat,
grid or tempo table anywhere in the 47-table schema. `djmdContent.BPM` is a
scalar display value stored ×100 (`12500` = 125.00 BPM) and is **`0` for 22 of
the 52 tracks** despite all 52 being analysed. The grid lives in the ANLZ
analysis files that `djmdContent.AnalysisDataPath` points at.

**VERIFIED — and it is a per-beat list, not a marker list.** The `PQTZ` tag in
`ANLZ0000.DAT` stores **every beat explicitly** — beat-number-in-bar, BPM and
time per beat (one track: 496 entries). This is a materially different shape from
Traktor's marker list. The adapter must *derive* `GridMarker`s from runs of
constant BPM on read, and *expand* markers back into a full beat list on write.

This directly revises the handoff's §4 landmine "grid marker `index` as identity
— untested against rows with their own primary keys": Rekordbox has no marker
rows at all. Index-as-identity is actually fine here, because the adapter mints
the marker list itself. The real work is the projection both ways.

**VERIFIED — cues live only in the database, in two places at once.**
`djmdCue` holds normalized cue rows, and `contentCue` holds a **JSON mirror** of
the same cues per track (`contentCue.Cues` is a JSON array of the full cue
objects, with `rb_cue_count` alongside). Both are maintained by Rekordbox and
must be kept consistent by any writer — this is Rekordbox's structural analogue
of Traktor's companion-cue convention, and the same class of trap.

**VERIFIED — the ANLZ cue lists are empty and can be ignored.** Across the whole
library, every `PCOB` (hotcue and memory) and `PCO2` tag in every `.DAT` and
`.EXT` has a cue count of **0**, while the database holds 23 cues. Rekordbox 7
populates the ANLZ cue tags only on device/USB export — which is out of scope.
**Konduktor should write cues to the DB only.**

**VERIFIED — Rekordbox does not stamp `rb_local_usn` on `djmdCue`.** All 23
`djmdCue` rows have a NULL `rb_local_usn`, while `contentCue` rows are stamped
(956–1482). The sync unit for cues is `contentCue`, not the individual rows.
Note that `pyrekordbox`'s `set_row_usn=True` stamps *every* buffered instance
that has the attribute, so committing a new `DjmdCue` through it **would stamp a
row Rekordbox itself leaves NULL**. Whether that matters is UNTESTED, but it is a
deviation from observed native behaviour and worth matching deliberately.

### 8.3 ANLZ write fidelity — a clean, defensible boundary

Parse → `build()` → byte-compare, over every analysis file in the library:

| File | Byte-identical | Differs | Holds |
|---|---|---|---|
| `.DAT` | **52 / 52** | 0 | `PQTZ` beatgrid, waveform, cue tags |
| `.EXT` | **52 / 52** | 0 | `PQT2`, colour waveform, `PSSI`, cue tags |
| `.2EX` | 32 / 52 | **20** | RB7 colour-waveform tags only |

`.DAT` and `.EXT` — the only files Konduktor needs to write — round-trip
**byte-exactly**, so the gotcha-3 guarantee generalizes. `.2EX` loses ~4 KB on
rebuild because `pyrekordbox` 0.4.4 drops tags it does not know (`PVDI`), and
`.3EX` it cannot parse at all (`File type '.3EX' not supported!`). Neither holds
anything Konduktor edits.

**Rule for the adapter: only ever rebuild `.DAT` and `.EXT`; never touch `.2EX`
or `.3EX`.** A `test_rekordbox_fidelity.py` assertion should pin this.

### 8.4 `pyrekordbox` has no cue or beatgrid write API

Its write surface is playlists, folders, smart playlists, albums, artists,
genres, labels, `add_content`, and attribute assignment on ORM objects. There is
no `add_cue`/`set_cue`/`delete_cue` — only `get_cue` / `get_content_cue`.

So Konduktor must implement itself, against the ORM:
- creating/updating/deleting `DjmdCue` rows (including Rekordbox's own ID/UUID
  minting convention),
- regenerating the `contentCue.Cues` JSON mirror and `rb_cue_count`,
- the whole beatgrid path via the `anlz` module (`set_beats`/`set_bpms`/
  `set_times` exist on `PQTZAnlzTag`, plus `AnlzFile.build()`/`save()`).

**This is the single biggest correction to the effort estimate in this handoff.**
§3.1 frames the adapter as implementing 38 already-defined methods against a
mature library. That is true for metadata and playlists; it is not true for cues
and grids, which are the prep deck — Konduktor's most valuable feature — and
which need a hand-written native store per artefact type.

### 8.5 Two hazards found by hitting them

**A stale WAL beside a restored `.db` silently corrupts it.** `pyrekordbox` leaves
`master.db-wal`/`-shm` behind on `close()` — the write is in the WAL, not the
main file. The handoff's "back up the WAL too" is right, and there is a sharper
corollary for restore: **deleting any existing `-wal`/`-shm` before putting a
snapshot back is mandatory**, or SQLite replays the old WAL onto the new file.
Version-history restore for Rekordbox must be a *set* operation, never a
single-file copy. This cost real time in the spike: an early experiment reported
a clean diff that was an artefact of a stale WAL.

**A clean Rekordbox quit leaves no WAL.** `master.db-wal`/`-shm` exist only while
Rekordbox (or another writer) holds the DB open; on a clean quit it checkpoints
and removes them. So §3.3's "back up `master.db` *and* its `-wal`/`-shm`" is
conditional: copy them **if present**, and treat their presence on open as a
signal that something else has the library open. Konduktor's own writer must
checkpoint (`PRAGMA wal_checkpoint(TRUNCATE)`) before anything copies or
snapshots the file, or the snapshot is stale.

**`AnlzFile.keys()` / `len()` infinitely recurse** in 0.4.4 (`__len__` →
`keys()` → `__len__`). Use `file.tags`, `get_tag()` and `getall_tags()`; never
treat an `AnlzFile` as a Mapping.

### 8.6 Dependency posture

`pyrekordbox` 0.4.4 and `sqlcipher3` both install cleanly on Python 3.14 from
prebuilt wheels — no Rust or OpenSSL source build — so they satisfy the
constraint in CLAUDE.md's Conventions. The SQLCipher key is embedded obfuscated
in `pyrekordbox` (`db6/database.py`, `deobfuscate(BLOB)`); no key download or
extraction step is needed.

### 8.7 Capability table, revised against the real library

Replaces the "expected" column in §3.2. Still to be confirmed per item where
marked.

| Field | Value | Evidence |
|---|---|---|
| `cues.hotcue_slots` | 8 | INFERRED (RB convention; not exercised — this library has no full bank) |
| `cues.slot_labels` | `'letter'` | INFERRED |
| `cues.memory_cues` | `true` | VERIFIED (`djmdCue.Kind` distinguishes them) |
| `cues.types` | `['cue','loop']` | VERIFIED (a loop row carries `OutMsec` + `BeatLoopSize`) |
| `cues.color` | `'palette'` (probable) | UNRESOLVED: `djmdCue` has `Color` + `ColorTableIndex`, but the `djmdColor` table (8 rows: Pink/Red/Orange/Yellow/Green/Aqua/Blue/Purple) is the **track**-colour palette, not necessarily the cue one. Settle by colouring a cue in Rekordbox and diffing. |
| `cues.named` | `true` | VERIFIED (`djmdCue.Comment`, e.g. `"1.1Bars"`) |
| `grid.flexible` | `true` | VERIFIED (per-beat BPM in `PQTZ` can vary) |
| `grid.lockable` | ? | UNRESOLVED — no obvious lock column found; check `djmdContent` flags |
| `save.overwrite_risk` | `'while_running'` | as planned |
| `save.library_label` | `'master.db'` | |
| `save.history` | **`false` initially** | The artefact set is `master.db` + N ANLZ files + `masterPlaylists6.xml`; `history.py` is single-blob. See §4. |

**UNRESOLVED — the `djmdCue.Kind` encoding.** Observed: `Kind=1` ×20 (one per
analysed track, near t=0, comment `"1.1Bars"` — Rekordbox's auto-placed cue),
plus `Kind=2`, `Kind=3` and `Kind=5` (the loop) on the one hand-prepped track.
The ANLZ tags could not disambiguate because they are empty. Resolve by setting a
known hot cue A–H and a known memory cue in Rekordbox and re-reading the table —
a five-minute experiment that needs the app.

### 8.8 Recommended next steps

1. ~~Ask Ben the scope question: cloud-synced libraries.~~ **DECIDED 2026-09-17:
   detect and refuse.** If the library is cloud-synced, Konduktor opens it
   read-only and does not write. Detection: `usn` non-NULL on any row, or a
   populated `djmdCloudProperty` / `cloudAgentRegistry`. This needs a capability
   answer the UI can gate on — the existing `save` capabilities have no
   "readable but not writable" state, so either add one or report every
   `editable_fields`/grid/cue capability as empty. Prefer an explicit flag: a
   silently uneditable library looks like a bug.
2. **The one experiment this spike could not run**: make a Konduktor-shaped write
   to a *copy* of `master.db`, put it in place, launch Rekordbox, and observe
   whether it accepts the rows, rewrites them, or complains. This needs Ben's
   go-ahead because it means pointing the real app at a modified library. It also
   resolves the `Kind` encoding above in the same session.
3. **Re-scope step 4 before building.** The adapter is metadata + playlists
   (straightforward, `pyrekordbox` does the work) *plus* a hand-written cue store
   (two tables kept consistent) *plus* an ANLZ beatgrid store (per-beat ⇄ marker
   projection). Consider landing them as three separately-green milestones rather
   than one adapter.

### 8.9 Rekordbox mutates the library just by starting up

**VERIFIED.** Launching Rekordbox 7 and doing nothing advanced the global USN by
five (1489 → 1494) and changed, with no user edit at all:

- `agentRegistry.agentCredentials` — the auth token, `str_1`/`str_2`/`text_1` and
  its date, **rotated on launch**;
- `agentRegistry.notificationNextFetch` / `notificationFetchedUID` /
  `LangPath` / `SyncSettingsRootPath` / `unrecoverableAuth` — timers and timestamps;
- three `djmdSampler` rows restamped (`rb_local_usn` 104→1490, 105→1492, 106→1494).

Two consequences:

1. **The "no-op changes nothing" invariant is only assertable across Konduktor's
   own open/save cycle against a static file** — which is how §8.1 verified it.
   `test_rekordbox_fidelity.py` must work on a temp copy and must not try to
   assert quiescence across a Rekordbox app session. Worth stating in the test's
   docstring, because the natural reading of "a no-op save changes nothing" will
   otherwise look false the moment anyone launches the app.
2. **A whole-file restore of `master.db` rolls back more than library data.** It
   would revert the auth token, notification state and sampler rows to whatever
   they were at snapshot time. Traktor's `collection.nml` has no equivalent —
   restoring it is harmless. This is an independent argument for
   `capabilities.save.history = false` on Rekordbox at first, and for any later
   history support being a *selective* restore rather than a file swap.

### 8.10 Acceptance test: Rekordbox accepts Konduktor's writes — VERIFIED

The §8.8 experiment has now been run, on the real library (full backup taken
first). A `master.db` carrying exactly one Konduktor-shaped edit — `djmdContent`
Title + `rb_local_usn` 843→1490, and `agentRegistry.localUpdateCount` 1489→1490,
committed through `pyrekordbox` — was installed and Rekordbox 7 was launched
against it.

**Result: accepted, cleanly.**

- Rekordbox **displayed the edited title** (`Demo Track 1 [KONDUKTOR WROTE
  THIS]`). No warning, no error, no database-repair prompt, no re-analysis.
- It **left the edited row untouched**: `rb_local_usn` was still exactly 1490
  afterwards. Rekordbox did not restamp, revert or re-derive it.
- Crucially, it **continued the sequence from Konduktor's value instead of
  resetting it**: startup housekeeping took `localUpdateCount` 1490 → 1495,
  handing 1491/1493/1495 to the `djmdSampler` rows it always touches (§8.9).
- The only other differences were that same launch housekeeping — auth-token
  rotation, notification timers, sampler restamps. Nothing related to the edit.

So the USN protocol `pyrekordbox` implements is the one Rekordbox actually
expects, and a third-party write that maintains it is indistinguishable to
Rekordbox from its own. **This removes the last blocker on the local write path.**

Scope of the claim: one library, one field, one table, local-only (not cloud
synced), Rekordbox 7 on macOS. It does not license writing *any* row — the cue
and beatgrid paths (§8.2, §8.4) are still unexercised and carry their own risks.
But the premise the whole step was gated on — "writing rows without maintaining
the sync columns may corrupt Rekordbox's sync state" — is answered: maintain them
via `commit()` and Rekordbox is content.

**Still unresolved: the `djmdCue.Kind` encoding** (§8.7). The cue half of the
experiment was not performed; `Demo Track 2` still has zero cues. It needs
someone to set a hot cue in slot A, a hot cue in slot C (skipping B, to separate
slot-letter from sequence), a memory cue, and a distinctive colour on one of
them, in Rekordbox — then diff `djmdCue`.

### 8.11 The cue encoding, resolved

Settled by setting known cues in Rekordbox on a previously cue-free track
(`Demo Track 2`, 120.00 BPM) and reading back `djmdCue`. **VERIFIED** unless noted.

**`Kind` is the cue's bank slot, and `0` means "memory cue".**

| Observed | `Kind` | `OutMsec` | `ColorTableIndex` |
|---|---|---|---|
| hot cue **A** @ 25 ms | `1` | `-1` | 22 |
| hot cue **C** @ 62.5 s | `3` | `-1` | 62 |
| memory cue @ 78.0 s | `0` | `-1` | — |
| memory cue @ 25 ms | `0` | `-1` | — |
| hot cue **F**, 4-beat loop @ 120.0 s | `7` | `122026` | 0 |

So `Kind = 0` → memory cue (unslotted, unlimited), `Kind = N ≥ 1` → hot cue in
slot N. A→1 and C→3 confirm a 1-based letter index directly.

**CAVEAT — the slot→letter mapping is NOT a plain letter index.** The 4-beat loop
sits on pad **F**, the 6th of the eight pads (confirmed from a screenshot of the
deck: A/C/F lit, F sixth), yet it stored `Kind = 7`. So the observed mapping is
A→1, C→3, **F→7**, which no linear rule fits. The other track in the library
(`Motorola`) has `Kind` 2, 3 and 5, but which pads those were set on was never
observed, so it cannot arbitrate.

Both loops in the library sit at a `Kind` one above the next-lowest used slot,
which hints the anomaly may be loop-related — but that is one sample and a guess.

**Do not infer the mapping; measure it.** Set a cue on *all eight* pads A–H of a
fresh track and read the eight `Kind` values in one go. Two minutes, and it
yields the complete table with no inference. Until then the adapter should treat
`Kind` as an **opaque slot id** — store it, round-trip it, address cues by it —
and only the presentation layer needs the letter, which is precisely what
`cues.slot_labels = 'letter'` is for. Nothing else in this section depends on
the answer.

**A loop is an ordinary cue row with an out-point.** There is no separate loop
bank: `OutMsec` becomes the loop end and `BeatLoopSize` carries the musical
length. So `capabilities.cues.loops = 'cue_type'` (as Traktor), **not**
`'separate_bank'`. A loop can occupy a hot cue slot (`Kind=7` above) or be a
memory cue.

**`BeatLoopSize = (beats << 16) | 1`** — VERIFIED against two loops at different
tempos, each agreeing with the measured millisecond span:

| Track | BPM | `OutMsec - InMsec` | `BeatLoopSize` | hex | beats |
|---|---|---|---|---|---|
| Motorola | 125.00 | 480 ms | 65537 | `0x10001` | 1 |
| Demo Track 2 | 120.00 | 2000 ms | 262145 | `0x40001` | 4 |

The adapter must write both `OutMsec` and a consistent `BeatLoopSize`; deriving
one without the other will produce a loop Rekordbox renders inconsistently.

**Cue colour is a PALETTE index, and not `djmdColor`.** This closes the §8.7
question. The user-set colours came back as `ColorTableIndex` 22 and 62, with
`Color = -1`; Rekordbox's own auto-placed cues use `Color = 255,
ColorTableIndex = 0`. Index 62 is far beyond `djmdColor`'s 8 rows, so the cue
palette is a larger built-in table — `djmdColor` is the *track* colour palette
and is unrelated. Set `capabilities.cues.color = 'palette'` and populate
`palette` from the built-in table once its values are known (reading them out of
the Rekordbox app resources, or by sampling each swatch as above). `Color = -1`
appears to mean "no explicit colour".

**Field-level notes for the writer.** `InFrame` is set alongside `InMsec`
(frames at 150 fps: 62526 ms → 9378) and `OutFrame` alongside `OutMsec`; both are
populated by Rekordbox and should be written consistently. `ID` is a random
32-bit integer as a string, `UUID` a fresh uuid4, and `ContentUUID` copies the
track's `contentCue.UUID`. `ActiveLoop`, `CueMicrosec` and `Comment` are `0`/`""`
on user cues; `Comment` carries Rekordbox's own label on auto cues (`"1.1Bars"`).

**The `contentCue` mirror is maintained in lockstep** (§8.2): after the edits it
read `rb_cue_count = 5` with a 1964-byte JSON array holding all five records,
including their `created_at`/`updated_at`. Its `rb_local_usn` was stamped (1527)
while **every `djmdCue` row's stayed NULL** — confirming §8.2's finding that
`contentCue` is the sync unit and the individual cue rows are not.

---

## 9. Milestone 1 landed: the read-only Rekordbox adapter

`backend/konduktor/adapters/rekordbox/` now opens, projects and serves a real
Rekordbox library through the generic layer. `./run_tests.sh` is green at **217
assertions** (was 146). Nothing about the Traktor path changed.

**What works**: `can_open` probe (decrypts one page, looks for `djmdContent`;
rejects a plain SQLite `.db`), default-location discovery via `pyrekordbox`'s own
config, the full read projection (tracks, cues, beatgrid, playlist tree, query /
facets / stats), `audio_path` with path remapping, and capabilities. Verified end
to end through the HTTP API: `/api/capabilities`, `/api/tracks`,
`/api/tracks/cues`, `/api/playlists`, `/api/stats` all serve Rekordbox data, and
`PATCH /api/tracks` returns a clean 422.

**What it deliberately does not do**: every one of the 21 mutating commands
raises `Unsupported`. The capability system is the mechanism (nothing is
advertised as editable); the raise is the backstop.

### Decisions made while building it, which the write path inherits

- **`Kind` stays an opaque slot id** all the way through. §8.11's anomaly (pad 6
  → `Kind` 7) therefore blocks nothing; only a UI label needs the mapping.
- **`grid_marker_count` is an approximation, corrected on read.** Reading every
  ANLZ at open costs ~12 s on a full-size library, so the projection reports 1
  for any track with a BPM and `track_cues()` writes the true count back. BPM —
  not the presence of an analysis file — is the signal: Rekordbox analyses
  one-shot samples too and gives them an ANLZ with no grid. On the reference
  library the two sets agree exactly (22 tracks, both).
- **Internal Rekordbox playlists are hidden** (`SPECIAL_PLAYLIST_IDS`: "CUE
  Analysis Playlist", "Cloud Library Sync"). They are not the user's.
- **Cue colour is projected as `None`, not guessed.** `ColorTableIndex` is an
  index into a built-in palette whose values are not yet known, and inventing an
  RGB value would be worse than admitting the gap. `capabilities.cues.color` is
  already `"palette"` so the UI knows not to offer a free picker.
- **`snapshot()` and `restore()` raise.** A Rekordbox library is
  `master.db` + N ANLZ files + `masterPlaylists6.xml`, and restoring the database
  alone would also roll back Rekordbox's auth token and sampler state (§8.9).
  `capabilities.save.history` is `False` to match.

### Two fixes that were not strictly in scope

- **`main.py` no longer imports Traktor's `describe`.** The picker's "last
  opened" shortcut ran every path through `adapters.traktor.discovery.describe`
  regardless of platform — a layering leak `test_layering.py` could not see,
  because it only guards `core/`. There is now a generic `registry.describe()`
  that delegates to whichever driver recognises the path and falls back to a
  plain `exists: false` record for a library that has since moved.
- **`test_layering.py` now guards every adapter**, not just `core/`: no adapter
  may import another platform's library. It checks real imports via AST rather
  than text, because the first (text-matching) version failed on docstrings that
  merely *mention* the other platform.

### Next, in order

1. **Milestone 2 — metadata + playlist writes.** This is the part `pyrekordbox`
   actually implements and §8.1/§8.10 verified: commit through it, USNs are
   maintained, Rekordbox accepts the result. Needs `test_rekordbox_fidelity.py`
   (the row-level dump/diff harness — a working prototype exists in the session
   scratch dir and is described in §8.1), plus the cloud-sync refusal wired to a
   real capability rather than only to `Unsupported`.
2. **The capability system needs a "readable but not writable" state.** Right now
   read-only is expressed by reporting every individual capability as false,
   which is indistinguishable from "this platform has no such feature" and will
   read as a bug to a user. Add an explicit flag before milestone 2, not after.
3. **Milestone 3 — the cue store and the ANLZ grid store.** Both hand-written;
   see §8.2 and §8.4. `beatgrid.beats_from_markers()` is already written and
   unit-tested as the inverse of the read projection, so the grid write has a
   starting point.

---

## 10. The read-only capability landed

`Capabilities` now carries **`writable: bool`** and **`readonly_cause`**
(`platform_incomplete` | `cloud_synced`), backend and frontend in the same
change. `./run_tests.sh` is green at **225 assertions**; `npm run build` and
`npm run test` (25) pass.

This was §9's prerequisite, and building it surfaced that the gap was bigger than
"the UI lacks a label":

**Almost nothing gated on editability.** `capabilities.tracks.editable_fields`
and `grid.editable` were modelled but consulted by **no component at all** — only
`hotcue_slots`, `cues.types` and `rating_max` were ever read. So on a Rekordbox
library the UI would have cheerfully offered Edit Tags, inline editing,
click-to-rate, playlist create/rename/delete and every prep-deck control, each
one 422-ing at the adapter. The adapter's `Unsupported` was doing all the work,
as a backstop for a UI that never gated.

Now gated on `writable`: `SaveBar` (replaces the save button with the reason),
`App`'s `onEditField` (inline edit + rating go inert), the "Edit Tags…" context
item, `Sidebar`'s new-playlist button, and all 12 `PrepStrip` edit handlers —
which report the reason instead of firing the request. The deck stays fully
usable for listening, with a "Read-only" badge, because that is most of its value
on a library you cannot write.

**Two latent bugs fixed in passing**, both only reachable once a second platform
existed:
- The sidebar's **delete** button was gated on `can_rename`, not `can_delete`, so
  a platform allowing one but not the other would offer both.
- The playlist **track-count badge** was inside that same block, so a read-only
  library would have shown no counts at all — information hidden as though it
  were an action.

**Verified end to end**, not just in tests: opening a Rekordbox library reports
`writable: false, readonly_cause: platform_incomplete` over HTTP, and switching
to the Traktor collection at runtime flips it to `writable: true, cause: null`
with 11 editable fields.

**The cloud-synced branch is tested for real**, not asserted: the test forges a
server-issued `usn` on a throwaway copy, confirms detection flips to
`cloud_synced`, and confirms the refusal names Rekordbox Cloud rather than the
milestone. That is the one failure mode version history cannot undo, so it is
worth the setup.

Also corrected: CLAUDE.md documented the runtime open route as
`POST /api/collection/open`; it is `POST /api/library/open`.

### Note for whoever does milestone 2

When Rekordbox writes land, `writable` flips to true for a *local* library while
staying false for a cloud-synced one — the two causes already distinguish that,
and `RekordboxAdapter._readonly_reason()` already words both. The per-feature
flags (`editable_fields`, `grid.editable`, …) then become the finer gate, and
they will need components to actually read them: **this change did not retrofit
per-feature gating**, it added the library-level gate that was missing. A
platform that is writable but cannot, say, edit a beatgrid would still offer the
control today.

---

## 11. Milestone 2: metadata + playlist writes

**Scope decision (Ben, 2026-09-18): shipping Rekordbox writes with NO undo is
accepted.** `capabilities.save.history` stays `False` — a Rekordbox library is
`master.db` + ~200 ANLZ files + `masterPlaylists6.xml`, and restoring the
database alone would roll back Rekordbox's auth token and sampler state (§8.9).
Rekordbox's own `master.backup.db` is the only safety net. Revisit only if Ben
asks; do not silently add a half-backup.

What turns on here: **track metadata and playlists**. Cues and the beatgrid stay
read-only until milestone 3 (§8.2, §8.4 — both are hand-written write paths).

### Milestone 2 landed

`./run_tests.sh` green at **258 assertions**; `npm run build` + 25 vitest pass.
Rekordbox now accepts **track metadata and playlist** edits. Cues and the
beatgrid still refuse, and now say so through `cues.editable` / `grid.editable`
rather than through the library-level flag.

**Per-feature gating came first, deliberately.** §10 warned that flipping
`writable` would re-expose every prep control, because `caps.writable` was the
only gate in the app. So `cues.editable` was added to `CueCapabilities`
(symmetric with `grid.editable`), the 12 PrepStrip handlers were split onto
`refuseCueEdit` / `refuseGridEdit`, and the tables now gate **per field** via a
`editableFields` set in the table meta — `InlineEdit` takes a `readOnly` prop and
`RatingStars` simply loses its `onChange`. Without that, milestone 2 would have
shipped a regression.

**Three real bugs found by building it:**

1. **Foreign-key edits projected as no-ops.** `artist`/`album`/`genre`/`label`/
   `remixer` are FKs into lookup tables. Setting the id does not move
   SQLAlchemy's cached relationship, so re-projecting immediately read the OLD
   name back — the API returned `genre: null` for an edit that had in fact
   worked. Fixed with a flush + expire; pinned by a test that sets both a brand
   new lookup value and an existing one.
2. **`close()` did not release the file.** `pyrekordbox`'s `close()` only closes
   the session; the pooled connection keeps the OS handle, so `master.db` could
   not be replaced. Now disposes the engine. This surfaced as a `disk I/O error`
   in the fidelity test and would have surfaced in production as a failed
   restore or a locked library.
3. **Nothing ever closed the previous library.** `AppState.open()` replaced the
   adapter without closing it, leaking a handle per library switch. It now closes
   the old one *after* the new one parses, so a failed open leaves the current
   library intact.

**`SaveOutcome` was promoted to `core/adapter.py`** under the two-platform rule,
with `snapshot` now optional — Rekordbox has no single blob that IS the library.
`AppState.save()` gates version history on `capabilities.save.history` instead of
assuming every platform is versioned.

Also corrected in CLAUDE.md: the cue routes are `/api/tracks/cue`, not
`/api/tracks/hotcue`.

### What milestone 3 inherits

- `cues.editable` / `grid.editable` are the flags to flip, and the UI already
  gates on them — no frontend work should be needed beyond flipping them.
- `beatgrid.beats_from_markers()` is written and unit-tested as the inverse of
  the read projection.
- The cue write must maintain **both** `djmdCue` rows and the `contentCue` JSON
  mirror + `rb_cue_count` (§8.2), and should decide deliberately whether to stamp
  `rb_local_usn` on `djmdCue` rows — Rekordbox itself leaves them NULL.
- `test_rekordbox_fidelity.py` is the harness to extend: add a cue edit and a
  grid edit as phases F and G, asserting the same "exactly these rows" property.

### Milestone 3a landed: hot cue writes

`./run_tests.sh` green at **288 assertions**. `cues.editable` is now true;
`grid.editable` is still false.

Every native field convention was read off the real library before writing any
of it, and all of them are asserted in `test_rekordbox_fidelity.py` phase F:

| Field | Value Konduktor writes | Evidence |
|---|---|---|
| `InFrame` / `OutFrame` | `floor(msec × 150 / 1000)` | matches every row in the library |
| `ContentUUID` | the **track's** UUID | verified == `djmdContent.UUID` |
| `contentCue.ID` | the **track's** UUID too | same |
| uncoloured cue | `Color=-1, ColorTableIndex=NULL` | how Rekordbox wrote Ben's uncoloured hot cues |
| loop | `Color=255, ColorTableIndex=0, ActiveLoop=0, CueMicrosec=0` | how Rekordbox wrote both loops |
| `BeatLoopSize` | `(beats << 16) \| 1`, beats from the track's own tempo | §8.11, verified at two tempos |

Fidelity phases F and G assert that a cue write touches **only** `djmdCue`,
`contentCue` and `agentRegistry` — never `djmdContent` — that the mirror holds
one record per live cue row with `rb_cue_count` agreeing, and that a delete
clears both.

**Memory cues are refused**, not because they are hard but because of the
two-platform promotion rule: Rekordbox is the only platform that has them, so
they are projected and displayed and never written. `role="memory"` raises
`Unsupported` with that explanation.

### Milestone 3b: the beatgrid — read this before starting

`pyrekordbox`'s ANLZ tags **cannot change the number of beats**. `PQTZAnlzTag`'s
`set()`, `set_beats()`, `set_bpms()` and `set_times()` all raise unless the new
sequence is exactly as long as the existing one ("For now only values of existing
beats can be set"). Since a tempo change alters how many beats fit in a track,
every real grid edit changes that count.

So a grid write means manipulating `tag.content.entries` (a `construct`
ListContainer) directly, setting `content.entry_count`, and calling the tag's
`update_len()` — which exists for exactly this and recomputes
`len_tag = len_header + 8 × entries`. `check_parse()` asserts the count and the
list agree, so it is worth calling after.

Two further things that make this the riskiest piece so far:
- the grid lives in **two files** — `PQTZ` in `.DAT` and `PQT2` in `.EXT` — which
  presumably must stay consistent; and `djmdContent.BPM` (tempo ×100) is a third
  copy that Rekordbox shows in the library list.
- `.DAT`/`.EXT` round-trip byte-identically today (§8.3), which is the property
  that makes a grid write verifiable at all. **Re-check it after any write**: if
  a rebuilt file stops being byte-identical in the regions we did not touch, the
  write is corrupting something.

`beatgrid.beats_from_markers()` is already written and unit-tested as the exact
inverse of the read projection, so the marker→beat maths is not the hard part.

### Milestone 3a verification: point cues confirmed, LOOP WRITES DISABLED

The staged acceptance test was run in Rekordbox 7. Two results, one good and one
that changed the plan.

**Good: Rekordbox accepted the cue rows verbatim.** A re-read after the session
showed both rows byte-for-byte as Konduktor wrote them — same `Kind`, same
`BeatLoopSize`, same USN stamps. No repair, no renumbering. The write mechanism
and the `contentCue` mirror are sound, and a point cue written with `Kind=2`
landed on **pad B** exactly as predicted.

**Bad: a cue row carrying `OutMsec` does not land in the slot its `Kind` names.**
Written with `Kind=4` plus an out-point, it appeared as a **memory cue** with pad
D left empty (confirmed from a screenshot of the deck — only B lit). The loop's
*length* was right (4 beats), so `BeatLoopSize` is correct; only its placement is
wrong. Rekordbox's own 4-beat loop on pad F — the 6th pad — stores `Kind=7`, so
loops evidently use a slot encoding that has not been measured.

**So loop writing is now refused** (`WRITABLE_CUE_TYPES = ["cue"]`, and
`capabilities.cues.types` reports only `cue`). Loops are still READ and projected
with their length — losing them from the projection would be worse than being
unable to edit them. Writing one anyway would have silently created a memory cue,
which Konduktor deliberately cannot edit or delete, on a platform with no version
history.

**To finish loops, measure the encoding**: in Rekordbox, put a 4-beat loop on pad
**D** and another on pad **E** of any track, quit, and read the `Kind` values —
two points on known pads, plus Rekordbox-authored loop rows to diff field by
field against Konduktor's. Until then this is the one gap in cue support.

### Found while verifying: pyrekordbox refuses to commit while Rekordbox runs

`pyrekordbox.commit()` raises `RuntimeError` outright when it detects a Rekordbox
process. That contradicts the settled decision to **warn but proceed** (§6), and
the check is **process-wide rather than per-file** — so it refuses to write a
temp copy while the user has an entirely different library open, which made the
whole test suite fail purely because Rekordbox happened to be running.

`RekordboxStore._commit()` now suppresses that veto for the duration of the call
and logs a warning instead, so everything else `commit()` does — the USN
auto-increment and keeping `masterPlaylists6.xml`'s timestamps in step — still
runs as the library intends. `store.app_running` exposes the detection for
warning the user rather than blocking them. A test pins the behaviour by faking
a running process.

### Milestone 3b progress: the grid can be written, but only half of it

**The mechanical problem is solved.** `beatgrid.write_pqtz()` rebuilds a `.DAT`'s
`PQTZ` entry list from scratch — including changing the number of beats, which
`pyrekordbox`'s own setters refuse — by replacing `content.entries`, setting
`entry_count`, and calling the tag's `update_len()`. Verified on a real file:
resizing 374 → 367 beats produced exactly the expected byte-size delta, re-parsed
correctly, and left **every other tag byte-identical**.

**The blocker is `.EXT`.** The grid exists twice: `PQTZ` in `.DAT` and the
"extended" `PQT2` in `.EXT`. `PQT2` carries a 2-byte-per-beat array whose second
byte `pyrekordbox` literally names `unkown`, plus an undecoded `u3` field.
Inspection did not crack it: the value drifts ~2.75 per beat with a jump at each
bar boundary and must wrap within 8 bits, so it is neither a time nor a simple
index. Writing guesses into it would corrupt real user data on a platform with no
version history, so **`PQT2` is left untouched**.

That leaves one empirical question, and it decides the whole feature:

> **Does Rekordbox read the grid from `PQTZ` (`.DAT`) or from `PQT2` (`.EXT`)?**

A test is staged: Demo Track 1's `PQTZ` rewritten to **half-time (64 BPM, 184
beats)**, `.EXT` and `master.db` untouched, installed as a single-file copy.
Half-time is chosen because it is unmistakable — beat markers at half density and
a 64 BPM readout — rather than a subtle shift someone has to judge.

- **If Rekordbox shows the half-time grid**: `PQTZ` is authoritative, and grid
  editing is feasible. `PQT2` may still need attention for CDJ export, which is
  out of scope anyway.
- **If it shows the original 128 BPM grid**: `PQT2` wins, and grid editing stays
  out of reach until someone decodes it. That is a legitimate result — Rekordbox
  support would ship as library management + cues, with grid editing remaining a
  Traktor feature.

Note `djmdContent.BPM` is deliberately left at 128 in this test, so the library
list and the deck disagreeing is itself a signal about which copy is read where.

### Milestone 3b landed: the beatgrid writes

**`PQTZ` is authoritative — verified in Rekordbox 7.** The staged half-time test
showed the deck at 64 BPM with the grid correctly half-timed. Afterwards the
`.DAT` was byte-identical to what Konduktor wrote, `.EXT` and `.2EX` were
untouched, and Rekordbox did **not** reconcile `djmdContent.BPM`, which sat at
128 while the deck showed 64.

Three things follow, all now implemented:

1. **Only `.DAT`'s `PQTZ` is written.** The stale extended grid in `.EXT` does
   not trouble Rekordbox's own display; it would matter only for CDJ export,
   which is out of scope.
2. **`djmdContent.BPM` is Konduktor's to maintain**, mirroring the first marker —
   the same relationship Traktor's `<TEMPO>` has to its first grid marker.
   Without it the library list and the deck disagree.
3. **Grid edits are buffered until `save()`.** ANLZ edits are file writes, so
   applying them at command time would put an unsaved edit on disk and break the
   save contract every other platform obeys. `save()` writes the files first and
   commits the database second, so a failed file write leaves nothing committed.

The full marker vocabulary works — add / move (clamped between neighbours) /
retempo / delete / replace / delete-grid — including genuinely **flexible
multi-tempo grids**, end to end through the HTTP API.

Fidelity phase H is the safety net: an unsaved grid edit does not touch the file;
`.EXT`/`.2EX` stay byte-identical; **every tag in the rewritten `.DAT` except
`PQTZ` is byte-identical**; and in the database only `BPM` + the row USN + the
counter move. Suite: **316 assertions**.

### Remaining Rekordbox gaps

- **Saved loops** — read but not written (§ above). Needs the slot encoding
  measured: a 4-beat loop on pads D and E, then read the `Kind` values.
- ~~A Konduktor-written FLEXIBLE grid has not been seen in Rekordbox.~~
  **VERIFIED 2026-09-19**: Demo Track 1 written as 128 BPM dropping to 90 at
  1:00 displayed exactly that in Rekordbox 7 — the deck's BPM readout followed
  the tempo change at the marker. Multi-marker grids are confirmed end to end,
  which is the last thing milestone 3 was waiting on.
- **Cover art** — never investigated; `tracks.artwork` is false.
- **`PQT2` is undecoded**, so the extended grid drifts out of step with `PQTZ`
  after any Konduktor grid edit. Harmless for Rekordbox itself; would need
  solving before any CDJ/USB export (currently rejected as out of scope).

### The hot cue slot encoding, measured — and it was never about loops

`djmdCue.Kind` is 1-based **and skips 4**. Established by writing a 4-beat loop
at every `Kind` from 1 to 8, ten seconds apart, and reading the pads off a real
deck:

| `Kind` | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 |
|---|---|---|---|---|---|---|---|---|---|
| pad | A | B | C | **—** | D | E | F | G | H |

A cue at `Kind=4` occupies no pad at all; Rekordbox displays it as a memory cue.

That single gap explains everything that had been confusing:

- Ben's Rekordbox-authored loop on pad **F** stored `Kind=7` — correct, not an
  anomaly;
- a Konduktor cue written at `Kind=4` "turned into a memory cue" — it *was* one;
- pad H stayed empty in the probe because H is `Kind=9`, and the probe stopped
  at 8.

**Loops were never the problem.** They had been disabled on the theory that an
out-point changed the slot encoding; in fact the adapter's `slot -> Kind` map was
simply wrong from pad D upward, for *every* cue type. Loops write fine now, and
`cue_types.WRITABLE_CUE_TYPES` is back to `["cue", "loop"]`. **Confirmed in
Rekordbox**: looped hot cues set in Konduktor save and display correctly.

The lesson is the project's own standing one, and it cost two rounds of wrong
theory: the mapping was *inferred* from three incidental data points before
anyone thought to *measure* it across the whole range. One probe settled it.

Tests now pin the exact `Kind` sequence and assert that all eight pads are
individually addressable, so a regression fails loudly instead of quietly moving
someone's cues.

### Remaining Rekordbox gap

Only **cover art**, which has never been investigated (`tracks.artwork` is
false; `djmdContent.ImagePath` plus separate artwork files). `PQT2` also stays
undecoded, which only matters for CDJ export — out of scope.
