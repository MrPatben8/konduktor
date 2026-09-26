# OneLibrary fixture drive

A real OneLibrary USB export, trimmed to the size of a test fixture. This is the
drive `test_onelibrary_adapter.py` runs against, so the suite needs no USB stick
plugged in and gives the same answer on every machine.

Produced by rekordbox 7.2.18 exporting a two-track playlist to an exFAT stick.
**Nothing in the database was written or edited by Konduktor** — it is the bytes
rekordbox itself wrote, which is the whole point of a fixture whose job is to
prove we read the real format correctly.

## What was changed

Only the analysis files, and only by deleting whole tags:

| File | Original | Here |
|---|---|---|
| `ANLZ0000.DAT` | 4,972 B | 2,812 B |
| `ANLZ0000.EXT` | 85,602 B | 732 B |

`PPTH`, `PQTZ`, `PCOB` and `PCO2` — the path, the beatgrid and the two cue lists,
i.e. everything the adapter reads — are kept **byte for byte**. The waveform tags
(`PWAV`, `PWV2`–`PWV5`) are dropped, which is where ~99% of the `.EXT` went, and
the `.2EX` files are omitted entirely.

The trim is byte-level tag surgery, rewriting only the `PMAI` header's file
length: `pyrekordbox` cannot rebuild a file containing cue tags, so re-serialising
was not an option.

The two `.mp3` files are **placeholders**, not audio. The adapter resolves paths
and never opens them; carrying 12 MB of demo tracks in the repo to prove that
would be silly.

## What is in it

- 2 tracks, one of which (`Demo Track 1`) carries a **flexible multi-tempo grid**
  (128.00 BPM to 60 s, then 90.00 BPM) — the awkward case, deliberately.
- 4 hot cues plus a looped memory cue on that track, including one on pad **D**,
  which is the cue that lives only in the `.EXT` and that a reader looking at the
  `.DAT` alone would lose.
- 1 playlist, `demos`, holding both tracks in order.

`Demo Track 1`'s title still reads `[KONDUKTOR WROTE THIS]` because the grid and
title were written into `master.db` by Konduktor before the export. That is
incidental to the fixture, but it does record that the whole chain — Konduktor →
`master.db` → rekordbox's OneLibrary export → this file — preserved them.

## schema.sql

The complete `CREATE TABLE` set for a OneLibrary database, all 22 tables,
extracted from Traktor Pro 4.5's binary, which embeds the DDL it uses to create
one. AlphaTheta publishes no specification, so this is the closest thing to one:
a second vendor's independent implementation of the same schema. It was verified
by building an empty database from it and having `pyrekordbox`'s ORM — a third
implementation — open and write to it.

Not loaded by the tests; kept as documentation, and as the starting point if
Konduktor ever needs to create a drive from nothing.
