"""The Convert to Stems batch: plan it, run it, swap it in.

**The collection is untouched while tracks convert** — for hours, on a big batch
— so edits made meanwhile carry over, and Cancel only deletes new files. Each
track becomes a verified `<target>.konduktor-partial`. Then one short END STEP,
under the app's mutation lock, publishes each file (a rename that refuses to
overwrite), parks each original in Replace mode, and swaps every entry at once
(`adapter.apply_stem_swaps`). From there the swap is an unsaved edit that Save
commits and Discard undoes (`pending.py`).

Skipped and reported, never failed (decided): a file that is already a stem
container (by its CONTENTS — the collection's `<STEMS>` element is on some
plain MP3s), a missing file, a target that already exists, two sources that
would write one target, a target the collection already names. A track that
fails keeps its original and is reported; the rest carry on.
"""
from __future__ import annotations

import contextlib
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import paths
from ..core import stem_file as sf
from ..core.adapter import AdapterError, StemSwap
from ..core.pathmap import common_dir_prefix
from ..importer import SPACE_HEADROOM, free_bytes
from . import engine_manager as em
from .engine_process import EngineCancelled, EngineProcess, keep_awake
from .pending import Pending, _replace_no_overwrite, _retrying, parked_name, partial_name, source_facts

_LOSSLESS = (".wav", ".aif", ".aiff", ".flac")
#: Five AAC streams at up to 320 kbps plus container overhead.
_BYTES_PER_SECOND = {256_000: 5 * 256_000 / 8 * 1.03, 320_000: 5 * 320_000 / 8 * 1.03}
#: Raw float32 stereo, mix + four stems, while a track is being separated.
_WORK_BYTES_PER_SECOND = 5 * 2 * 4 * sf.SR


#: Shares of ONE track's conversion time, for the status bar's within-track
#: progress (measured on the M3 Pro: decode ~1 s, separate ~50 s, encode +
#: verify ~4 s for a 4-minute track).
_DECODE_SHARE = 0.03
_SEPARATE_SHARE = 0.90


class ConvertError(Exception):
    """User-facing; refuses the whole batch."""


class _Cancel(Exception):
    pass


@dataclass
class Options:
    mode: str  # "replace" | "destination"
    destination: Path | None = None
    collection: str = "repoint"  # "repoint" | "add"
    playlist_id: str | None = None  # add mode: an existing playlist
    new_playlist: str | None = None  # add mode: create one with this name
    device: str = "auto"


@dataclass
class Planned:
    track_id: str
    title: str
    source: Path
    target: Path
    seconds: float
    reuse: dict | None = None


@dataclass
class Plan:
    items: list[Planned] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    space: list[dict] = field(default_factory=list)
    blocked: str | None = None

    def as_dict(self) -> dict:
        return {"convert": [{"track_id": p.track_id, "title": p.title, "target": str(p.target),
                             "reuse": p.reuse is not None} for p in self.items],
                "skipped": self.skipped, "space": self.space, "blocked": self.blocked,
                # For the dialog's estimate: audio still to SEPARATE (reused
                # leftovers cost nothing), and what the new files will take.
                "seconds": sum(p.seconds for p in self.items if p.reuse is None),
                "bytes": sum(e["bytes"] for e in self.space),
                # What Save would delete in Replace mode — the dialog says so.
                "original_bytes": sum(p.source.stat().st_size for p in self.items if p.source.exists())}


def _title(track) -> str:
    """How a track is named in progress, skip lists and reports: the track's
    name FIRST — it is what tells one track from another when the end is cut
    off in the status bar."""
    if track is None:
        return "?"
    if track.artist and track.title:
        return f"{track.title} - {track.artist}"
    return track.title or Path(track.filepath or "").name or track.id


def _mirror(sources: list[Path], destination: Path) -> dict[Path, Path]:
    """Target folder per source: the source tree under `destination`, relative
    to the sources' common folder, so same-named files cannot collide. Tracks
    on different drives share no folder; each then keeps its full path (drive
    or volume first)."""
    prefix = common_dir_prefix([str(s) for s in sources])
    out: dict[Path, Path] = {}
    for s in sources:
        if prefix:
            rel = Path(str(s.parent)[len(prefix):].lstrip("/\\"))
        else:
            parts = s.parent.parts[1:]
            head = s.drive.rstrip(":") if s.drive else ""
            rel = Path(head, *parts) if head else Path(*parts)
        out[s] = destination / rel
    return out


def _output_rate(track, source: Path) -> int:
    if source.suffix.lower() in _LOSSLESS:
        return 320_000
    bitrate = getattr(track, "bitrate", None) or 0
    return 256_000 if 0 < bitrate < 256_000 else 320_000


def plan(adapter, track_ids: list[str], opts: Options, pending: Pending | None) -> Plan:
    """What the batch would do, without touching anything."""
    result = Plan()
    if opts.mode not in ("replace", "destination"):
        raise ConvertError(f"Unknown mode {opts.mode!r}")
    if opts.mode == "replace" and opts.collection != "repoint":
        raise ConvertError("Replace mode replaces the entry; adding a second one needs a destination folder")
    if opts.mode == "destination" and not opts.destination:
        raise ConvertError("Choose a destination folder")

    in_library = {str(p) for t in adapter.tracks if (p := adapter.audio_path(t.id)) is not None}
    candidates: list[tuple[str, object, Path]] = []
    for tid in dict.fromkeys(track_ids):
        track = adapter.track(tid)
        src = adapter.audio_path(tid) if track is not None else None
        if track is None:
            result.skipped.append({"track_id": tid, "title": tid, "reason": "not in the library"})
        elif src is None or not Path(src).is_file():
            result.skipped.append({"track_id": tid, "title": _title(track), "reason": "the file is missing"})
        elif sf.is_stem_file(src):
            result.skipped.append({"track_id": tid, "title": _title(track), "reason": "already a stem file"})
        else:
            candidates.append((tid, track, Path(src)))

    folders = ({s: s.parent for _, _, s in candidates} if opts.mode == "replace"
               else _mirror([s for _, _, s in candidates], Path(opts.destination)))
    claimed: set[str] = set()
    need: dict[str, dict] = {}
    for tid, track, src in candidates:
        target = folders[src] / sf.stem_target_name(src.name)
        key = str(target).casefold()  # APFS and NTFS are case-insensitive by default
        reuse = pending.find_leftover(src) if pending is not None else None
        if reuse is not None and Path(reuse["partial"]) != partial_name(target):
            reuse = None
        if key in claimed:
            reason = "another selected track converts to the same file"
        elif target.exists():
            reason = f"a stem file already exists: {target.name}"
        elif str(target) in in_library:
            reason = f"the collection already has {target.name}"
        else:
            reason = None
        if reason:
            result.skipped.append({"track_id": tid, "title": _title(track), "reason": reason})
            continue
        claimed.add(key)
        seconds = float(getattr(track, "length", 0) or 0) or src.stat().st_size / (320_000 / 8)
        result.items.append(Planned(tid, _title(track), src, target, seconds, reuse))
        vol = _volume_key(target)
        entry = need.setdefault(vol, {"folder": str(target.parent), "bytes": 0})
        entry["bytes"] += int(seconds * _BYTES_PER_SECOND[_output_rate(track, src)]) if reuse is None else 0

    # Both copies coexist until Save, so every target volume needs the stem files'
    # room on top of what is there; the work folder needs the largest track's.
    for vol, entry in need.items():
        free = free_bytes(Path(entry["folder"]))
        entry.update(volume=vol, free=free)
        result.space.append(entry)
        if free is not None and entry["bytes"] + SPACE_HEADROOM > free:
            result.blocked = (f"Not enough space on {vol}: the stem files need about "
                              f"{entry['bytes'] / 1e9:.1f} GB and {free / 1e9:.1f} GB is free")
    if result.items and result.blocked is None:
        work = work_root()
        biggest = max(p.seconds for p in result.items) * _WORK_BYTES_PER_SECOND
        free = free_bytes(work)
        if free is not None and biggest + SPACE_HEADROOM > free:
            result.blocked = (f"Not enough space for working files: about {biggest / 1e9:.1f} GB is "
                              f"needed while a track converts, {free / 1e9:.1f} GB is free")
    return result


def _volume_key(path: Path) -> str:
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    if sys.platform == "win32":
        return (p.drive or str(p))
    parts = p.parts
    return f"/{parts[1]}/{parts[2]}" if len(parts) > 2 and parts[1] == "Volumes" else "this computer"


def work_root() -> Path:
    root = paths.app_data_dir() / "stems" / "work"
    root.mkdir(parents=True, exist_ok=True)
    return root


def run(handle, adapter, planned: Plan, opts: Options, pending: Pending, *,
        mutation, still_current: Callable[[], bool], manager: em.EngineManager | None = None) -> dict:
    """The job. Returns the result dict; the job's state says cancelled/done."""
    mgr = manager or em.default_manager()
    result = {"converted": [], "renamed": {}, "added": {}, "skipped": list(planned.skipped),
              "failed": [], "clamped": {}, "cancelled": False}
    items = planned.items
    handle.progress(done=0, total=len(items), unit="tracks", message="Starting the stem engine…")
    work = work_root() / f"batch-{id(handle):x}"
    finished: list[tuple[Planned, dict, sf.Written]] = []
    needs_engine = any(p.reuse is None for p in items)
    if needs_engine and not mgr.ready():
        raise ConvertError("The stem engine is not installed")

    def checkpoint() -> None:
        if handle.cancelled:
            raise _Cancel()

    with contextlib.ExitStack() as stack:
        engine: EngineProcess | None = None
        try:
            for i, item in enumerate(items):
                checkpoint()
                handle.progress(done=i, message=item.title, fraction=0.0, status="")
                partial = partial_name(item.target)
                if item.reuse is not None:
                    written = sf.Written(path=partial, **{k: item.reuse["written"][k] for k in ("bit_rate", "duration", "size")},
                                         tags={})
                    pitem = pending.add({"track_id": item.track_id, "mode": opts.collection, "source": str(item.source),
                                         "stem": str(item.target), "state": "converted", "facts": source_facts(item.source),
                                         "written": item.reuse["written"]})
                    pending.forget_leftover(item.reuse, delete=False)
                    finished.append((item, pitem, written))
                    continue
                if engine is None:
                    handle.progress(status="starting…")
                    engine = stack.enter_context(EngineProcess(mgr.executable(), mgr.weights_dir(), device=opts.device))
                    stack.enter_context(keep_awake(engine.proc.pid))
                pitem = pending.add({"track_id": item.track_id, "mode": opts.collection, "source": str(item.source),
                                     "stem": str(item.target), "state": "converting", "facts": source_facts(item.source)})
                item.target.parent.mkdir(parents=True, exist_ok=True)

                # Of a TRACK's time, separation is nearly all (~90 % on MPS);
                # decoding before it and encoding + verifying after share the rest.
                def on_progress(f: float) -> None:
                    frac = _DECODE_SHARE + f * (_SEPARATE_SHARE)
                    handle.progress(fraction=frac, status=f"separating {frac * 100:.0f} %")

                def separate(pcm):
                    handle.progress(fraction=_DECODE_SHARE, status=f"separating {_DECODE_SHARE * 100:.0f} %")
                    stems = engine.separate(pcm, work, on_progress=on_progress, cancelled=lambda: handle.cancelled)
                    done_at = _DECODE_SHARE + _SEPARATE_SHARE
                    handle.progress(fraction=done_at, status=f"encoding {done_at * 100:.0f} %")
                    return stems

                handle.progress(status="decoding 0 %")
                try:
                    written = sf.build(item.source, partial, separate, checkpoint=checkpoint)
                except (_Cancel, EngineCancelled):
                    partial.unlink(missing_ok=True)
                    pending.drop([pitem])
                    raise
                except Exception as ex:  # noqa: BLE001 — one track's failure is reported, not fatal
                    partial.unlink(missing_ok=True)
                    pending.drop([pitem])
                    result["failed"].append({"track_id": item.track_id, "title": item.title, "reason": str(ex) or type(ex).__name__})
                    if isinstance(ex, em.EngineError) and engine.proc.poll() is not None:
                        engine = None  # it died: the next track starts a fresh one
                    continue
                pending.update(pitem, state="converted",
                               written={"bit_rate": written.bit_rate, "duration": written.duration, "size": written.size})
                finished.append((item, pitem, written))
        except (_Cancel, EngineCancelled):
            # Cancel = delete what this run made; nothing else was touched.
            for _item, pitem, written in finished:
                if pitem.get("state") == "converted":
                    Path(written.path).unlink(missing_ok=True)
            pending.drop([p for _, p, _ in finished])
            result["cancelled"] = True
            return result
        finally:
            shutil.rmtree(work, ignore_errors=True)

    handle.progress(done=len(items), message="Swapping the converted tracks into the collection…", status="")
    with mutation:
        if not still_current():
            for _item, pitem, written in finished:
                Path(written.path).unlink(missing_ok=True)
            pending.drop([p for _, p, _ in finished])
            raise ConvertError("The library changed while converting; nothing was swapped in")
        _end_step(adapter, finished, opts, pending, result)
    return result


def _end_step(adapter, finished, opts: Options, pending: Pending, result: dict) -> None:
    swaps: list[StemSwap] = []
    staged: list[tuple[Planned, dict]] = []
    for item, pitem, written in finished:
        partial = Path(written.path)
        try:
            _replace_no_overwrite(partial, item.target)
        except FileExistsError:
            partial.unlink(missing_ok=True)
            pending.drop([pitem])
            result["failed"].append({"track_id": item.track_id, "title": item.title,
                                     "reason": f"a file appeared at {item.target.name} while converting"})
            continue
        pending.update(pitem, state="published")
        original_now = item.source
        if opts.mode == "replace":
            parked = parked_name(item.source)
            try:
                _retrying(lambda: _replace_no_overwrite(item.source, parked))
            except OSError as ex:
                item.target.unlink(missing_ok=True)
                pending.drop([pitem])
                result["failed"].append({"track_id": item.track_id, "title": item.title,
                                         "reason": f"the original could not be moved aside (in use?): {ex}"})
                continue
            _hide(parked)
            pending.update(pitem, state="parked", parked=str(parked))
            original_now = parked
        swaps.append(StemSwap(item.track_id, item.target, opts.collection, original_now,
                              written.bit_rate, written.duration, written.size))
        staged.append((item, pitem))
    if not swaps:
        return
    playlist = opts.playlist_id
    try:
        if opts.collection == "add" and opts.new_playlist:
            playlist = adapter.create_playlist(opts.new_playlist)
        elif playlist is not None and adapter.playlist_entries(playlist) is None:
            result["playlist_missing"] = True  # deleted while converting: the tracks are still added
            playlist = None
        res = adapter.apply_stem_swaps(swaps, add_to_playlist=playlist if opts.collection == "add" else None)
    except AdapterError as ex:
        # Nothing was swapped (the command validates everything first): undo the files.
        for item, pitem in staged:
            pending._undo(pitem, keep_leftover=False)
            pending.drop([pitem])
            result["failed"].append({"track_id": item.track_id, "title": item.title, "reason": str(ex)})
        return
    for item, pitem in staged:
        new_id = res.renamed[item.track_id]
        pending.update(pitem, state="swapped", new_id=new_id)
        result["converted"].append({"track_id": item.track_id, "new_id": new_id, "title": item.title})
        (result["renamed"] if opts.collection == "repoint" else result["added"])[item.track_id] = new_id
    result["clamped"] = res.clamped


def _hide(path: Path) -> None:
    """Windows ignores dot-names; set the hidden attribute so a parked original
    does not show in Explorer (or a naive folder scan)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x02)
    except Exception:  # noqa: BLE001
        pass
