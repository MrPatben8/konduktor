"""Normalize: change tracks' audio level in place, as an unsaved change.

Decided 2026-10-19 (the gain-normalisation discussion). Per track the gain is
`core.level_plan.plan_gain` (the same function the dialog's preview counts
with), the file is rewritten by `core.level_edit` into
`<name>.konduktor-partial` beside it and decode-VERIFIED, and nothing the
library or the user sees changes until ONE short end step under the app's
mutation lock:

  1. the original is PARKED (`.<name>.konduktor-parked`, hidden on Windows)
     and the verified file renamed onto the original's own name — the path
     never changes, so no playlist, export set or cue needs retargeting;
  2. the library follows the new audio (`audio_changed`: its size, and its
     drawn waveforms where it stores them);
  3. the DJ app's auto gain is RECOMPUTED from the new level (decided — the
     old value would now double-apply the change; kit G1 measured that Traktor
     never re-analyses a file whose level changed);
  4. Konduktor's own loudness value is SHIFTED by the gain (exact: the edit
     scales the samples), under the new file's facts.

From there it is an ordinary unsaved change (`pending.py`, kind "gain"): Save
deletes the parked original — permanent for a lossless file, while an MP3
keeps an mp3gain undo tag — and Discard puts it back. Measuring first where a
track has no (or a stale) loudness happens inside the run.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..core import level_edit, loudness
from ..core.adapter import AudioChange
from ..core.level_plan import EXACT, GainStep, Measure, plan_gain, shifted
from ..pending import Pending, _replace_no_overwrite, _retrying, parked_name, partial_name, source_facts

log = logging.getLogger(__name__)


class NormalizeError(Exception):
    """The batch cannot run; the message is user-facing."""


@dataclass(frozen=True)
class Options:
    measure: Measure = "lufs"
    target: float = -9.0
    ceiling: float = -1.0


@dataclass
class Row:
    """One track's plan: what the preview lists and the run starts from."""

    track_id: str
    title: str
    source: Path | None
    step: GainStep | None = None
    level: float | None = None   # the measure now (None: unmeasured)
    skip: str | None = None      # a reason, when nothing can be done

    def as_dict(self) -> dict:
        out = {"track_id": self.track_id, "title": self.title, "level": self.level, "skip": self.skip}
        if self.step is not None:
            out.update(gain_db=self.step.gain_db, outcome=self.step.outcome, short_by=self.step.short_by,
                       lossless=self.source is not None and self.source.suffix.lower() in EXACT)
        return out


@dataclass
class Plan:
    rows: list[Row] = field(default_factory=list)

    def as_dict(self) -> dict:
        counts: dict[str, int] = {}
        lossless = 0
        for r in self.rows:
            key = r.skip and "skipped" or (r.step.outcome if r.step else "skipped")
            counts[key] = counts.get(key, 0) + 1
            if r.step is not None and r.step.writes and r.source.suffix.lower() in EXACT:
                lossless += 1
        writes = sum(1 for r in self.rows if r.step is not None and r.step.writes)
        unmeasured = counts.get("unmeasured", 0)
        return {"total": len(self.rows), "to_change": writes, "to_measure": unmeasured,
                "tracks_to_process": writes + unmeasured, "lossless": lossless, "counts": counts,
                "rows": [r.as_dict() for r in self.rows]}


def plan(adapter, track_ids, opts: Options, store, pending: Pending | None) -> Plan:
    """Per track: the gain `plan_gain` gives from the STORED loudness. A track
    with none (or a stale one) is `unmeasured`: the run measures it first."""
    held = pending.holds(track_ids) if pending is not None else set()
    out = Plan()
    ids = list(dict.fromkeys(track_ids))
    facts = adapter.audio_facts(ids)
    entries = store.get([f.key for f in facts.values() if f is not None])
    for tid in ids:
        track = adapter.track(tid)
        if track is None:
            continue
        title = track.title or tid
        path = adapter.audio_path(tid)
        f = facts.get(tid)
        if tid in held:
            out.rows.append(Row(tid, title, path, skip="has a stem conversion or level change awaiting Save"))
            continue
        if path is None or f is None or not Path(path).is_file():
            out.rows.append(Row(tid, title, path, skip="audio file not found (is the drive mounted?)"))
            continue
        entry = entries.get(f.key)
        values = entry.values if entry is not None and not entry.stale(f) else None
        step = plan_gain(values, Path(path).suffix, measure=opts.measure, target=opts.target, ceiling=opts.ceiling)
        level = None if values is None else (values.lufs if opts.measure == "lufs" else values.rms)
        out.rows.append(Row(tid, title, Path(path), step=step, level=level))
    return out


def run(handle, adapter, track_ids, opts: Options, *, store, pending: Pending, mutation,
        still_current: Callable[[], bool]) -> dict:
    """The job: measure where needed, rewrite + verify each file, then ONE end
    step. Returns the result dict."""
    planned = plan(adapter, track_ids, opts, store, pending)
    result = {"normalised": [], "skipped": [], "failed": [], "counts": {}, "cancelled": False}
    rows = [r for r in planned.rows]
    handle.progress(done=0, total=len(rows), unit="tracks")
    finished: list[tuple[Row, dict, object]] = []

    def skip(row: Row, reason: str) -> None:
        result["skipped"].append({"track_id": row.track_id, "title": row.title, "reason": reason})

    try:
        for i, row in enumerate(rows):
            if handle.cancelled:
                raise _Cancel()
            handle.progress(done=i, message=row.title, fraction=0.0, status="")
            if row.skip:
                skip(row, row.skip)
                continue
            source = row.source
            facts = adapter.audio_facts([row.track_id]).get(row.track_id)
            got = store.get([facts.key]).get(facts.key) if facts is not None else None
            values = got.values if got is not None and not got.stale(facts) else None
            if values is None:
                handle.progress(status="measuring")
                try:
                    values = loudness.measure_file(source)
                except Exception as ex:  # noqa: BLE001 — an undecodable file is one failure
                    result["failed"].append({"track_id": row.track_id, "title": row.title,
                                             "reason": f"Could not measure: {ex}"})
                    continue
                if values is None:
                    skip(row, "nothing to measure (silent or under 3 s)")
                    continue
                store.put(facts, values)
            step = plan_gain(values, source.suffix, measure=opts.measure, target=opts.target, ceiling=opts.ceiling)
            result["counts"][step.outcome] = result["counts"].get(step.outcome, 0) + 1
            if not step.writes:
                skip(row, _WHY[step.outcome])
                continue
            partial = partial_name(source)
            if partial.exists():
                skip(row, f"{partial.name} is in the way")
                continue
            track = adapter.track(row.track_id)
            before_wire = {"path": facts.key, "size": facts.size, "mtime_ns": facts.mtime_ns,
                           "version": loudness.VERSION, **values.as_dict()}
            pitem = pending.add({
                "kind": "gain", "track_id": row.track_id, "source": str(source), "partial": str(partial),
                "parked": str(parked_name(source)), "gain_db": step.gain_db,
                "lossless": source.suffix.lower() in EXACT, "state": "writing",
                "facts_before": source_facts(source), "loudness_before": before_wire,
                "gain_before": getattr(track, "auto_gain_db", None),
            })
            handle.progress(status=f"{step.gain_db:+.1f} dB")
            try:
                level_edit.apply(source, partial, step.gain_db)
                handle.progress(fraction=0.5, status="verifying")
                level_edit.verify(source, partial, step.gain_db)
            except Exception as ex:  # noqa: BLE001 — one track's failure is reported, not fatal
                partial.unlink(missing_ok=True)
                pending.drop([pitem])
                result["failed"].append({"track_id": row.track_id, "title": row.title, "reason": str(ex)})
                continue
            measured = None
            lead = getattr(adapter, "analysis_lead", None)
            if lead is not None:  # the platform draws waveforms from its own files
                from ..core import waveform

                handle.progress(fraction=0.8, status="waveform")
                measured = waveform.analyse(partial, lead=lead(source))
            pending.update(pitem, state="written", facts_after=source_facts(partial))
            finished.append((row, pitem, (values, step, measured)))
    except _Cancel:
        for _row, pitem, _ in finished:
            Path(pitem["partial"]).unlink(missing_ok=True)
        pending.drop([p for _, p, _ in finished])
        result["cancelled"] = True
        return result

    handle.progress(done=len(rows), message="Putting the new levels in place…", status="")
    with mutation:
        if not still_current():
            for _row, pitem, _ in finished:
                Path(pitem["partial"]).unlink(missing_ok=True)
            pending.drop([p for _, p, _ in finished])
            raise NormalizeError("The library changed while normalizing; nothing was changed")
        for row, pitem, (values, step, measured) in finished:
            try:
                _end_step(adapter, row, pitem, values, step, measured, store, pending)
                result["normalised"].append(row.track_id)
            except Exception as ex:  # noqa: BLE001 — undo that track, keep the rest
                log.warning("normalize end step failed for %s", row.track_id, exc_info=True)
                pending._undo_gain(pitem)
                pending.drop([pitem])
                result["failed"].append({"track_id": row.track_id, "title": row.title, "reason": str(ex)})
    return result


def _end_step(adapter, row: Row, pitem: dict, values, step: GainStep, measured, store, pending: Pending) -> None:
    source = Path(pitem["source"])
    parked = Path(pitem["parked"])
    _retrying(lambda: _replace_no_overwrite(source, parked))
    _hide(parked)
    pending.update(pitem, state="parked")
    _retrying(lambda: _replace_no_overwrite(Path(pitem["partial"]), source))
    pending.update(pitem, state="published")
    facts = adapter.audio_facts([row.track_id]).get(row.track_id)
    after = shifted(values, step.gain_db)
    adapter.audio_changed(row.track_id, AudioChange(gain_db=step.gain_db, size=source.stat().st_size,
                                                    measured=measured))
    if adapter.capabilities().tracks.auto_gain:
        # Recomputed from the NEW level (decided): the old value would now
        # double-apply the change.
        adapter.set_auto_gain(row.track_id, after)
    track = adapter.track(row.track_id)
    if facts is not None:
        store.put(facts, after)
    pending.update(pitem, state="swapped", gain_after=getattr(track, "auto_gain_db", None),
                   facts_after=source_facts(source))


_WHY = {
    "at_target": "already at the target",
    "hot_peak_left": "its peak is already over the ceiling",
    "capped": "no headroom under the ceiling",
    "unsupported": "this format cannot be changed losslessly",
    "unmeasured": "not measured",
}


class _Cancel(Exception):
    pass


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
