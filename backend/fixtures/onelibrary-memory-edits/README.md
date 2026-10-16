# Memory-cue edits rekordbox 7 made on a stick

Analysis files of six tracks on a OneLibrary test stick written by
rekordbox, before and after **rekordbox 7 itself** edited their MEMORY cues
(kit M2, 2026-10-16; the stick was backed up first and restored afterwards).
`test_onelibrary_anlz.py` replays each edit with Konduktor's helpers on
`before/` and compares the memory lists with `after/` byte for byte.

Only `PPTH`, `PQTZ`, `PCOB`, `PCO2` and `PQT2` are kept, byte for byte — the
waveforms were dropped, as in `onelibrary-goober/`. There is no database and
no audio: `exportLibrary.db` and both pdbs had NO row change for any of these
edits (memory cues on a stick live only in the analysis files).

| File | Track | Edit (one per track) |
|---|---|---|
| `black-bloc` | Justin Hawkes – Black Bloc (VIP) | memory cue ADDED at 2:11.206 |
| `formula` | RL Grime & Juelz – Formula (IMANU Remix) | 4-beat memory LOOP added at 1:20.378 |
| `lift-it` | Tian – Lift It (Hexa Remix) | 8 memory cues added (2 → 10, the cap) |
| `wtf` | Result – WTF (Phace Remix) | the memory loop at 1:53 recoloured Blue |
| `sell-my-soul` | Bensley, Justin Hawkes & Kumarion – Sell My Soul | comment "Loopy" on the memory loop at 1:18 |
| `doomed` | Nemean – Doomed | the memory loop at 2:01 DELETED |

What they show (the full write-up is in
`.claude/discussions/discuss-memory-cues-2026-10-16.md`, kit M2):
- `.DAT` `PCOB` memory list: a new entry is PREPENDED; every entry has status 0
  and bytes 24–27 = previous / next index in the list (`ffff` at the ends), so
  any add or delete relinks the whole list; the header i32 at offset 20 is
  count − 1.
- `.EXT` `PCO2` memory list: a new entry is APPENDED in rekordbox's compact
  44-byte form; the others stay byte-identical. The `.EXT`'s `PCOB` memory list
  stays empty.
- When rekordbox rewrites a `.DAT` (any memory add or delete), it ALSO
  re-sorts the untouched hot-cue `PCOB` into ascending pad order (an export
  writes it highest pad first); the entries themselves are unchanged.
  Konduktor changes only the list it edits, so the test compares that tag's
  entries regardless of order.
- Colour: `PCP2` byte 28, 1-based (Blue = 7), 0 = none. Comment: UTF-16BE + NUL
  at byte 44, `len_comment` at 40. Neither touches the `.DAT`.
