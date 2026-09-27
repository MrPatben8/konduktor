"""The legacy rekordbox device library: `export.pdb` (DeviceSQL), read and written.

What Rekordbox lists as a stick's **Device Library** and what pre-OneLibrary
players (CDJ-2000NXS2, many XDJs) read. Rekordbox writes it beside the
OneLibrary `exportLibrary.db`; both point at the same audio, analysis files and
artwork.

## The format

There is no public specification. The layout used here is the one Deep
Symmetry's crate-digger documents, CONFIRMED byte for byte against a real
rekordbox 7 device export of eight tracks (every row of every table decodes, and
the page arithmetic below reproduces rekordbox's header fields exactly):

  * the file is 4096-byte pages; page 0 is a header listing 20 tables, each a
    chain of pages `first -> ... -> last -> empty_candidate` (the candidate is a
    reserved page, possibly beyond the end of the file);
  * every table starts with a HEADER page that holds no rows (flags 0x64) but
    DOES hold an index: the table's first data page, and for tracks, the
    playlist tree and history an entry per data page (`page << 3 | flag`, 3 =
    has room). A first Konduktor export left these pointing nowhere and
    rekordbox 7 called the device library corrupted; rows live in DATA pages
    (flags 0x34 for tracks and the playlist tree, else 0x24);
  * a data page is a 0x28-byte header, a heap of rows (each 4-byte aligned) from
    the top, and a row index growing from the bottom in groups of 16: per group
    16 u16 heap offsets, a u16 "present" bitmask and a u16 bitmask of the last
    row written;
  * strings are DeviceSQL strings: short ASCII `((len+1)<<1)|1` + bytes, long
    ASCII `0x40` + u16 length + pad, or UTF-16LE `0x90` + u16 length + pad.

## Writing

**From a template, not from nothing** (`fixtures/rekordbox/device/`): the empty
library rekordbox itself writes for a new stick. It carries the tables that are
the same on every stick and cannot be derived (colours, browse columns, menus,
the history header), and reserves a blank page after each data table's header
page. Filling a table follows rekordbox's own allocation, read off a real
export: the first data page is the table's reserved candidate, further pages and
the new candidate come from the file header's next-unused counter, and any
reserved page left inside the file stays blank.

Data pages are written the way a rekordbox EXPORT writes them (rows appended
one by one): `u5` = 1, `num_rows_large` = rows - 1, and each index group's
"last written" mask names its final row.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

PAGE = 4096
_HEADER = 0x28
_GROUP = 0x24          # one row-index group: 16 offsets + present + last-written

TRACKS, GENRES, ARTISTS, ALBUMS, LABELS, KEYS, COLORS = 0, 1, 2, 3, 4, 5, 6
PLAYLIST_TREE, PLAYLIST_ENTRIES, ARTWORK, HISTORY = 7, 8, 13, 19

#: Data-page flags per table, as rekordbox writes them.
_PAGE_FLAGS = {TRACKS: 0x34, PLAYLIST_TREE: 0x34}
_DEFAULT_FLAGS = 0x24
_HEADER_PAGE = 0x40    # bit set on header / non-data pages

TEMPLATE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "rekordbox" / "device"


# ---- strings -------------------------------------------------------------------


def dsql(text: str | None) -> bytes:
    """A DeviceSQL string."""
    text = text or ""
    try:
        raw = text.encode("ascii")
    except UnicodeEncodeError:
        wide = text.encode("utf-16-le")
        return struct.pack("<BHB", 0x90, len(wide) + 4, 0) + wide
    if len(raw) <= 126:
        return bytes([((len(raw) + 1) << 1) | 1]) + raw
    return struct.pack("<BHB", 0x40, len(raw) + 4, 0) + raw


def read_dsql(buf: bytes, off: int) -> str:
    head = buf[off]
    if head & 1:
        n = (head >> 1) - 1
        return buf[off + 1:off + 1 + n].decode("ascii", "replace")
    n = struct.unpack_from("<H", buf, off + 1)[0]
    body = buf[off + 4:off + n]
    return body.decode("utf-16-le" if head == 0x90 else "ascii", "replace")


def _pad4(row: bytes) -> bytes:
    return row + bytes(-len(row) % 4)


# ---- rows ----------------------------------------------------------------------

#: The track row's fixed part, in order: 94 bytes.
_TRACK_FIELDS = (
    ("subtype", "H"), ("index_shift", "H"), ("bitmask", "I"), ("sample_rate", "I"),
    ("composer_id", "I"), ("file_size", "I"), ("u2", "I"), ("u3", "H"), ("u4", "H"),
    ("artwork_id", "I"), ("key_id", "I"), ("original_artist_id", "I"), ("label_id", "I"),
    ("remixer_id", "I"), ("bitrate", "I"), ("track_number", "I"), ("tempo", "I"),
    ("genre_id", "I"), ("album_id", "I"), ("artist_id", "I"), ("id", "I"),
    ("disc_number", "H"), ("play_count", "H"), ("year", "H"), ("sample_depth", "H"),
    ("duration", "H"), ("u5", "H"), ("color_id", "B"), ("rating", "B"), ("u6", "H"), ("u7", "H"),
)
_TRACK_FMT = "<" + "".join(f for _, f in _TRACK_FIELDS)
_TRACK_FIXED = struct.calcsize(_TRACK_FMT)   # 94
#: The 21 string slots, in the order their offsets follow the fixed part.
TRACK_STRINGS = (
    "isrc", "texter", "unknown_string_2", "unknown_string_3", "unknown_string_4",
    "message", "kuvo_public", "autoload_hotcues", "unknown_string_5", "unknown_string_6",
    "date_added", "release_date", "mix_name", "unknown_string_7", "analyze_path",
    "analyze_date", "comment", "title", "unknown_string_8", "filename", "file_path",
)
_INDEX_SHIFT_AT = 2    # tracks, artists and albums carry their slot << 5 here
#: Tables whose header page indexes every data page (the rest only point at the
#: first). History (19) is already indexed in the template.
_INDEXED = (TRACKS, PLAYLIST_TREE)
_NONE = 0x1FFF


def track_row(fields: dict, strings: dict) -> bytes:
    """A tracks-table row. `fields` fills the fixed part (missing = 0);
    `strings` the string slots (missing = empty)."""
    values = [fields.get(name, 0) for name, _ in _TRACK_FIELDS]
    fixed = struct.pack(_TRACK_FMT, *values)
    offsets, heap = [], b""
    base = _TRACK_FIXED + 2 * len(TRACK_STRINGS)
    for name in TRACK_STRINGS:
        offsets.append(base + len(heap))
        heap += dsql(strings.get(name))
    return _pad4(fixed + struct.pack(f"<{len(offsets)}H", *offsets) + heap)


def id_string_row(row_id: int, text: str) -> bytes:
    """Genres, labels and artwork: u32 id + string."""
    return _pad4(struct.pack("<I", row_id) + dsql(text))


def key_row(row_id: int, name: str) -> bytes:
    """Keys store their id twice."""
    return _pad4(struct.pack("<II", row_id, row_id) + dsql(name))


def artist_row(row_id: int, name: str) -> bytes:
    """Subtype 0x60: the name starts at a fixed offset of 10."""
    return _pad4(struct.pack("<HHIBB", 0x60, 0, row_id, 0x03, 10) + dsql(name))


def album_row(row_id: int, name: str, artist_id: int = 0) -> bytes:
    """Subtype 0x80: the name starts at 22. rekordbox's own export leaves the
    album's artist 0 even when the track has one, so the default follows it."""
    return _pad4(struct.pack("<HHIIIIBB", 0x80, 0, 0, artist_id, row_id, 0, 0x03, 22) + dsql(name))


def playlist_tree_row(row_id: int, parent_id: int, sort_order: int, name: str, *, folder: bool) -> bytes:
    return _pad4(struct.pack("<IIIII", parent_id, 0, sort_order, row_id, 1 if folder else 0) + dsql(name))


def playlist_entry_row(entry_index: int, track_id: int, playlist_id: int) -> bytes:
    return struct.pack("<III", entry_index, track_id, playlist_id)


# ---- pages ---------------------------------------------------------------------


def _index_space(n: int) -> int:
    """Bytes the row index occupies at the bottom of a page holding `n` rows."""
    return ((n + 15) // 16) * _GROUP


def _chunk(rows: list[bytes]) -> list[list[bytes]]:
    """Greedy: as many rows per page as fit beside their index."""
    pages, cur, used = [], [], 0
    for row in rows:
        if cur and _HEADER + used + len(row) + _index_space(len(cur) + 1) > PAGE:
            pages.append(cur)
            cur, used = [], 0
        if _HEADER + len(row) + _index_space(1) > PAGE:
            raise ValueError(f"a {len(row)}-byte row cannot fit in a page")
        cur.append(row)
        used += len(row)
    if cur:
        pages.append(cur)
    return pages


def _data_page(index: int, table: int, next_page: int, rows: list[bytes], sequence: int,
               *, shift_at: int | None, fill: tuple[int, int] | None = None) -> bytes:
    page = bytearray(PAGE)
    heap = bytearray()
    offsets = []
    for slot, row in enumerate(rows):
        row = bytearray(row)
        if shift_at is not None:
            struct.pack_into("<H", row, shift_at, slot << 5)
        offsets.append(len(heap))
        heap += row
    n = len(rows)
    groups = (n + 15) // 16
    page[_HEADER:_HEADER + len(heap)] = heap
    for g in range(groups):
        base = PAGE - g * _GROUP
        k = min(16, n - 16 * g)
        struct.pack_into("<H", page, base - 4, (1 << k) - 1)      # present
        struct.pack_into("<H", page, base - 2, 1 << (k - 1))      # last written
        for r in range(k):
            struct.pack_into("<H", page, base - 6 - 2 * r, offsets[16 * g + r])
    used = len(heap)
    free = PAGE - _HEADER - used - (2 * n + 4 * groups)
    struct.pack_into("<6I", page, 0, 0, index, table, next_page, sequence, 0)
    page[0x18] = n & 0xFF
    page[0x19] = ((n >> 8) & 0x1F) | ((n & 0x07) << 5)
    page[0x1A] = (n >> 3) & 0xFF
    page[0x1B] = _PAGE_FLAGS.get(table, _DEFAULT_FLAGS)
    u5, nrl = fill if fill is not None else (1, n - 1)
    struct.pack_into("<6H", page, 0x1C, free, used, u5, nrl, 0, 0)
    return bytes(page)


# ---- the writer ----------------------------------------------------------------


class PdbWriter:
    """Fills an empty rekordbox device library (the template) with rows."""

    def __init__(self, template: bytes):
        self.buf = bytearray(template)
        self.pages: dict[int, bytes] = {}
        n_tables = struct.unpack_from("<I", self.buf, 8)[0]
        self.next_unused = struct.unpack_from("<I", self.buf, 12)[0]
        self.sequence = struct.unpack_from("<I", self.buf, 20)[0]
        self.tables = {}
        self.pages_with_room = 0
        for i in range(n_tables):
            t, empty, first, last = struct.unpack_from("<4I", self.buf, 28 + 16 * i)
            self.tables[t] = [i, empty, first, last]

    def _alloc(self) -> int:
        page = self.next_unused
        self.next_unused += 1
        return page

    def fill(self, table: int, rows: list[bytes]) -> None:
        """Write `rows` into `table`, which must still be empty (header page only)."""
        if not rows:
            return
        slot, empty, first, last = self.tables[table]
        if first != last:
            raise ValueError(f"table {table} already has data pages")
        chunks = _chunk(rows)
        indices = [empty] + [self._alloc() for _ in chunks[1:]]
        candidate = self._alloc()
        shift_at = _INDEX_SHIFT_AT if table in (TRACKS, ARTISTS, ALBUMS) else None
        indexed = table in _INDEXED
        for i, chunk in enumerate(chunks):
            nxt = indices[i + 1] if i + 1 < len(indices) else candidate
            last_page = i + 1 == len(chunks)
            # Indexed tables mark a full page 0x1FFF/0x1FFF and a page with room
            # rows+1 / 0 — as a real export's tracks and playlist pages do.
            fill = None
            if indexed:
                fill = (len(chunk) + 1, 0) if last_page else (_NONE, _NONE)
            self.sequence += 1
            self.pages[indices[i]] = _data_page(indices[i], table, nxt, chunk, self.sequence,
                                                shift_at=shift_at, fill=fill)
        self._index(first, indices, indexed=indexed)
        self.tables[table] = [slot, candidate, first, indices[-1]]

    def _index(self, header_page: int, data_pages: list[int], *, indexed: bool) -> None:
        """Point the table's HEADER page at its data.

        A header page is not empty: past its 0x28-byte header it holds an index —
        its own number, the FIRST DATA PAGE, 0x03FFFFFF, 0, a u16 entry count,
        0x1FFF, then entries `page << 3 | flag` padded with 0x1FFFFFF8. Every
        table with data points at its first data page (left at 0x03FFFFFF,
        rekordbox calls the library corrupted); the indexed tables (tracks, the
        playlist tree, history) also list every data page — flag 3 for a page
        with room, 0 for a full one — and the header's u7 is that count, u5 the
        number of pages with room and num_rows_large the first of them.
        """
        o = header_page * PAGE
        struct.pack_into("<I", self.buf, o + _HEADER + 4, data_pages[0])
        if not indexed:
            return
        flags = [3 if i + 1 == len(data_pages) else 0 for i in range(len(data_pages))]
        struct.pack_into("<HH", self.buf, o + _HEADER + 16, len(data_pages), 0x1FFF)
        for k, (page, flag) in enumerate(zip(data_pages, flags)):
            struct.pack_into("<I", self.buf, o + _HEADER + 20 + 4 * k, (page << 3) | flag)
        room = [k for k, flag in enumerate(flags) if flag == 3]
        struct.pack_into("<HH", self.buf, o + 0x20, len(room) or _NONE, room[0] if room else _NONE)
        struct.pack_into("<H", self.buf, o + 0x26, len(data_pages))
        self.pages_with_room += len(room)

    def stamp_track_count(self, n: int) -> None:
        """The history header row counts the library's tracks (and flags that
        it has any): 8 on a real 8-track export, 0 in the empty template."""
        _, _, first, last = self.tables[HISTORY]
        for _header, row in _rows(bytes(self.buf), first, last):
            if row is not None:
                self.buf[row + 3] = 1 if n else 0
                struct.pack_into("<I", self.buf, row + 4, n)
                break
        if n:
            # A library with tracks also INDEXES its history page — entry
            # `page << 3 | 0`, count 1, u7 1 — which the empty template does not.
            o = first * PAGE
            struct.pack_into("<HH", self.buf, o + _HEADER + 16, 1, 0x1FFF)
            struct.pack_into("<I", self.buf, o + _HEADER + 20, last << 3)
            struct.pack_into("<H", self.buf, o + 0x26, 1)

    def stamp_history_date(self, when: date) -> None:
        """The history header row records the library's creation date."""
        _, _, first, last = self.tables[HISTORY]
        o = last * PAGE
        page = self.buf[o:o + PAGE]
        m = re.search(rb"\d{4}-\d{2}-\d{2}", page[_HEADER:])
        if m:
            at = o + _HEADER + m.start()
            self.buf[at:at + 10] = when.isoformat().encode("ascii")

    def render(self) -> bytes:
        for t, (slot, empty, first, last) in self.tables.items():
            struct.pack_into("<4I", self.buf, 28 + 16 * slot, t, empty, first, last)
        struct.pack_into("<I", self.buf, 12, self.next_unused)
        # 1 in the empty template, 4 on a real export whose index lists three
        # pages with room: 1 + that count fits both. The least certain field here.
        struct.pack_into("<I", self.buf, 16, 1 + self.pages_with_room)
        struct.pack_into("<I", self.buf, 20, self.sequence + 1)
        top = max([len(self.buf) // PAGE - 1, *self.pages])
        out = bytearray(self.buf) + bytes((top + 1) * PAGE - len(self.buf))
        for index, page in self.pages.items():
            out[index * PAGE:(index + 1) * PAGE] = page
        return bytes(out)


def template(name: str = "export.pdb") -> bytes:
    return (TEMPLATE_DIR / name).read_bytes()


# ---- the reader ----------------------------------------------------------------


@dataclass
class Library:
    """What a device library holds, decoded — for tests and diagnostics."""

    tracks: list[dict] = field(default_factory=list)
    genres: dict[int, str] = field(default_factory=dict)
    artists: dict[int, str] = field(default_factory=dict)
    albums: dict[int, str] = field(default_factory=dict)
    labels: dict[int, str] = field(default_factory=dict)
    keys: dict[int, str] = field(default_factory=dict)
    artwork: dict[int, str] = field(default_factory=dict)
    playlists: list[dict] = field(default_factory=list)
    entries: list[tuple[int, int, int]] = field(default_factory=list)
    pages: dict[int, list[dict]] = field(default_factory=dict)   # table -> page headers


def _rows(buf: bytes, first: int, last: int):
    """(page header dict, row offset) for every present row of a table."""
    p, seen = first, 0
    while True:
        o = p * PAGE
        if o + PAGE > len(buf):
            return
        flags = buf[o + 0x1B]
        n = (buf[o + 0x19] >> 5) | (buf[o + 0x1A] << 3)
        header = {"index": p, "flags": flags, "rows": n,
                  "next": struct.unpack_from("<I", buf, o + 12)[0],
                  "free": struct.unpack_from("<H", buf, o + 0x1C)[0],
                  "used": struct.unpack_from("<H", buf, o + 0x1E)[0]}
        yield header, None
        if not flags & _HEADER_PAGE:
            for g in range((n + 15) // 16):
                base = o + PAGE - g * _GROUP
                present = struct.unpack_from("<H", buf, base - 4)[0]
                for r in range(16):
                    if present & (1 << r):
                        yield header, o + _HEADER + struct.unpack_from("<H", buf, base - 6 - 2 * r)[0]
        if p == last or seen > 10000:
            return
        p, seen = header["next"], seen + 1


def read(buf: bytes) -> Library:
    lib = Library()
    n_tables = struct.unpack_from("<I", buf, 8)[0]
    for i in range(n_tables):
        t, _empty, first, last = struct.unpack_from("<4I", buf, 28 + 16 * i)
        for header, row in _rows(buf, first, last):
            if row is None:
                lib.pages.setdefault(t, []).append(header)
                continue
            if t == TRACKS:
                values = dict(zip([n for n, _ in _TRACK_FIELDS], struct.unpack_from(_TRACK_FMT, buf, row)))
                offs = struct.unpack_from(f"<{len(TRACK_STRINGS)}H", buf, row + _TRACK_FIXED)
                values.update({name: read_dsql(buf, row + o) for name, o in zip(TRACK_STRINGS, offs)})
                lib.tracks.append(values)
            elif t in (GENRES, LABELS, ARTWORK):
                target = {GENRES: lib.genres, LABELS: lib.labels, ARTWORK: lib.artwork}[t]
                target[struct.unpack_from("<I", buf, row)[0]] = read_dsql(buf, row + 4)
            elif t == KEYS:
                lib.keys[struct.unpack_from("<I", buf, row)[0]] = read_dsql(buf, row + 8)
            elif t == ARTISTS:
                sub, _shift, rid, _b, ofs = struct.unpack_from("<HHIBB", buf, row)
                if sub == 0x64:
                    ofs = struct.unpack_from("<H", buf, row + 10)[0]
                lib.artists[rid] = read_dsql(buf, row + ofs)
            elif t == ALBUMS:
                sub, _shift, _u, artist, rid, _u3, _b, ofs = struct.unpack_from("<HHIIIIBB", buf, row)
                lib.albums[rid] = read_dsql(buf, row + ofs)
            elif t == PLAYLIST_TREE:
                parent, _u, sort, rid, folder = struct.unpack_from("<5I", buf, row)
                lib.playlists.append({"id": rid, "parent": parent, "sort": sort,
                                      "folder": bool(folder), "name": read_dsql(buf, row + 20)})
            elif t == PLAYLIST_ENTRIES:
                lib.entries.append(struct.unpack_from("<3I", buf, row))
    return lib


__all__ = [
    "ALBUMS", "ARTISTS", "ARTWORK", "GENRES", "KEYS", "LABELS", "Library", "PAGE",
    "PLAYLIST_ENTRIES", "PLAYLIST_TREE", "PdbWriter", "TRACKS", "TRACK_STRINGS",
    "album_row", "artist_row", "dsql", "id_string_row", "key_row", "playlist_entry_row",
    "playlist_tree_row", "read", "read_dsql", "template", "track_row",
]
