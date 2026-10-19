"""Normalize on a library held by a Konduktor server (decided 2026-10-19: the
same transfer Convert to Stems uses).

The batch of `normalize.py`, split where the machines split:

  * **The server plans** — from its own loudness store, with its own ledger
    (a track awaiting Save is skipped there) — and says per track how big the
    source is, the platform's analysis lead, and whether a verified upload at
    this very gain is already there (`staged`: a takeover interrupted the
    run that made it, and this one reuses it).
  * **This computer rewrites.** The server never decodes audio, so each source
    is downloaded into the shared cache, measured if the server has no
    measurement (stored back on the server), rewritten by `core.level_edit`,
    VERIFIED, measured for its drawn waveform where the platform keeps one,
    and UPLOADED.
  * **The server swaps.** One end step there (`/v1/gain/publish`) — park the
    original, the upload under its name, the library's size / waveforms /
    auto gain — under its mutation lock, with its own ledger, which its Save
    commits and its Discard undoes.

Cancel deletes what this run uploaded; a takeover cannot, so those uploads
stay and the next run reuses them.
"""
from __future__ import annotations

import contextlib
import shutil
from pathlib import Path

from ..core.adapter import AdapterError
from .normalize import _WHY, NormalizeError, Options

#: Before this computer has measured a download, a conservative LAN speed.
DEFAULT_SPEED = 20 * 1024 ** 2  # bytes / s


def options_body(opts: Options) -> dict:
    return {"measure": opts.measure, "target": opts.target, "ceiling": opts.ceiling}


def plan(adapter, track_ids, opts: Options) -> dict:
    """The server's plan, plus what only this computer can answer: how much
    must cross the network (a source already cached here is not downloaded)."""
    try:
        out = adapter.normalize_plan({"track_ids": list(track_ids), **options_body(opts)})
    except AdapterError as ex:
        raise NormalizeError(str(ex))
    work = [r for r in out["rows"] if _needs_work(r) and not r.get("staged")]
    download = sum(int(r.get("source_size") or 0) for r in work if adapter.cached_audio(r["track_id"]) is None)
    upload = sum(int(r.get("source_size") or 0) for r in work)  # a level change keeps (about) the size
    speed = adapter.cache.speed()
    out["transfer"] = {"download": download, "upload": upload, "speed": speed or DEFAULT_SPEED,
                       "measured": speed is not None}
    return out


def _needs_work(row: dict) -> bool:
    return not row.get("skip") and (bool(row.get("gain_db")) or row.get("outcome") == "unmeasured")


def run(handle, adapter, track_ids, opts: Options, *, still_current) -> dict:
    from ..adapters.remote.services import RemoteLoudnessStore
    from ..core import level_edit, loudness
    from ..core.adapter import PreparedAnalysis
    from ..core.level_plan import plan_gain
    from .. import remote_protocol as rp
    from ..stems import convert

    planned = plan(adapter, track_ids, opts)
    result = {"normalised": [], "skipped": [], "failed": [], "counts": dict(planned.get("counts") or {}),
              "cancelled": False}
    rows = planned["rows"]
    store = RemoteLoudnessStore(adapter.client, adapter.session)
    body = options_body(opts)
    staged: list[str] = []
    uploaded: list[str] = []  # what THIS run uploaded (a cancel deletes it)
    work = convert.work_root() / f"remote-normalize-{id(handle):x}"
    work.mkdir(parents=True, exist_ok=True)
    handle.progress(done=0, total=len(rows), unit="tracks")

    def skip(row: dict, reason: str) -> None:
        result["skipped"].append({"track_id": row["track_id"], "title": row["title"], "reason": reason})

    def fail(row: dict, reason: str) -> None:
        result["failed"].append({"track_id": row["track_id"], "title": row["title"], "reason": reason})

    def checkpoint() -> None:
        if handle.cancelled:
            raise _Cancel()

    try:
        for n, row in enumerate(rows):
            checkpoint()
            handle.progress(done=n, message=row["title"], fraction=0.0, status="")
            if row.get("skip"):
                skip(row, row["skip"])
                continue
            if not _needs_work(row):
                skip(row, _WHY.get(row.get("outcome"), "nothing to change"))
                continue
            if row.get("staged"):
                staged.append(row["track_id"])  # already on the server, verified
                continue
            size = max(int(row.get("source_size") or 0), 1)
            got = 0

            def downloaded(k: int) -> None:
                nonlocal got
                got += k
                checkpoint()
                f = min(got / size, 1.0) * 0.3
                handle.progress(fraction=f, status=f"downloading {f / 0.3 * 100:.0f} %")

            local = adapter.audio_path(row["track_id"], progress=downloaded)
            if local is None:
                fail(row, "the file is missing on the server")
                continue
            gain = float(row.get("gain_db") or 0.0)
            if row.get("outcome") == "unmeasured":
                handle.progress(fraction=0.3, status="measuring")
                try:
                    values = loudness.measure_file(local)
                except Exception as ex:  # noqa: BLE001 — one track's failure is reported, not fatal
                    fail(row, f"Could not measure: {ex}")
                    continue
                if values is None:
                    skip(row, "nothing to measure (silent or under 3 s)")
                    continue
                facts = adapter.audio_facts([row["track_id"]]).get(row["track_id"])
                if facts is not None:
                    store.put(facts, values)  # the server's store: its stage checks against it
                step = plan_gain(values, Path(local).suffix, measure=opts.measure, target=opts.target,
                                 ceiling=opts.ceiling)
                result["counts"][step.outcome] = result["counts"].get(step.outcome, 0) + 1
                if not step.writes:
                    skip(row, _WHY.get(step.outcome, "nothing to change"))
                    continue
                gain = step.gain_db
            partial = work / f"{n}{Path(local).suffix}"
            handle.progress(fraction=0.4, status=f"{gain:+.1f} dB")
            try:
                level_edit.apply(local, partial, gain)
                checkpoint()
                handle.progress(fraction=0.5, status="verifying")
                level_edit.verify(local, partial, gain)
                packed = None
                if row.get("lead") is not None:  # the platform draws waveforms from its own files
                    from ..core import waveform

                    handle.progress(fraction=0.7, status="waveform")
                    measured = waveform.analyse(partial, lead=float(row["lead"]))
                    packed = rp.pack_analysis(PreparedAnalysis(measured=measured)) if measured else None
                sent = 0
                total = max(partial.stat().st_size, 1)

                def uploading(k: int) -> None:
                    nonlocal sent
                    sent += k
                    checkpoint()
                    f = 0.8 + min(sent / total, 1.0) * 0.2
                    handle.progress(fraction=f, status=f"uploading {(f - 0.8) / 0.2 * 100:.0f} %")

                adapter.stage_gain(row["track_id"], partial, gain_db=gain, options=body, waveform=packed,
                                   progress=uploading)
            except _Cancel:
                raise
            except AdapterError:
                raise
            except Exception as ex:  # noqa: BLE001 — one track's failure is reported, not fatal
                fail(row, str(ex) or type(ex).__name__)
                continue
            finally:
                partial.unlink(missing_ok=True)
            uploaded.append(row["track_id"])
            staged.append(row["track_id"])
    except _Cancel:
        with contextlib.suppress(AdapterError):
            if uploaded:
                adapter.abandon_gain(uploaded)
        result["cancelled"] = True
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if not staged:
        return result
    if not still_current():
        with contextlib.suppress(AdapterError):
            adapter.abandon_gain(uploaded)
        raise NormalizeError("The library changed while normalizing; nothing was changed")
    handle.progress(done=len(rows), message="Putting the new levels in place on the server…", status="")
    published = adapter.publish_gain(staged)
    result["normalised"] = list(published.get("normalised") or [])
    result["failed"] += published.get("failed") or []
    return result


class _Cancel(Exception):
    pass
