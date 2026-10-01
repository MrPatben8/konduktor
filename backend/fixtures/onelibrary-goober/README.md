# Goober: a rekordbox-written stick, before and after rekordbox edited it

The stick Ben keeps for measuring the Pioneer formats, as rekordbox 7 left it:
11 tracks, 2 playlists, **both** libraries (`exportLibrary.db` and the legacy
`export.pdb` + `exportExt.pdb`). `test_onelibrary_anlz.py`,
`test_onelibrary_pdb.py` and `test_onelibrary_tracks.py` run against copies of it.

## What is here

- `PIONEER/rekordbox/` holds the three databases, **untouched**: bytes rekordbox
  wrote (snapshot `s0`, 2026-10-01).
- `PIONEER/USBANLZ/` holds every track's `.DAT` and `.EXT`, trimmed by dropping
  whole tags: only `PPTH`, `PQTZ`, `PCOB`, `PCO2` and `PQT2` are kept, byte for
  byte. The waveforms are gone, and so are the `.2EX` files.
- `after/` holds the same tags after **rekordbox 7 itself** edited the stick
  (snapshot `s1`, one edit per track):
  - `black-bloc-recoloured.EXT`: pad C recoloured to `0x2D` `#FF0045`.
  - `wtf-87bpm.DAT` / `.EXT`: "Result – WTF" changed from 174 to 87 BPM.
  - `demo2-pad-e-added.EXT`: a hot cue added on pad E of "Demo Track 2".

There is no audio. The adapter resolves paths and never needs the files, so the
time base falls back to rekordbox's MP3 default, the same offset for reading and
writing.

## Why

Konduktor's edits are pinned against what rekordbox wrote for the SAME edit, not
against Konduktor's own reader. A recolour and a grid edit must reproduce
rekordbox's bytes exactly (colour bytes, the blanked `PQT2`, loop beats cleared).
The pdb tests prove that an in-place edit of a rekordbox-written `export.pdb`
leaves everything else as rekordbox wrote it. The full diff of the measurement is
in `.claude/discussions/discuss-onelibrary-editing-2026-10-01.md`.
