"""Memory-cue edits on a track's two analysis files, the way rekordbox 7 makes them.

Pure functions over parsed `AnlzFile`s, so they are pinned byte for byte
against rekordbox's own edits (`fixtures/onelibrary-memory-edits`, kit M2,
2026-10-16) without a drive; the store adds the drive, the time base and Save.

A stick keeps memory cues ONLY in the analysis files — the database and both
pdbs keep every row — in two lists kept in OPPOSITE orders:

  * `.DAT` `PCOB` memory: a new cue is PREPENDED, and the whole list is
    relinked (`anlz_writer.relinked_memory_pcpts`: status 0, prev/next
    indexes) on any add or delete; header field = count − 1.
  * `.EXT` `PCO2` memory: a new cue is APPENDED in the compact 44-byte form;
    every other entry keeps its bytes. The `.EXT`'s own `PCOB` memory list
    stays empty.

A colour is `PCP2` byte 28 and a name the `PCP2` comment; neither touches the
`.DAT`. A cue is addressed by `cues.memory_cue_id` (stored time, kind, loop
end), which is the same in both lists.
"""
from __future__ import annotations

from ..rekordbox import anlz_file
from ..rekordbox import anlz_writer as W
from .cues import memory_cue_id

DAT_ORDER = ("PPTH", "PVBR", "PQTZ", "PWAV", "PWV2", "PCOB")
EXT_ORDER = ("PPTH", "PWV3", "PCOB", "PCO2")


def entries(f: anlz_file.AnlzFile, kind: str) -> list[bytes]:
    """The raw entries of a file's `kind` (PCOB / PCO2) MEMORY list."""
    i = f.find(kind, anlz_file.LIST_MEMORY)
    return anlz_file.raw_entries(f.tags[i]) if i is not None else []


def count(ext: anlz_file.AnlzFile) -> int:
    return len(entries(ext, "PCO2"))


def _matches(entry: bytes, cue_id: str) -> bool:
    return memory_cue_id(anlz_file.decode_entry(entry)) == cue_id


def _put(dat, ext, pcob: list[bytes] | None, pco2: list[bytes] | None) -> None:
    if pcob is not None:
        dat.put("PCOB", W.pcob_tag(anlz_file.LIST_MEMORY, W.relinked_memory_pcpts(pcob)),
                list_kind=anlz_file.LIST_MEMORY, after=DAT_ORDER)
    if pco2 is not None:
        ext.put("PCO2", W.pco2_tag(anlz_file.LIST_MEMORY, pco2),
                list_kind=anlz_file.LIST_MEMORY, after=EXT_ORDER)


def add(dat, ext, *, kind: int, time_ms: int, loop_ms: int | None, beats: int | None,
        name: str | None = None, color_id: int = 0) -> None:
    """Add one memory cue (kind 1 cue, 2 loop): prepended to the `.DAT`'s list,
    appended — compact unless named — to the `.EXT`'s."""
    pcp2 = W.pcp2_compact_entry(kind=kind, time_ms=time_ms, loop_ms=loop_ms,
                                beats=beats, color_id=color_id)
    if name:
        pcp2 = anlz_file.entry_with_comment(pcp2, name)
    pcpt = W.pcpt_entry(hot_cue=0, kind=kind, time_ms=time_ms, loop_ms=loop_ms)
    _put(dat, ext, [pcpt] + entries(dat, "PCOB"), entries(ext, "PCO2") + [pcp2])


def _index(ext, cue_id: str) -> tuple[list[bytes], int | None]:
    pco2 = entries(ext, "PCO2")
    return pco2, next((n for n, e in enumerate(pco2) if _matches(e, cue_id)), None)


def rename(ext, cue_id: str, name: str | None) -> bool:
    """Rename in the `.EXT` only; False if no such cue."""
    pco2, i = _index(ext, cue_id)
    if i is None:
        return False
    pco2[i] = anlz_file.entry_with_comment(pco2[i], name)
    _put(None, ext, None, pco2)
    return True


def recolour(ext, cue_id: str, color_id: int) -> bool:
    """Byte 28 of the cue's `PCP2` entry, nothing else; False if no such cue."""
    pco2, i = _index(ext, cue_id)
    if i is None:
        return False
    pco2[i] = anlz_file.entry_with_color_id(pco2[i], color_id)
    _put(None, ext, None, pco2)
    return True


def delete(dat, ext, cue_id: str) -> bool:
    """Out of both lists (the `.DAT`'s relinked); False if no such cue."""
    pco2, i = _index(ext, cue_id)
    if i is None:
        return False
    del pco2[i]
    pcob = entries(dat, "PCOB")
    j = next((n for n, e in enumerate(pcob) if _matches(e, cue_id)), None)
    if j is not None:
        del pcob[j]
    _put(dat, ext, pcob if j is not None else None, pco2)
    return True
