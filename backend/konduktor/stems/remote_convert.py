"""Convert to Stems on a library held by a Konduktor server.

The same batch as `convert.py`, split where the machines split:

  * **The server plans** — where each stem file goes, what is skipped and why,
    whether its disk has room — because the files and the library are there.
  * **This computer separates.** The server never decodes audio, so each source
    is downloaded into the shared cache (`adapters/remote/cache.py`), separated
    and encoded here with the local engine, verified, and UPLOADED. The server
    keeps each upload as a verified leftover until the end step, so a batch
    interrupted by a takeover loses no separation: the next run reuses it.
  * **The server swaps.** One end step there — publish, park the original
    (Replace), swap every entry at once — under its own mutation lock, with its
    own ledger, which its Save commits and its Discard restores.

Cancel deletes what this run uploaded, as a local cancel deletes what it wrote.
"""
from __future__ import annotations

import contextlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..core import stem_file as sf
from ..core.adapter import AdapterError
from ..importer import SPACE_HEADROOM
from . import convert
from . import engine_manager as em
from .engine_process import EngineCancelled, EngineProcess, keep_awake

#: Before this computer has measured a download, a conservative LAN speed.
DEFAULT_SPEED = 20 * 1024 ** 2  # bytes / s


def options_body(opts: convert.Options) -> dict:
    return {
        "mode": opts.mode,
        "destination": str(opts.destination) if opts.destination else None,
        "collection": opts.collection,
        "playlist_id": opts.playlist_id,
        "new_playlist": opts.new_playlist,
    }


@dataclass
class RemotePlan:
    server: dict
    items: list[dict] = field(default_factory=list)
    blocked: str | None = None
    #: Bytes to download (sources this computer has no copy of) and upload.
    download: int = 0
    upload: int = 0
    speed: float | None = None
    space: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        out = dict(self.server)
        out.pop("items", None)
        out["blocked"] = self.blocked
        out["space"] = self.space
        out["transfer"] = {"download": self.download, "upload": self.upload,
                           "speed": self.speed or DEFAULT_SPEED, "measured": self.speed is not None}
        return out


def plan(adapter, track_ids: list[str], opts: convert.Options) -> RemotePlan:
    """The server's plan, plus what only this computer can answer: how much it
    must download and upload, and whether its cache and work folder have room."""
    from ..importer import free_bytes

    try:
        server = adapter.stems_plan({"track_ids": list(track_ids), **options_body(opts)})
    except AdapterError as ex:
        raise convert.ConvertError(str(ex))
    items = list(server.get("items") or [])
    result = RemotePlan(server=server, items=items, blocked=server.get("blocked"))
    to_separate = [i for i in items if i.get("reuse") is None]
    # Server volumes, named as the server's.
    result.space = [{**e, "volume": f"the server ({e.get('volume')})"} for e in server.get("space") or []]
    result.download = sum(int(i.get("source_size") or 0) for i in to_separate
                          if adapter.cached_audio(i["track_id"]) is None)
    result.upload = int(server.get("bytes") or 0)
    result.speed = adapter.cache.speed()
    if to_separate:
        biggest = max(float(i.get("seconds") or 0) for i in to_separate) * convert._WORK_BYTES_PER_SECOND
        work = convert.work_root()
        work_free = free_bytes(work)
        cache_usage = adapter.cache.usage()
        # Downloads land in the cache and the stem file is written in the work
        # folder before it is uploaded — both on THIS computer's disk.
        need_here = biggest + max((int(i.get("source_size") or 0) for i in to_separate), default=0)
        result.space.append({"volume": "this computer", "folder": str(work), "bytes": int(need_here),
                             "free": work_free})
        if result.blocked is None and work_free is not None and need_here + SPACE_HEADROOM > work_free:
            result.blocked = (f"Not enough space on this computer: about {need_here / 1e9:.1f} GB is needed "
                              f"while a track is downloaded and converted, {work_free / 1e9:.1f} GB is free")
        if result.blocked is None and cache_usage["free"] and result.download > cache_usage["free"]:
            result.blocked = "Not enough space on this computer for the downloads"
    return result


def run(handle, adapter, planned: RemotePlan, opts: convert.Options, *, still_current,
        manager: em.EngineManager | None = None) -> dict:
    mgr = manager or em.default_manager()
    result = {"converted": [], "renamed": {}, "added": {}, "skipped": list(planned.server.get("skipped") or []),
              "failed": [], "clamped": {}, "cancelled": False}
    items = planned.items
    handle.progress(done=0, total=len(items), unit="tracks", message="Starting the stem engine…")
    work = convert.work_root() / f"remote-batch-{id(handle):x}"
    work.mkdir(parents=True, exist_ok=True)
    staged: list[dict] = []
    uploaded: list[str] = []  # targets THIS run uploaded (a cancel deletes them)
    if any(i.get("reuse") is None for i in items) and not mgr.ready():
        raise convert.ConvertError("The stem engine is not installed")

    def checkpoint() -> None:
        if handle.cancelled:
            raise convert._Cancel()

    with contextlib.ExitStack() as stack:
        engine: EngineProcess | None = None
        try:
            for n, item in enumerate(items):
                checkpoint()
                handle.progress(done=n, message=item["title"], fraction=0.0, status="")
                if item.get("reuse") is not None:
                    staged.append(item)  # already on the server, verified
                    continue
                size = max(int(item.get("source_size") or 0), 1)
                got = 0

                def downloaded(k: int) -> None:
                    nonlocal got
                    got += k
                    checkpoint()
                    f = min(got / size, 1.0) * 0.05
                    handle.progress(fraction=f, status=f"downloading {f * 100:.0f} %")

                local = adapter.audio_path(item["track_id"], progress=downloaded)
                if local is None:
                    result["failed"].append({"track_id": item["track_id"], "title": item["title"],
                                             "reason": "the file is missing on the server"})
                    continue
                if engine is None:
                    handle.progress(status="starting…")
                    engine = stack.enter_context(EngineProcess(mgr.executable(), mgr.weights_dir(), device=opts.device))
                    stack.enter_context(keep_awake(engine.proc.pid))

                def on_progress(f: float) -> None:
                    frac = 0.05 + f * 0.80
                    handle.progress(fraction=frac, status=f"separating {frac * 100:.0f} %")

                def separate(pcm):
                    handle.progress(fraction=0.05, status="separating 5 %")
                    stems = engine.separate(pcm, work, on_progress=on_progress, cancelled=lambda: handle.cancelled)
                    handle.progress(fraction=0.85, status="encoding 85 %")
                    return stems

                partial = work / f"{n}.stem.m4a"
                try:
                    written = sf.build(local, partial, separate, checkpoint=checkpoint)
                except (convert._Cancel, EngineCancelled):
                    partial.unlink(missing_ok=True)
                    raise
                except Exception as ex:  # noqa: BLE001 — one track's failure is reported, not fatal
                    partial.unlink(missing_ok=True)
                    result["failed"].append({"track_id": item["track_id"], "title": item["title"],
                                             "reason": str(ex) or type(ex).__name__})
                    if isinstance(ex, em.EngineError) and engine.proc.poll() is not None:
                        engine = None
                    continue
                sent = 0

                def uploading(k: int) -> None:
                    nonlocal sent
                    sent += k
                    checkpoint()
                    f = 0.9 + min(sent / max(written.size, 1), 1.0) * 0.1
                    handle.progress(fraction=f, status=f"uploading {f * 100:.0f} %")

                try:
                    adapter.stage_stem(item["track_id"], item["target"], partial, bit_rate=written.bit_rate,
                                       duration=written.duration, progress=uploading)
                finally:
                    partial.unlink(missing_ok=True)
                uploaded.append(item["target"])
                staged.append(item)
        except (convert._Cancel, EngineCancelled):
            # Cancel = delete what this run uploaded; nothing else was touched.
            # (If another computer took over, this is refused — and the uploads
            # stay as verified leftovers the next run reuses, as decided.)
            with contextlib.suppress(AdapterError):
                if uploaded:
                    adapter.abandon_stems(uploaded)
            result["cancelled"] = True
            return result
        finally:
            shutil.rmtree(work, ignore_errors=True)

    if not staged:
        return result
    if not still_current():
        with contextlib.suppress(AdapterError):
            adapter.abandon_stems(uploaded)
        raise convert.ConvertError("The library changed while converting; nothing was swapped in")
    handle.progress(done=len(items), message="Swapping the converted tracks in on the server…", status="")
    swapped = adapter.publish_stems([{"track_id": i["track_id"], "target": i["target"]} for i in staged],
                                    options_body(opts))
    for key in ("converted", "renamed", "added", "clamped"):
        if key in swapped:
            result[key] = swapped[key]
    result["failed"] += swapped.get("failed") or []
    if swapped.get("playlist_missing"):
        result["playlist_missing"] = True
    return result
