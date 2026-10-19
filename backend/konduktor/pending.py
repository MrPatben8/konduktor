"""Changes to audio FILES that are done on disk but not yet committed by a Save.

Two kinds of item share this ledger (and every hook into it — Save, Discard,
opening another library, crash recovery, the routes that must refuse while
any are pending): a Convert to Stems swap (`kind` "stem", or no kind — every
ledger written before Normalize) and a Normalize level change (`kind` "gain",
see "Gain items" below).

A Convert to Stems batch leaves, per track: a stem file, and — in Replace mode —
the original PARKED beside it (`.<name>.<ext>.konduktor-parked`, the same
folder, so the rename is atomic and nothing looks like audio). The collection
swap itself is an ordinary unsaved edit. So the files must follow what happens
to the edits:

  * **Save** commits them: parked originals are deleted (the user chose Replace,
    and was told Save deletes them), export sets follow the new ids.
  * **Discard** — or opening another library, after its confirm — undoes them:
    originals go back under their own names, the stem files are removed.
  * **A crash** leaves this ledger on disk. The next open of the library
    decides each item from the SAVED collection: if it names the stem file the
    swap was committed (finish deleting the original), otherwise it was not
    (put the original back). Verified stem files of an uncommitted item are
    kept — renamed to their `.konduktor-partial` name, so neither the user nor
    Traktor mistakes them for finished files — and a later batch converting the
    same source reuses one instead of spending minutes separating it again.

**Gain items** (Normalize, decided 2026-10-19). The level-changed file is
written beside the source as `<name>.konduktor-partial` and verified; then the
source is PARKED and the partial renamed onto the source's own name, so the
path never changes. States: writing -> written -> parked -> published ->
swapped (the library's gain/waveform edits made) -> saving -> saved. Because
the path does not change, "does the saved library name the new file" cannot
tell a crash's outcome; instead `AppState._save` brackets `adapter.save()`
with `begin_save` / `mark_saved`, so only a crash INSIDE the save (state
"saving") needs the library asked (`is_committed`, which the caller words
per platform). Unsure always means restore: the original audio is never
destroyed on a guess. After Save a lossless file's change is permanent (the
original is deleted); an MP3's stays reversible by its mp3gain undo tag.

The ledger is Konduktor's own curated state, written atomically
(`paths.write_json`), one file per library id. Every step is taken from what is
actually ON DISK, never from the recorded state alone: a crash can fall between
any two renames.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from . import paths

PARTIAL = ".konduktor-partial"
PARKED = ".konduktor-parked"


def parked_name(original: Path) -> Path:
    return original.with_name(f".{original.name}{PARKED}")


def partial_name(stem: Path) -> Path:
    return stem.with_name(stem.name + PARTIAL)


def source_facts(path: Path) -> dict | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _replace_no_overwrite(src: Path, dst: Path) -> None:
    """Rename refusing to overwrite: POSIX link+unlink (link fails if dst
    exists), Windows rename (which already refuses). A filesystem with no hard
    links (FAT / exFAT: a USB stick, which a Normalize can rewrite in place)
    falls back to check-then-rename — the only window is another program
    creating that exact name in between."""
    if os.name == "nt":
        os.rename(src, dst)
        return
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError:
        if os.path.lexists(dst):
            raise FileExistsError(dst)
        os.rename(src, dst)
        return
    os.unlink(src)


def _retrying(fn, *, attempts: int = 6, delay: float = 0.25):
    """Windows cannot rename a file another process has open (Traktor, the
    deck's stream, an antivirus scan of a file just written): retry briefly."""
    for i in range(attempts):
        try:
            return fn()
        except PermissionError:
            if os.name != "nt" or i == attempts - 1:
                raise
            time.sleep(delay * (2 ** i))


class Pending:
    """The ledger for ONE library."""

    def __init__(self, library_id: str | None) -> None:
        self.library_id = library_id
        #: Gain items the last restore/recover put back, for their loudness.
        self.restored_gain: list[dict] = []
        self._lock = threading.RLock()
        self._path = (paths.app_data_dir() / "stems" / f"pending-{library_id}.json") if library_id else None
        data = paths.read_json(self._path, None) if self._path else None
        self.items: list[dict] = (data or {}).get("items", [])
        self.leftovers: list[dict] = (data or {}).get("leftovers", [])

    # ---- persistence ----------------------------------------------------------
    def _write(self) -> None:
        if self._path is None:
            return
        if not self.items and not self.leftovers:
            self._path.unlink(missing_ok=True)
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        paths.write_json(self._path, {"items": self.items, "leftovers": self.leftovers})

    def add(self, item: dict) -> dict:
        with self._lock:
            self.items.append(item)
            self._write()
            return item

    def update(self, item: dict, **fields) -> None:
        with self._lock:
            item.update(fields)
            self._write()

    def drop(self, items: list[dict]) -> None:
        with self._lock:
            ids = {id(i) for i in items}
            self.items = [i for i in self.items if id(i) not in ids]
            self._write()

    # ---- what the rest of the app asks ----------------------------------------------
    def swapped(self) -> list[dict]:
        """Every item whose library edit is made and awaits Save (both kinds)."""
        return [i for i in self.items if i.get("state") in _AWAITING_SAVE.get(_kind(i), ())]

    def blocking(self) -> bool:
        """Changes awaiting Save or Discard — while there are any, import,
        remap, reload and history restore must refuse: each would save or drop
        them as a side effect."""
        return bool(self.swapped())

    def holds(self, track_ids) -> set[str]:
        """The given tracks that have an item of EITHER kind in flight — a
        track is converted or normalised once per Save, never both at once."""
        wanted = set(track_ids)
        return {i["track_id"] for i in self.items if i.get("track_id") in wanted}

    def summary(self) -> dict:
        waiting = self.swapped()
        stems = [i for i in waiting if _kind(i) == "stem"]
        gains = [i for i in waiting if _kind(i) == "gain"]
        parked = [Path(i["parked"]) for i in stems if i.get("parked")]
        return {"tracks": len(stems),
                "parked": len(parked),
                "bytes": sum(p.stat().st_size for p in parked if p.exists()),
                # Normalize: files whose level Save makes permanent.
                "gain_tracks": len(gains),
                "gain_lossless": sum(1 for i in gains if i.get("lossless"))}

    # ---- Save markers (gain items) ------------------------------------------------
    def begin_save(self, library_facts: dict | None) -> None:
        """Right before `adapter.save()`: from here a crash's outcome depends
        on whether the save landed, which recovery asks the library."""
        with self._lock:
            changed = False
            for item in self.items:
                if _kind(item) == "gain" and item.get("state") == "swapped":
                    item["state"], item["library_before"] = "saving", library_facts
                    changed = True
            if changed:
                self._write()

    def abort_save(self) -> None:
        """The save raised: nothing was committed, the items await Save again."""
        self._set_gain_state("saving", "swapped")

    def mark_saved(self) -> None:
        """The save returned: the library holds the new gains — commit next."""
        self._set_gain_state("saving", "saved")

    def _set_gain_state(self, frm: str, to: str) -> None:
        with self._lock:
            changed = False
            for item in self.items:
                if _kind(item) == "gain" and item.get("state") == frm:
                    item["state"] = to
                    changed = True
            if changed:
                self._write()

    # ---- leftovers (reuse after a crash) -----------------------------------------------
    def find_leftover(self, source: Path) -> dict | None:
        facts = source_facts(source)
        for lo in self.leftovers:
            if lo["source"] == str(source) and lo.get("facts") == facts and Path(lo["partial"]).is_file():
                return lo
        return None

    def forget_leftover(self, leftover: dict, *, delete: bool) -> None:
        with self._lock:
            if delete:
                Path(leftover["partial"]).unlink(missing_ok=True)
            self.leftovers = [lo for lo in self.leftovers if lo is not leftover]
            self._write()

    def _keep_as_leftover(self, item: dict) -> None:
        """A verified stem file whose swap was not committed: keep it under its
        partial name for reuse (rather than hours of separation lost)."""
        stem = Path(item["stem"])
        partial = partial_name(stem)
        try:
            if _published(item) and stem.is_file() and not partial.exists():
                os.replace(stem, partial)
        except OSError:
            return
        if partial.is_file() and item.get("written"):
            self.leftovers.append({"source": item["source"], "facts": item.get("facts"),
                                   "partial": str(partial), "written": item["written"]})

    # ---- Save / Discard / recovery ---------------------------------------------------
    def commit(self) -> dict[str, str]:
        """After a successful Save: delete what the swap replaced. Returns the
        repoint renames so the caller retargets export sets."""
        renames: dict[str, str] = {}
        with self._lock:
            for item in self.swapped():
                if _kind(item) == "gain":
                    if item.get("state") == "saved":
                        self._commit_gain(item)
                    continue
                if item.get("parked"):
                    try:
                        Path(item["parked"]).unlink(missing_ok=True)
                    except OSError:
                        pass  # a file still open on Windows: recovery tries again
                if item.get("mode") == "repoint" and item.get("new_id"):
                    renames[item["track_id"]] = item["new_id"]
                item["state"] = "committed"
            self.items = [i for i in self.items if i.get("state") != "committed"
                          or (i.get("parked") and Path(i["parked"]).exists())]
            self._write()
        return renames

    def _commit_gain(self, item: dict) -> None:
        """The new level is saved: the parked original goes — only if the
        normalised file really is in place, never leaving the path empty."""
        parked = Path(item["parked"]) if item.get("parked") else None
        if parked is not None and Path(item["source"]).is_file():
            try:
                parked.unlink(missing_ok=True)
            except OSError:
                return  # open on Windows: stays "saved", recovery tries again
        item["state"] = "committed"

    def restore(self) -> int:
        """Undo every uncommitted conversion: originals back, stem files gone.
        Returns how many originals were put back."""
        restored = 0
        self.restored_gain = []
        with self._lock:
            for item in list(self.items):
                if _kind(item) == "gain":
                    restored += self._undo_gain(item)
                else:
                    restored += self._undo(item, keep_leftover=False)
            self.items = []
            self._write()
        return restored

    def recover(self, is_committed) -> dict:
        """At open, after a crash: decide each leftover item from the SAVED
        collection (`is_committed(item)` — does it name the stem file?)."""
        report = {"committed": 0, "restored": 0, "kept": 0}
        self.restored_gain = []
        with self._lock:
            before = len(self.leftovers)
            for item in list(self.items):
                if _kind(item) == "gain":
                    state = item.get("state")
                    if state == "saved" or (state == "saving" and is_committed(item)):
                        self._commit_gain(item)
                        report["committed"] += 1
                    else:
                        report["restored"] += self._undo_gain(item)
                    continue
                if item.get("new_id") and is_committed(item):
                    if item.get("parked"):
                        Path(item["parked"]).unlink(missing_ok=True)
                    report["committed"] += 1
                else:
                    report["restored"] += self._undo(item, keep_leftover=True)
            report["kept"] = len(self.leftovers) - before
            self.items = []
            self._write()
        return report

    def _undo_gain(self, item: dict) -> int:
        """Put one Normalize item's file back as it was. Filesystem-driven:
        the normalised file at the source's name is removed only when it is
        recognisably OURS (its facts, or the state says Save may have touched
        its tags); a file someone else put there is kept, with the original
        still parked beside it. Records the item in `restored_gain` so the
        caller puts its loudness value back."""
        source = Path(item["source"])
        parked = Path(item["parked"]) if item.get("parked") else None
        partial = Path(item["partial"]) if item.get("partial") else None
        restored = 0
        try:
            if partial is not None:
                partial.unlink(missing_ok=True)
            if parked is not None and parked.exists():
                ours = item.get("state") in ("saving", "saved") or source_facts(source) in (None, item.get("facts_after"))
                if source.exists() and ours and item.get("state") in _PUBLISHED_GAIN:
                    source.unlink()
                if not source.exists():
                    _retrying(lambda: _replace_no_overwrite(parked, source))
                    restored = 1
        except OSError:
            return restored
        if restored:
            self.restored_gain.append(item)
        return restored

    def _undo(self, item: dict, *, keep_leftover: bool) -> int:
        """Put one item's files back as they were. Filesystem-driven."""
        restored = 0
        original = Path(item["source"])
        parked = Path(item["parked"]) if item.get("parked") else None
        if parked is not None and parked.exists() and not original.exists():
            _retrying(lambda: _replace_no_overwrite(parked, original))
            restored = 1
        stem = Path(item["stem"])
        if keep_leftover and item.get("written"):
            self._keep_as_leftover(item)
        else:
            try:
                # Only a stem file THIS batch published is ours to delete: before
                # that, a file at the target name belongs to someone else.
                if _published(item):
                    stem.unlink(missing_ok=True)
                partial_name(stem).unlink(missing_ok=True)  # unverified: worthless
            except OSError:
                pass
        return restored


def _published(item: dict) -> bool:
    return item.get("state") in ("published", "parked", "swapped", "committed")


def _kind(item: dict) -> str:
    return item.get("kind") or "stem"


#: The states in which an item's library edit is made and awaits Save.
_AWAITING_SAVE = {"stem": ("swapped",), "gain": ("swapped", "saving", "saved")}
#: A gain item whose normalised file has been renamed onto the source's name.
_PUBLISHED_GAIN = ("published", "swapped", "saving", "saved")


__all__ = ["PARKED", "PARTIAL", "Pending", "parked_name", "partial_name", "source_facts",
           "_replace_no_overwrite", "_retrying"]
