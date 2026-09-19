# Handoff: the export feature

**For**: an agent picking up Konduktor with no prior context.
**Task**: build library export — converting a curated slice of the loaded library
into a **fresh, self-contained library for another platform**, with the audio
files copied alongside.
**Status of the plan**: the multi-platform work is done through the Rekordbox
adapter. Export was originally sequenced *after* a Serato adapter; Ben has
chosen to bring it forward, so **the only export targets that can exist today are
Traktor and Rekordbox**.

Every *design* decision was settled in
[`.claude/discussions/discuss-multi-platform-library-2026-09-17.md`](../discussions/discuss-multi-platform-library-2026-09-17.md)
— read its **Export** sections and **"If Moving to Planning/Implementation"**
before designing anything. This handoff is the current-state companion: what
exists, what to build, and what will bite you. Where they disagree, the
discussion doc wins on intent and this one on facts about the code.

The Rekordbox handoff ([`rekordbox-adapter.md`](rekordbox-adapter.md)) is worth
skimming too — its §8 Findings are the format knowledge any Rekordbox *writer*
will need, and its history is a good model of how this project verifies things.

---

## 1. Where the project is

Konduktor is a library-management and track-prep tool for DJ software, built on a
**generic model + per-platform adapter** architecture. Two adapters exist:

- **Traktor** (`collection.nml`) — full read/write, byte-exact saves, version
  history.
- **Rekordbox** (`master.db` + ANLZ analysis files) — read/write for metadata,
  playlists, hot cues and beatgrids. No version history (a deliberate, accepted
  scope decision). Gaps: cover art, `remap_locations`.

```
backend/konduktor/
  core/          generic. MUST NOT import a native library type (test_layering.py)
    model.py         Track, CuePoint, GridMarker, TrackCues, PlaylistNode…
    adapter.py       LibraryAdapter / LibraryDriver protocols, SaveOutcome, errors
    capabilities.py  what a loaded library can persist
    query.py         TrackIndex: query/filter/sort/facets/stats
    registry.py      adapter selection by can_open() probe
  adapters/traktor/    store.py (native model) adapter.py projection.py driver.py …
  adapters/rekordbox/  same shape; store.py owns the SQLCipher session + ANLZ
  app_state.py   the one loaded library + version-history commits
  prefs.py       userprefs.json in the per-OS app-data dir (paths.app_data_dir())
  main.py        thin FastAPI routes, all under /api
```

**Tests**: `cd backend && ./run_tests.sh` → 331 assertions across
`test_layering.py`, `test_save_fidelity.py`, `test_traktor_adapter.py`,
`test_rekordbox_adapter.py`, `test_rekordbox_fidelity.py`, `test_phase3.py`,
`test_history.py`. Frontend: `npm run build && npm run test`.
**The suite must be green at the end of every step** — that discipline is why a
1,500-line refactor of the save path landed without a fidelity regression.

---

## 2. The one architectural gap, and it is not small

**No adapter can create a library from nothing.**

`LibraryDriver` is `can_open` / `open` / `detect` / `describe` / `restore`.
`LibraryAdapter` is built entirely around *one already-open library* that commands
are replayed onto. Export needs the opposite: **write a brand-new library at a
path where nothing exists yet.**

This is the central design question of the whole feature, and the discussion doc
does not settle it. Decide it deliberately and write down why. The obvious
shapes:

1. **A separate `LibraryWriter` protocol** — `create(path)` returning something
   that accepts tracks, playlists and prep data, implemented per target. Keeps
   the edit path untouched, which matters because the edit path is the one with
   the fidelity guarantees. Some duplication with the adapters' write code.
2. **Extend `LibraryDriver` with `create(path) -> LibraryAdapter`** — an empty
   library is then just a library, and the existing commands populate it. Elegant
   on paper; the risk is that "empty new file" and "the user's real library"
   start sharing code paths, and the *reason* the current architecture is safe is
   that the write target is always a retained native model that was parsed from a
   real file.
3. **Target-specific exporters with no shared protocol** — fastest to a working
   Traktor export, and it will rot.

The discussion doc's strongest constraint bears on this: *"Two distinct write
paths must coexist: a high-fidelity in-place edit path per platform, and a
lossy-by-design export path. They have opposite requirements and should not
share an implementation."* Read that as pointing at (1), but make the call
yourself with the code in front of you.

---

## 3. What the design already settles

Do not relitigate these. They are decided, with rationale and rejected
alternatives recorded in the discussion doc.

**Shape of an export**
- Always a **fresh, self-contained library** at a user-chosen destination.
  **Never merges** into an existing target library. This is what removes any need
  for cross-platform track identity — there is nothing to match against.
- **Always copies the audio files**, for every target, not just Serato.
- Audio layout is a **global, persisted setting**: **flat single folder
  (default)** or **mirror the source directory structure**.
- Flat-layout filename collisions get a **counter suffix** (`Xtal-2.mp3`).
  Known consequence: numbering depends on processing order, so repeat exports may
  assign different suffixes to the same tracks.
- Playlist folders: **preserve where the target supports them**, otherwise
  flatten to path-encoding names (`House / Peak Time`).
- Re-export to an occupied destination: **warn, overwrite on confirm.**
- Provide an explicit **"export entire library"** action that skips export-set
  creation entirely.

**Export Sets** (the unit of curation)
- A named, persisted list of **track and playlist references** scoping an export.
  Called an **Export Set**, never a "Crate" — Serato uses "crate" for a playlist
  and Konduktor will import those.
- **References are LIVE**: an export set stores pointers, and export resolves
  them at export time, so later additions to a playlist are picked up.
  **Consequence: the export preview must be computed at export time, never
  stored.**
- Playlist resolution **auto-includes** referenced tracks, **deduplicates**, and
  copies each file **exactly once**.
- Stored in **Konduktor's own store**, not in the platform library and not in
  `userprefs.json`, keyed by a **stable library ID**.

**Running an export**
- **Pre-scan for missing audio files**, report them, let the user cancel or
  proceed without them — fail fast rather than halfway through a multi-hour copy.
- **Pre-check disk space** before starting.
- **Progress with a working cancel.** Known consequence: cancelling leaves a
  partial export, which must be cleaned up or clearly marked incomplete.

**Lossiness**
- **Degrade to the nearest equivalent where one exists, drop only where none
  does.** A Traktor fade-in cue becomes a plain cue on a target without them;
  Rekordbox memory cues fill spare hot cue slots until they run out.
- **No loss report for now** — explicitly deferred. Adapters know what they
  collapsed, so it stays cheap to add.

---

## 4. Landmines, earned

**There is no stable library ID.** Export sets are specified as keyed by one,
held in a sidecar next to the user's collection — and nothing like it exists.
Today everything keys off the library's **OS path**: `prefs.get_path_mapping`,
`prefs.get_last_collection`, and `history`'s repo directory (a hash of the
resolved path). Moving a collection silently orphans all of it. Building export
sets on the path would inherit that; building the sidecar is the decided design
but is genuinely new work. Do it first — retrofitting an ID once sets exist means
migrating them.

**The lossiness rule needs capabilities the target does not have loaded.**
`Capabilities` describes *the currently open library*, via an adapter instance
built from a real file. Export needs to know what a target platform can hold
*before* any such library exists. Either capabilities must become derivable from
a platform alone (not an instance), or exporters must carry their own static
declaration. This is exactly where a "what can the target represent?" question
will first bite, and `capabilities_for()` in each adapter currently takes a
`path`.

**The two-platform promotion rule still applies.** A concept enters the generic
vocabulary only once **two or more** platforms have it. Export will tempt you to
add target-specific knobs to generic types; don't. Single-platform concepts stay
*preserved but not editable* — for instance Rekordbox **memory cues** are
projected and displayed and never written.

**Rekordbox as an export TARGET is much harder than Traktor.** A Traktor export
is one XML file Konduktor already renders byte-exactly. A Rekordbox export needs
a `master.db` — SQLCipher, foreign keys into lookup tables, USN bookkeeping — plus
per-track ANLZ analysis files containing the beatgrid, plus `masterPlaylists6.xml`.
Worse, `.DAT`/`.EXT` analysis files can currently only be *modified*, never
created: `beatgrid.write_pqtz()` rebuilds the `PQTZ` tag **inside an existing
file**. Generating ANLZ from scratch is unexplored, and `PQT2` (the extended
grid) is undecoded — `pyrekordbox` names its per-beat field `unkown`.
**Strongly consider shipping Traktor as the only export target first.**

**CDJ/USB export (`export.pdb` + ANLZ) is rejected as impractical.** It has been
re-proposed twice. Do not raise it again.

---

## 5. Working agreements

- **`./run_tests.sh` green at the end of every step.**
- **`schemas.py` and `api.ts` land in the same commit** — one contract in two
  languages.
- **Verify against reality, do not infer.** The project's standing lesson, and it
  has been proved right four times: Traktor cue colours, flexible grid marker
  naming, whether Rekordbox reads `PQTZ` or `PQT2`, and the hot cue slot
  encoding. That last one cost two rounds of wrong theory built on three
  incidental data points; one deliberate probe across the full range settled it.
  For export the equivalent is: **write an export, then open it in the real DJ
  software** before believing it works.
- **Ben's Rekordbox library is a disposable test library** — write to it directly,
  no backups needed. **His Traktor collection is real, irreplaceable data**
  (8,485 entries): always work against copies. `./dev-sandbox.sh` already
  encodes exactly this split and refuses to start if another server holds the
  ports.
- **Capability-gate every control.** The UI must never offer an edit — or an
  export target — that cannot actually be produced. This has failed twice and
  both times it was a stale flag, not missing machinery.

## 6. Suggested first steps

1. **Decide the writer architecture** (§2) and record the decision and its
   rejected alternatives, in the style of the discussion doc.
2. **Build the stable library ID sidecar** (§4) — everything else keys off it.
3. **Export Sets: model, store, API, UI.** Live references; preview computed at
   export time.
4. **A Traktor exporter end to end**, with the audio copy, the pre-scan, the
   space check and cancellable progress.
5. **Open the result in Traktor.** Not "the tests pass" — actually open it.
6. Only then consider a Rekordbox exporter, knowing §4's warning.

Bulk metadata editing also remains unbuilt and unblocked; it is far smaller than
this and was deferred specifically until the adapter architecture existed. If a
shippable win is wanted sooner, that is the alternative — confirm with Ben rather
than assuming.
