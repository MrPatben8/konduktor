"""Keeping a stick's legacy Device Library (`export.pdb`) in step with its OneLibrary.

Most rekordbox sticks carry BOTH: `exportLibrary.db` (OneLibrary, newer players)
and `export.pdb` (the Device Library, older players and rekordbox's own "Device
Library" view). Cues and the grid live in the analysis files both share, so they
follow an edit by themselves; titles and playlists live in each database
separately. **rekordbox itself lets them drift**: measured on Goober
(2026-10-01), its edits updated the pdb's track metadata but not a new playlist
or a reorder. Konduktor rebuilds the pdb at Save instead (decision 2 of the
editing discussion), so a stick never shows different playlists depending on
which player reads it.

**Ids are mirrored, not assigned.** rekordbox exports both libraries from the
same ids — pdb track id = `content_id`, the artist/album/genre/label/key ids, the
playlist ids, playlist `sort` = `sequenceNo`, entries `(sequenceNo, content_id,
playlist_id)`, artwork id = `image_id` — all checked on Goober. So the OneLibrary
database IS the answer for every row the pdb shares with it.

**An edit, not a regeneration.** The stick's own `export.pdb` is edited in place
(`pdb.PdbEditor`): only the tracks, the four lookup tables and the playlist tree
and entries are candidates, a table is rewritten only if one of its rows changed,
and an unchanged row keeps rekordbox's bytes. A changed TRACK row is the old row
with the edited fields overwritten, so everything the pdb holds that OneLibrary
does not — the phrase-analysis strings, the `masterDbId` halves, play counts,
dates — survives. History, colours, menus, artwork, keys and `exportExt.pdb` are
never touched.
"""
from __future__ import annotations

import struct
from pathlib import Path

from ..rekordbox import pdb

#: Tables this rebuild owns. Everything else in the file is left as it is.
_OWNED = (pdb.TRACKS, pdb.ARTISTS, pdb.ALBUMS, pdb.GENRES, pdb.LABELS,
          pdb.PLAYLIST_TREE, pdb.PLAYLIST_ENTRIES)


def _lookup_name(table: int, row: bytes) -> str:
    if table == pdb.ARTISTS:
        sub, _shift, _rid, _b, ofs = struct.unpack_from("<HHIBB", row, 0)
        if sub == 0x64:
            ofs = struct.unpack_from("<H", row, 10)[0]
        return pdb.read_dsql(row, ofs)
    if table == pdb.ALBUMS:
        sub, _shift, _u, _artist, _rid, _u3, _b, ofs = struct.unpack_from("<HHIIIIBB", row, 0)
        return pdb.read_dsql(row, ofs)
    return pdb.read_dsql(row, 4)


def _tree_value(row: bytes) -> tuple:
    parent, _u, sort, rid, folder = struct.unpack_from("<5I", row, 0)
    return parent, sort, rid, bool(folder), pdb.read_dsql(row, 20)


def _merge(old: list[bytes], new: dict, table: int, same) -> list[bytes] | None:
    """The table's rows: old ORDER kept, an unchanged row's old BYTES kept, new
    rows appended by id. None when nothing changed (the table is not rewritten).

    `new` maps row id -> (value, encode()); `same(old_row, value)` says whether
    an old row already says `value`.
    """
    out, changed, seen = [], False, set()
    for row in old:
        rid = pdb.row_id(table, row)
        if rid not in new:
            changed = True  # removed
            continue
        seen.add(rid)
        value, encode = new[rid]
        if same(row, value):
            out.append(row)
        else:
            out.append(encode())
            changed = True
    for rid in sorted((r for r in new if r not in seen), key=str):
        out.append(new[rid][1]())
        changed = True
    return out if changed else None


def rebuild(existing: bytes, session) -> bytes | None:
    """`export.pdb` edited to match the OneLibrary database open in `session`
    (the working copy, edits committed); None if it already matches."""
    from sqlalchemy import text

    tables: dict[int, list[bytes]] = {}

    # ---- lookups ----------------------------------------------------------------
    for table, sql, build in (
        (pdb.ARTISTS, "SELECT artist_id, name FROM artist", pdb.artist_row),
        # rekordbox's export leaves an album's artist 0 (see `pdb.album_row`).
        (pdb.ALBUMS, "SELECT album_id, name FROM album", pdb.album_row),
        (pdb.GENRES, "SELECT genre_id, name FROM genre", pdb.id_string_row),
        (pdb.LABELS, "SELECT label_id, name FROM label", pdb.id_string_row),
    ):
        want = {int(i): (name or "", (lambda i=i, n=name, b=build: b(int(i), n or "")))
                for i, name in session.execute(text(sql))}
        merged = _merge(pdb.table_rows(existing, table), want, table,
                        lambda row, value, t=table: _lookup_name(t, row) == value)
        if merged is not None:
            tables[table] = merged

    # ---- tracks -----------------------------------------------------------------
    old_tracks = {pdb.row_id(pdb.TRACKS, r): r for r in pdb.table_rows(existing, pdb.TRACKS)}
    want = {}
    for (cid, title, artist, remixer, album, genre, label, rating, bpm, year, released,
         comment, *fresh) in session.execute(text(
            "SELECT content_id, title, artist_id_artist, artist_id_remixer, album_id, genre_id, "
            "label_id, rating, bpmx100, releaseYear, releaseDate, djComment, "
            "path, fileName, fileSize, fileType, bitrate, bitDepth, samplingRate, length, "
            "analysisDataFilePath, dateAdded, key_id, image_id FROM content")):
        cid = int(cid)
        if cid in old_tracks:
            fields, strings = pdb.track_fields(old_tracks[cid])
        else:
            fields, strings = _fresh_track(cid, *fresh)
        fields = {**fields, "artist_id": artist or 0, "remixer_id": remixer or 0,
                  "album_id": album or 0, "genre_id": genre or 0, "label_id": label or 0,
                  "rating": max(0, min(5, int(rating or 0))), "tempo": int(bpm or 0),
                  "year": int(year or 0)}
        strings = {**strings, "title": title or "", "comment": comment or "",
                   "release_date": released or ""}
        want[cid] = ((fields, strings),
                     (lambda f=fields, s=strings: pdb.track_row(f, s)))
    merged = _merge(list(old_tracks.values()), want, pdb.TRACKS,
                    lambda row, value: _same_track(pdb.track_fields(row), value))
    if merged is not None:
        tables[pdb.TRACKS] = merged

    # ---- playlists ----------------------------------------------------------------
    want = {int(pid): ((int(parent or 0), int(seq or 0), int(pid), int(attr or 0) == 1, name or ""),
                       (lambda p=pid, par=parent, s=seq, n=name, a=attr:
                        pdb.playlist_tree_row(int(p), int(par or 0), int(s or 0), n or "",
                                              folder=int(a or 0) == 1)))
            for pid, parent, seq, name, attr in session.execute(text(
                "SELECT playlist_id, playlist_id_parent, sequenceNo, name, attribute FROM playlist"))}
    merged = _merge(pdb.table_rows(existing, pdb.PLAYLIST_TREE), want, pdb.PLAYLIST_TREE,
                    lambda row, value: _tree_value(row) == value)
    if merged is not None:
        tables[pdb.PLAYLIST_TREE] = merged

    want = {}
    for pid, cid, seq in session.execute(text(
            "SELECT playlist_id, content_id, sequenceNo FROM playlist_content "
            "ORDER BY playlist_id, sequenceNo")):
        key = (int(seq), int(cid), int(pid))
        want[key] = (key, (lambda k=key: pdb.playlist_entry_row(*k)))
    merged = _merge(pdb.table_rows(existing, pdb.PLAYLIST_ENTRIES), want, pdb.PLAYLIST_ENTRIES,
                    lambda row, value: True)
    if merged is not None:
        tables[pdb.PLAYLIST_ENTRIES] = merged

    if not tables:
        return None
    editor = pdb.PdbEditor(existing)
    for table in _OWNED:
        if table in tables:
            editor.refill(table, tables[table])
    return editor.render()


def _same_track(old: tuple[dict, dict], new: tuple[dict, dict]) -> bool:
    """Equal in every field that means something: `index_shift` is the row's slot
    in its page, re-stamped whenever a page is written."""
    (of, os_), (nf, ns) = old, new
    return ({k: v for k, v in of.items() if k != "index_shift"}
            == {k: v for k, v in nf.items() if k != "index_shift"} and os_ == ns)


def _fresh_track(cid: int, path, file_name, file_size, file_type, bitrate, bit_depth,
                 sample_rate, length, analysis, date_added, key_id, image_id) -> tuple[dict, dict]:
    """A track the pdb has never listed (one added to the stick by Konduktor), in
    the shape `device_export` writes one — the values a rekordbox device export
    was measured to carry (see that module)."""
    from ..rekordbox.device_export import ANALYSED, BITMASK

    added = str(date_added or "")[:10]
    fields = {
        "subtype": 0x24, "bitmask": BITMASK, "sample_rate": int(sample_rate or 0),
        "file_size": int(file_size or 0), "u2": cid + 0x600, "artwork_id": int(image_id or 0),
        "key_id": int(key_id or 0), "bitrate": int(bitrate or 0), "id": cid,
        "sample_depth": int(bit_depth or 0), "duration": min(int(length or 0), 0xFFFF),
        "u5": ANALYSED, "u6": int(file_type or 0), "u7": 3,
    }
    strings = {
        "kuvo_public": "ON", "autoload_hotcues": "ON", "date_added": added,
        "analyze_path": analysis or "", "analyze_date": added,
        "filename": file_name or Path(str(path or "")).name, "file_path": path or "",
    }
    return fields, strings
