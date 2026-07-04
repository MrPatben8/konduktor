"""Write track metadata into audio files' embedded tags (best-effort).

Konduktor's source of truth is the `.nml`; this module additionally syncs the
safe metadata fields into the audio file itself so the data travels with the
file (portability to other apps). It is best-effort: if the file isn't present
(e.g. the drive isn't mounted) or the format doesn't support a field, it reports
that rather than failing the whole save.

Field writes are frame/atom-level, so unrelated tags (cover art, beatgrid BPM,
key, track number, private frames) are preserved — verified against real files.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# The safe, editable metadata we sync to files (mirrors PlaylistStore.EDITABLE_FIELDS).
# rating is 0–5 stars.
TagMeta = dict  # keys: title, artist, album, genre, label, remixer, producer,
#                        comment, mix, release_date, rating


@dataclass
class TagResult:
    ok: bool
    status: str  # "written" | "file-not-found" | "unsupported-format" | "error"
    detail: str = ""


def resolve_path(volume: str | None, dir_: str | None, file: str | None) -> Path:
    """Map a Traktor LOCATION to an OS path.

    Paths are trusted (per project decision): boot volume 'Macintosh HD' -> '/',
    a Windows drive like 'X:' stays a drive path, other volumes -> /Volumes/<v>.
    """
    d = (dir_ or "").replace("/:", "/")
    name = file or ""
    vol = volume or ""
    if vol.endswith(":"):  # Windows drive letter, e.g. "X:"
        return Path(vol + d + name)
    if vol and vol != "Macintosh HD":  # other mounted volume (macOS/*nix)
        return Path("/Volumes") / vol / (d.lstrip("/") + name)
    return Path(d + name)  # boot volume


def read_cover(path: Path) -> tuple[bytes, str] | None:
    """Return (image_bytes, mime) of the file's embedded front cover, or None."""
    if not path.exists():
        return None
    ext = path.suffix.lower()
    try:
        if ext in (".mp3", ".aif", ".aiff"):
            from mutagen.id3 import ID3, ID3NoHeaderError

            try:
                id3 = ID3(path)
            except ID3NoHeaderError:
                return None
            apics = id3.getall("APIC")
            if not apics:
                return None
            front = next((a for a in apics if a.type == 3), apics[0])
            return bytes(front.data), (front.mime or "image/jpeg")
        if ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4

            covr = MP4(path).tags.get("covr") if MP4(path).tags else None
            if not covr:
                return None
            c = covr[0]
            from mutagen.mp4 import MP4Cover

            mime = "image/png" if c.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg"
            return bytes(c), mime
        if ext == ".flac":
            from mutagen.flac import FLAC

            pics = FLAC(path).pictures
            if not pics:
                return None
            front = next((p for p in pics if p.type == 3), pics[0])
            return bytes(front.data), (front.mime or "image/jpeg")
    except Exception:  # noqa: BLE001
        return None
    return None


def write_cover(path: Path, data: bytes, mime: str) -> TagResult:
    """Replace the file's embedded front cover art. Best-effort."""
    if not path.exists():
        return TagResult(False, "file-not-found", str(path))
    ext = path.suffix.lower()
    mime = mime if mime in ("image/jpeg", "image/png") else "image/jpeg"
    try:
        if ext in (".mp3", ".aif", ".aiff"):
            from mutagen.id3 import APIC

            if ext == ".mp3":
                from mutagen.id3 import ID3, ID3NoHeaderError

                try:
                    tags = ID3(path)
                except ID3NoHeaderError:
                    tags = ID3()
                tags.delall("APIC")
                tags.add(APIC(encoding=3, mime=mime, type=3, desc="", data=data))
                tags.save(path)
            else:
                from mutagen.aiff import AIFF

                audio = AIFF(path)
                if audio.tags is None:
                    audio.add_tags()
                audio.tags.delall("APIC")
                audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="", data=data))
                audio.save()
        elif ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4, MP4Cover

            audio = MP4(path)
            if audio.tags is None:
                audio.add_tags()
            fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            audio.tags["covr"] = [MP4Cover(data, imageformat=fmt)]
            audio.save()
        elif ext == ".flac":
            from mutagen.flac import FLAC, Picture

            audio = FLAC(path)
            audio.clear_pictures()
            pic = Picture()
            pic.type = 3
            pic.mime = mime
            pic.data = data
            audio.add_picture(pic)
            audio.save()
        else:
            return TagResult(False, "unsupported-format", ext)
    except Exception as ex:  # noqa: BLE001
        return TagResult(False, "error", f"{type(ex).__name__}: {ex}")
    return TagResult(True, "written")


def _clean(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def write_tags(path: Path, meta: TagMeta) -> TagResult:
    if not path.exists():
        return TagResult(False, "file-not-found", str(path))
    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            _write_id3(path, meta, id3_only=False)
        elif ext in (".aif", ".aiff"):
            _write_aiff(path, meta)
        elif ext in (".m4a", ".mp4") or path.name.lower().endswith(".stem.m4a"):
            _write_mp4(path, meta)
        elif ext == ".flac":
            _write_flac(path, meta)
        else:
            # WAV and anything else: tag support is weak/inconsistent — skip.
            return TagResult(False, "unsupported-format", ext)
    except Exception as ex:  # noqa: BLE001 — best-effort; report, don't crash save
        return TagResult(False, "error", f"{type(ex).__name__}: {ex}")
    return TagResult(True, "written")


# ---- ID3 (mp3 / aiff) --------------------------------------------------
def _apply_id3(id3, meta: TagMeta) -> None:
    from mutagen.id3 import (
        COMM,
        POPM,
        TALB,
        TCON,
        TDRC,
        TIT2,
        TIT3,
        TPE1,
        TPE4,
        TPUB,
        TXXX,
    )

    simple = {
        "title": TIT2,
        "artist": TPE1,
        "album": TALB,
        "genre": TCON,
        "label": TPUB,
        "remixer": TPE4,
        "mix": TIT3,
        "release_date": TDRC,
    }
    for field, frame in simple.items():
        if field in meta:
            val = _clean(meta[field])
            id3.delall(frame.__name__)
            if val is not None:
                id3.add(frame(encoding=3, text=[val]))
    if "producer" in meta:
        id3.delall("TXXX:PRODUCER")
        val = _clean(meta["producer"])
        if val is not None:
            id3.add(TXXX(encoding=3, desc="PRODUCER", text=[val]))
    if "comment" in meta:
        id3.delall("COMM")
        val = _clean(meta["comment"])
        if val is not None:
            id3.add(COMM(encoding=3, lang="eng", desc="", text=[val]))
    if "rating" in meta:
        id3.delall("POPM")
        stars = int(meta["rating"] or 0)
        if stars > 0:
            id3.add(
                POPM(
                    email="traktor@native-instruments.de",
                    rating=max(0, min(5, stars)) * 51,
                    count=0,
                )
            )


def _write_id3(path: Path, meta: TagMeta, id3_only: bool) -> None:
    from mutagen.id3 import ID3, ID3NoHeaderError

    try:
        id3 = ID3(path)
    except ID3NoHeaderError:
        id3 = ID3()
    _apply_id3(id3, meta)
    id3.save(path)


def _write_aiff(path: Path, meta: TagMeta) -> None:
    from mutagen.aiff import AIFF

    audio = AIFF(path)
    if audio.tags is None:
        audio.add_tags()
    _apply_id3(audio.tags, meta)
    audio.save()


# ---- MP4 (m4a / stem) --------------------------------------------------
def _write_mp4(path: Path, meta: TagMeta) -> None:
    from mutagen.mp4 import MP4, MP4FreeForm

    audio = MP4(path)
    if audio.tags is None:
        audio.add_tags()
    t = audio.tags
    standard = {
        "title": "\xa9nam",
        "artist": "\xa9ART",
        "album": "\xa9alb",
        "genre": "\xa9gen",
        "comment": "\xa9cmt",
        "release_date": "\xa9day",
    }
    for field, atom in standard.items():
        if field in meta:
            val = _clean(meta[field])
            if val is None:
                t.pop(atom, None)
            else:
                t[atom] = [val]
    # No standard MP4 atoms for these — use iTunes-style freeform atoms.
    freeform = {"label": "LABEL", "remixer": "REMIXER", "producer": "PRODUCER", "mix": "MIX"}
    for field, name in freeform.items():
        if field in meta:
            key = f"----:com.apple.iTunes:{name}"
            val = _clean(meta[field])
            if val is None:
                t.pop(key, None)
            else:
                t[key] = [MP4FreeForm(val.encode("utf-8"))]
    if "rating" in meta:
        key = "----:com.apple.iTunes:RATING"
        stars = int(meta["rating"] or 0)
        if stars > 0:
            t[key] = [MP4FreeForm(str(max(0, min(5, stars)) * 51).encode("utf-8"))]
        else:
            t.pop(key, None)
    audio.save()


# ---- FLAC --------------------------------------------------------------
def _write_flac(path: Path, meta: TagMeta) -> None:
    from mutagen.flac import FLAC

    audio = FLAC(path)
    mapping = {
        "title": "title",
        "artist": "artist",
        "album": "album",
        "genre": "genre",
        "label": "label",
        "remixer": "remixer",
        "producer": "producer",
        "comment": "comment",
        "mix": "mixname",
        "release_date": "date",
    }
    for field, vc in mapping.items():
        if field in meta:
            val = _clean(meta[field])
            if val is None:
                audio.pop(vc, None)
            else:
                audio[vc] = [val]
    if "rating" in meta:
        stars = int(meta["rating"] or 0)
        if stars > 0:
            audio["rating"] = [str(max(0, min(5, stars)) * 51)]
        else:
            audio.pop("rating", None)
    audio.save()
