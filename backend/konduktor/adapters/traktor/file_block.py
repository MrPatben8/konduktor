"""Traktor's own block INSIDE an audio file, and removing it from a copy.

Traktor writes a binary block of its analysis into every file it tags — the
same `DMRT` chunk tree in each format, stored where the format allows:

  - MP4 (`.m4a`, stem files): the `ilst` atom `NITR`
  - ID3 (MP3, AIFF, WAV): a `PRIV` frame owned by `TRAKTOR4`
  - Vorbis comments (FLAC, Ogg): the field `traktor4`

It holds a BPM (`HBPM`), the cues (`CUEP`), the audio id, transients, tags.
Measured in Traktor 4.5 (2026-10-24, 2MUCH / Alone (Vip)): on an entry it did
not write itself — no MODIFIED_DATE, as every entry Konduktor creates — Traktor
re-reads the file and takes the block's BPM over the collection's, LOCK or not,
keeping the anchor. A grid fixed after Traktor last wrote the block (in
Konduktor, or in Traktor without a tag write) is reverted on every export.

Giving the entry a MODIFIED_DATE later than the file stops that, but Traktor
then skips the file altogether, cover art included. Giving it the block's
AUDIO_ID changes nothing. Removing the block from the copy does both: Traktor
imports the file as new — art, file facts and analysis from the file; BPM,
grid, lock and cues from the entry — and writes a fresh, correct block itself
when the track is first loaded. A stale `TBPM`/`tmpo` tag is ignored.

Only ever applied to a COPY Konduktor wrote. A file the user's library
references in place is the user's, and is never touched.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

#: The ID3 PRIV owner and the Vorbis field name Traktor writes the block under.
_PRIV_OWNER = "TRAKTOR4"
_VORBIS_FIELD = "traktor4"
#: The MP4 atom. mutagen keeps it among the atoms it could not parse — or, in
#: some files, parses it into an ordinary key; both are checked.
_MP4_ATOM = "NITR"


def has_block(path: Path) -> bool:
    """Whether the file carries Traktor's block. False for anything unreadable."""
    audio = _open(path)
    return audio is not None and bool(_remove(audio, dry_run=True))


def strip(path: Path) -> bool:
    """Remove Traktor's block from `path`, in place; True if it held one.

    Everything else in the file is kept: other tags, cover, the stem box, the
    audio. A file without a block is not rewritten at all. A file that cannot
    be read or written is left as it is — a copy that still carries the block
    is the old behaviour, never a failed export — and the reason is logged.
    """
    audio = _open(path)
    if audio is None or not _remove(audio, dry_run=False):
        return False
    try:
        version = getattr(audio.tags, "version", None)
        if version is not None and _is_id3(audio):
            # Keep the file's own ID3 version: nothing else in it is changing.
            audio.save(v2_version=3 if version[1] == 3 else 4)
        else:
            audio.save()
    except Exception as ex:  # noqa: BLE001 — see the docstring
        log.warning("could not remove Traktor's block from %s: %s", path, ex)
        return False
    return True


def _open(path: Path):
    try:
        import mutagen

        audio = mutagen.File(path)
    except Exception as ex:  # noqa: BLE001 — an unreadable file has no block we can see
        log.debug("cannot read tags of %s: %s", path, ex)
        return None
    if audio is None or audio.tags is None:
        return None
    return audio


def _is_id3(audio) -> bool:
    from mutagen.id3 import ID3

    return isinstance(audio.tags, ID3)


def _remove(audio, *, dry_run: bool) -> int:
    """How many block entries `audio`'s tags hold, removing them unless
    `dry_run`. Never saves."""
    from mutagen.mp4 import MP4Tags

    tags = audio.tags
    found = 0
    if isinstance(tags, MP4Tags):
        failed = getattr(tags, "_failed_atoms", {})
        if _MP4_ATOM in failed:
            found += 1
            if not dry_run:
                del failed[_MP4_ATOM]
        if _MP4_ATOM in tags:
            found += 1
            if not dry_run:
                del tags[_MP4_ATOM]
    elif _is_id3(audio):
        keys = [k for k, frame in tags.items()
                if k.startswith("PRIV:") and getattr(frame, "owner", None) == _PRIV_OWNER]
        found += len(keys)
        if not dry_run:
            for k in keys:
                del tags[k]
    elif hasattr(tags, "keys"):  # Vorbis comments: case-insensitive field names
        keys = [k for k in tags.keys() if k.lower() == _VORBIS_FIELD]
        found += len(keys)
        if not dry_run:
            for k in keys:
                del tags[k]
    return found
