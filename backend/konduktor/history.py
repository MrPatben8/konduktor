"""Per-collection version history backed by a purely-local git repo (dulwich).

Replaces the old ``.bak`` copy-on-save. Every Konduktor write to a collection is
committed to a small git repo — one per collection — living in the app-data dir
(NOT next to the user's collection). Git's delta packing makes near-identical
NMLs almost free, so we keep full history forever; a manual "delete all history"
is the only pruning.

Design rules:
  * ALL git interaction is confined to this module.
  * EVERY public function is best-effort: it swallows its own errors (returning a
    safe empty/None value) so a history failure can never break a save or an open.
  * The tracked file is a fixed name inside each repo (``collection.nml``); the
    repo is keyed by a hash of the collection's absolute OS path (mirrors prefs'
    path-based keying). Collection identity is therefore the OS path — moving the
    collection starts a fresh history (same fragility prefs already accepts).
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dulwich import porcelain
from dulwich.repo import Repo

from . import paths

log = logging.getLogger(__name__)

_TRACKED_NAME = "collection.nml"
_AUTHOR = b"Konduktor <konduktor@localhost>"


@dataclass
class HistoryEntry:
    id: str  # full commit sha (hex)
    timestamp: str  # ISO 8601, UTC
    summary: str  # first line of the commit message


# ---- repo location -----------------------------------------------------------

def _key(nml_path: Path) -> str:
    return hashlib.sha256(str(Path(nml_path).resolve()).encode()).hexdigest()[:16]


def _repo_dir(nml_path: Path) -> Path:
    return paths.app_data_dir() / "history" / _key(nml_path)


def _tracked_file(repo_dir: Path) -> Path:
    return repo_dir / _TRACKED_NAME


def _write_meta(repo_dir: Path, nml_path: Path) -> None:
    """A tiny sidecar recording which collection this repo belongs to (for
    debugging / a friendly display name). Best-effort."""
    try:
        meta = {"path": str(Path(nml_path).resolve()), "name": Path(nml_path).parent.name}
        (repo_dir / ".meta.json").write_text(json.dumps(meta, indent=2))
    except OSError:
        pass


def _open_or_init(nml_path: Path) -> Path:
    """Ensure the collection's repo exists; return its directory."""
    repo_dir = _repo_dir(nml_path)
    if not (repo_dir / ".git").exists():
        repo_dir.mkdir(parents=True, exist_ok=True)
        porcelain.init(str(repo_dir))
        _write_meta(repo_dir, nml_path)
    return repo_dir


def _head_blob(repo: Repo) -> bytes | None:
    """The tracked file's bytes at HEAD, or None if the repo has no commits."""
    try:
        head = repo.head()
    except KeyError:
        return None
    commit = repo[head]
    tree = repo[commit.tree]
    _mode, blob_sha = tree[_TRACKED_NAME.encode()]
    return repo[blob_sha].data


# ---- public API --------------------------------------------------------------

def commit(nml_path: Path, data: bytes, message: str, app_version: str) -> str | None:
    """Commit ``data`` as the collection's newest version. Deduped: if ``data``
    is byte-identical to HEAD, no commit is made and None is returned. Returns
    the new commit sha (hex) on success, None on dedup or any failure."""
    try:
        repo_dir = _open_or_init(nml_path)
        with Repo(str(repo_dir)) as repo:
            if _head_blob(repo) == data:
                return None  # nothing changed since last version
            path = _tracked_file(repo_dir)
            path.write_bytes(data)
            porcelain.add(str(repo_dir), paths=[str(path)])
            msg = f"{message}\n\nApp-Version: {app_version}".encode()
            sha = porcelain.commit(str(repo_dir), message=msg, author=_AUTHOR, committer=_AUTHOR)
        return sha.decode() if isinstance(sha, bytes) else str(sha)
    except Exception:  # best-effort — history must never break a save
        log.exception("history.commit failed for %s", nml_path)
        return None


def ensure_baseline(nml_path: Path) -> None:
    """Commit the current on-disk collection as an "as I found it" baseline.
    Deduped, so re-opening an unchanged collection is a no-op. Best-effort."""
    try:
        data = Path(nml_path).read_bytes()
    except OSError:
        return
    commit(nml_path, data, "Baseline — opened collection", _app_version())


def list_history(nml_path: Path) -> list[HistoryEntry]:
    """All versions, newest first. Empty list if no history / on any failure."""
    repo_dir = _repo_dir(nml_path)
    if not (repo_dir / ".git").exists():
        return []
    try:
        entries: list[HistoryEntry] = []
        with Repo(str(repo_dir)) as repo:
            try:
                repo.head()
            except KeyError:
                return []
            for e in repo.get_walker():
                c = e.commit
                ts = datetime.fromtimestamp(c.commit_time, tz=timezone.utc).isoformat()
                summary = c.message.decode("utf-8", "replace").splitlines()[0] if c.message else ""
                entries.append(HistoryEntry(id=c.id.decode(), timestamp=ts, summary=summary))
        return entries
    except Exception:
        log.exception("history.list_history failed for %s", nml_path)
        return []


def read_version(nml_path: Path, commit_id: str) -> bytes | None:
    """The tracked collection bytes at a specific commit, or None on failure."""
    repo_dir = _repo_dir(nml_path)
    if not (repo_dir / ".git").exists():
        return None
    try:
        with Repo(str(repo_dir)) as repo:
            c = repo[commit_id.encode()]
            tree = repo[c.tree]
            _mode, blob_sha = tree[_TRACKED_NAME.encode()]
            return repo[blob_sha].data
    except Exception:
        log.exception("history.read_version failed for %s @ %s", nml_path, commit_id)
        return None


def clear_history(nml_path: Path) -> None:
    """Delete ALL version history for a collection (removes its repo). Best-effort."""
    try:
        shutil.rmtree(_repo_dir(nml_path), ignore_errors=True)
    except Exception:
        log.exception("history.clear_history failed for %s", nml_path)


def _app_version() -> str:
    from . import __version__

    return __version__
