"""Copy a track's file tags into the MP4 a stem conversion writes.

A converted `.stem.m4a` replaces its source file — in Replace mode the source is
deleted on Save — so every tag the source carried must survive the move, not just
the handful Konduktor edits (`audio_tags.write_tags`). The collection keeps its
own copy of the metadata either way; this is about the FILE, which is what
travels: another DJ app, a stick, a re-import.

One canonical field list, read from whichever tag format the source uses (ID3 on
MP3/AIFF/WAV, Vorbis comments on FLAC/Ogg, MP4 atoms on M4A) and written as the
MP4 atoms the user's own commercial stem files use — `tmpo` for BPM and the
iTunes freeform `initialkey` for the key — plus the freeform names
`audio_tags._write_mp4` already writes (LABEL / REMIXER / PRODUCER / MIX), so a
later tag edit updates the same atom. Cover art is read by `audio_tags.read_cover`.

Deliberately NOT copied: the rating (the collection owns it, and players store
it under incompatible schemes), ReplayGain / loudness (it describes the SOURCE
encode), and app-private blobs (Traktor's own `NITR`/PRIV data: Traktor rewrites
its analysis for the new file).
"""
from __future__ import annotations

from pathlib import Path

from . import audio_tags

#: canonical field -> MP4 atom. `None` = an iTunes freeform atom named below.
_MP4_ATOMS = {
    "title": "\xa9nam",
    "artist": "\xa9ART",
    "album": "\xa9alb",
    "albumartist": "aART",
    "genre": "\xa9gen",
    "comment": "\xa9cmt",
    "date": "\xa9day",
    "composer": "\xa9wrt",
    "grouping": "\xa9grp",
    "copyright": "cprt",
    "lyrics": "\xa9lyr",
}
_FREEFORM = {
    "key": "initialkey",
    "label": "LABEL",
    "remixer": "REMIXER",
    "producer": "PRODUCER",
    "mix": "MIX",
    "isrc": "ISRC",
}
#: Structured MP4 atoms: track/disc number pairs, BPM, compilation flag.
_STRUCTURED = ("tracknumber", "discnumber", "bpm", "compilation")

# ---- reading ---------------------------------------------------------------
_ID3_TEXT = {
    "TIT2": "title", "TPE1": "artist", "TALB": "album", "TPE2": "albumartist",
    "TCON": "genre", "TDRC": "date", "TCOM": "composer", "GRP1": "grouping",
    "TIT1": "grouping", "TCOP": "copyright", "TKEY": "key", "TPUB": "label",
    "TPE4": "remixer", "TIT3": "mix", "TSRC": "isrc", "TRCK": "tracknumber",
    "TPOS": "discnumber", "TBPM": "bpm", "TCMP": "compilation",
}
_VORBIS = {
    "title": "title", "artist": "artist", "album": "album", "albumartist": "albumartist",
    "genre": "genre", "comment": "comment", "description": "comment", "date": "date",
    "composer": "composer", "grouping": "grouping", "copyright": "copyright",
    "lyrics": "lyrics", "unsyncedlyrics": "lyrics", "initialkey": "key", "key": "key",
    "label": "label", "organization": "label", "publisher": "label", "remixer": "remixer",
    "producer": "producer", "mixname": "mix", "isrc": "isrc", "tracknumber": "tracknumber",
    "discnumber": "discnumber", "bpm": "bpm", "compilation": "compilation",
}


def _first(values) -> str | None:
    for v in values or []:
        s = str(v).strip()
        if s:
            return s
    return None


def _read_id3(tags) -> dict[str, str]:
    out: dict[str, str] = {}
    for frame_id, field in _ID3_TEXT.items():
        frame = tags.get(frame_id)
        if frame is not None and field not in out:
            val = _first(getattr(frame, "text", []))
            if val:
                out[field] = val
    for frame in tags.getall("TXXX"):
        if frame.desc.upper() == "PRODUCER" and "producer" not in out:
            out["producer"] = _first(frame.text) or ""
    comm = [f for f in tags.getall("COMM") if _first(f.text)]
    if comm:  # the untitled comment first: that is the one players show
        comm.sort(key=lambda f: f.desc != "")
        out["comment"] = _first(comm[0].text)
    uslt = tags.getall("USLT")
    if uslt and getattr(uslt[0], "text", ""):
        out["lyrics"] = str(uslt[0].text)
    return {k: v for k, v in out.items() if v}


def _read_vorbis(tags) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, values in tags.items():
        field = _VORBIS.get(key.lower())
        if field and field not in out:
            val = _first(values)
            if val:
                out[field] = val
    lower = {k.lower(): v for k, v in tags.items()}
    for field, total in (("tracknumber", "tracktotal"), ("discnumber", "disctotal")):
        if field in out and "/" not in out[field] and _first(lower.get(total)):
            out[field] = f"{out[field]}/{_first(lower[total])}"
    return out


def _read_mp4(tags) -> dict[str, str]:
    out: dict[str, str] = {}
    for field, atom in _MP4_ATOMS.items():
        val = _first(tags.get(atom))
        if val:
            out[field] = val
    for field, name in _FREEFORM.items():
        raw = tags.get(f"----:com.apple.iTunes:{name}")
        if raw:
            val = bytes(raw[0]).decode("utf-8", "replace").strip()
            if val:
                out[field] = val
    for field, atom in (("tracknumber", "trkn"), ("discnumber", "disk")):
        pair = (tags.get(atom) or [None])[0]
        if pair and pair[0]:
            out[field] = f"{pair[0]}/{pair[1]}" if pair[1] else str(pair[0])
    if tags.get("tmpo"):
        out["bpm"] = str(tags["tmpo"][0])
    if tags.get("cpil"):
        out["compilation"] = "1" if tags["cpil"] else "0"
    return out


def read_tags(path: Path | str) -> dict[str, str]:
    """The canonical fields a file carries, whatever its tag format; `{}` for a
    file with none or one mutagen cannot read."""
    import mutagen
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4Tags

    try:
        audio = mutagen.File(str(path))
    except Exception:  # noqa: BLE001 — an unreadable tag is no tags, not a failure
        return {}
    tags = getattr(audio, "tags", None)
    if tags is None:
        return {}
    if isinstance(tags, ID3):
        return _read_id3(tags)
    if isinstance(tags, MP4Tags):
        return _read_mp4(tags)
    if hasattr(tags, "items"):  # Vorbis comments (FLAC, Ogg, Opus)
        return _read_vorbis(tags)
    return {}


# ---- writing ---------------------------------------------------------------
def _number_pair(value: str) -> tuple[int, int] | None:
    head, _, tail = value.partition("/")
    try:
        return int(head), int(tail) if tail.strip() else 0
    except ValueError:
        return None


def write_mp4_tags(path: Path | str, fields: dict[str, str],
                   cover: tuple[bytes, str] | None = None) -> None:
    """Write canonical `fields` (and a cover) into an MP4 file, replacing nothing
    it does not name. Values that cannot be represented (a BPM that is not a
    number) are skipped rather than written wrong.

    Opened as `MP4` explicitly, never by suffix: a conversion writes into a
    `*.konduktor-partial` file, which `audio_tags.write_cover` would refuse as
    an unsupported format — silently losing the art."""
    from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

    audio = MP4(str(path))
    if audio.tags is None:
        audio.add_tags()
    t = audio.tags
    for field, atom in _MP4_ATOMS.items():
        if fields.get(field):
            t[atom] = [fields[field]]
    for field, name in _FREEFORM.items():
        if fields.get(field):
            t[f"----:com.apple.iTunes:{name}"] = [MP4FreeForm(fields[field].encode("utf-8"))]
    for field, atom in (("tracknumber", "trkn"), ("discnumber", "disk")):
        pair = _number_pair(fields.get(field, "")) if fields.get(field) else None
        if pair:
            t[atom] = [pair]
    if fields.get("bpm"):
        try:
            bpm = round(float(fields["bpm"]))
            if 0 < bpm < 65536:
                t["tmpo"] = [bpm]
        except ValueError:
            pass
    if fields.get("compilation"):
        t["cpil"] = fields["compilation"].strip() not in ("", "0", "false", "False")
    if cover is not None:
        data, mime = cover
        fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
        t["covr"] = [MP4Cover(data, imageformat=fmt)]
    audio.save()


def copy_tags(source: Path | str, target: Path | str) -> dict[str, str]:
    """Copy every canonical tag and the cover art from `source` into the MP4
    `target`. Returns the fields copied (for reporting and tests)."""
    fields = read_tags(source)
    cover = audio_tags.read_cover(Path(source))
    if fields or cover is not None:
        write_mp4_tags(target, fields, cover)
    return fields
