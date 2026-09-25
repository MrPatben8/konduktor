"""What changed this session, at field resolution.

Three things need to know about an edit, and they need different views of it:

  * the file-tag sync wants *which metadata fields* of which track changed, so it
    writes only those and leaves everything else in the file alone;
  * the version history wants a human sentence;
  * a future undo wants the value a field held before.

The journal records one `Change` per command and derives all three. Recording it
at field resolution has to happen from the start: reconstructed afterwards by
diffing, an edit that set a field back to its original value is indistinguishable
from one that never happened — which is exactly the case where rewriting the
field would lose the platform's own formatting of it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Change:
    scope: str  # "track" | "cue" | "grid" | "lock" | "playlist" | "library"
    op: str  # "set" | "add" | "delete" | "modify" | "create" | "rename" | ...
    target: str | None = None  # track id, or a playlist's name
    detail: str | None = None  # field name, "slot:3", "marker:1", "art", ...
    before: Any = None
    after: Any = None


@dataclass
class EditJournal:
    changes: list[Change] = field(default_factory=list)

    # ---- recording --------------------------------------------------------
    def record(
        self,
        scope: str,
        op: str,
        target: str | None = None,
        detail: str | None = None,
        before: Any = None,
        after: Any = None,
    ) -> None:
        self.changes.append(Change(scope, op, target, detail, before, after))

    def clear(self) -> None:
        self.changes.clear()

    @property
    def dirty(self) -> bool:
        return bool(self.changes)

    # ---- views ------------------------------------------------------------
    def fields_for(self, track_id: str) -> set[str]:
        """Metadata field names edited on this track (drives the file-tag sync)."""
        return {
            c.detail
            for c in self.changes
            if c.scope == "track" and c.op == "set" and c.target == track_id and c.detail
        }

    def edited_tracks(self) -> set[str]:
        """Tracks whose metadata or artwork changed.

        Excludes tracks that were ADDED. They are not edits: nothing about them
        changed, they simply did not exist before. Counting them here would both
        mislabel the version-history message and drag them through the file-tag
        sync, which has nothing to write for a track whose fields were never
        touched. Excludes tracks REMOVED from the library for the same reason:
        "edited 40 tracks" would describe a deletion as an edit.
        """
        removed = self.removed_tracks()
        return {
            c.target
            for c in self.changes
            if c.scope == "track" and c.target and c.op not in ("add", "remove")
            and c.target not in removed
        }

    def removed_tracks(self) -> set[str]:
        """Tracks taken out of the library this session."""
        return {
            c.target for c in self.changes if c.scope == "track" and c.op == "remove" and c.target
        }

    def added_tracks(self) -> set[str]:
        """Tracks put into the library that it did not previously hold."""
        return {
            c.target for c in self.changes if c.scope == "track" and c.op == "add" and c.target
        }

    def retarget(self, old_id: str, new_id: str) -> None:
        """Follow a track whose id changed (a path remap rewrites LOCATIONs, and
        the id is derived from the location). Without this, edits made earlier in
        the session would silently stop syncing to the file."""
        if old_id == new_id:
            return
        for i, c in enumerate(self.changes):
            if c.target == old_id and c.scope in ("track", "cue", "grid", "lock"):
                self.changes[i] = Change(c.scope, c.op, new_id, c.detail, c.before, c.after)

    # ---- the version-history message --------------------------------------
    def summary(self, extra_tracks: set[str] | None = None) -> str:
        """A one-line, human-readable summary of this session's edits, used as the
        version-history commit message (e.g. "Edited 3 tracks; renamed playlist
        'House'; added 6 hotcues; edited beatgrid on 2 tracks").

        Prep edits are collapsed to meaningful aggregates rather than raw op
        counts: hotcues report their NET change (created − removed), and beatgrid/
        lock edits report how many distinct tracks were affected — so tweaking one
        grid ten times reads as "1 track", not "10 edits".
        """
        parts: list[str] = []

        def tracks(n: int) -> str:
            return f"{n} track{'' if n == 1 else 's'}"

        # --- tracks added to the library (import) ---
        # First, because it is the largest-grained thing that can have happened:
        # "added 52 tracks" is the headline, and the cue/grid counts that follow
        # are mostly describing those same tracks' imported prep.
        n_added = len(self.added_tracks())
        if n_added:
            parts.append(f"added {tracks(n_added)}")
        n_removed = len(self.removed_tracks())
        if n_removed:
            parts.append(f"removed {tracks(n_removed)} from the collection")

        # --- track metadata + cover art (already counted per-track) ---
        n_meta = len(self.edited_tracks() | (extra_tracks or set()))
        if n_meta:
            parts.append(f"edited {tracks(n_meta)}")

        # --- playlists (name-bearing) ---
        pl_names: dict[str, list[str]] = {}
        for c in self.changes:
            if c.scope == "playlist" and c.target:
                pl_names.setdefault(c.op, []).append(c.target)
        # A folder is its own op so the message does not call it a playlist —
        # "created playlists 'Hardy', 'demos'" reads as two playlists when one is
        # the folder the other went into.
        for op, verb, noun in (
            ("create", "created", "playlist"),
            ("create-folder", "created", "folder"),
            ("rename", "renamed", "playlist"),
            ("delete", "deleted", "playlist"),
            ("entries", "reordered", "playlist"),
        ):
            names = pl_names.get(op, [])
            if not names:
                continue
            if len(names) <= 2:
                joined = ", ".join(f"'{n}'" for n in names)
                parts.append(f"{verb} {noun}{'' if len(names) == 1 else 's'} {joined}")
            else:
                parts.append(f"{verb} {len(names)} {noun}s")

        # --- hotcues: net change in count, scoped by how many tracks were touched ---
        hc_add = hc_del = 0
        hc_tracks: set[str] = set()
        grid_edit: set[str] = set()
        grid_del: set[str] = set()
        lock_on: set[str] = set()
        lock_off: set[str] = set()
        for c in self.changes:
            if c.scope == "cue":
                if c.target:
                    hc_tracks.add(c.target)
                if c.op == "add":
                    hc_add += 1
                elif c.op == "delete":
                    hc_del += 1
            elif c.scope == "grid":
                # "delete" is the ONLY deletion op. Everything else — including
                # ops added later — counts as an edit, because mis-labelling an
                # edit as a deletion in the history summary is the damaging direction.
                (grid_del if c.op == "delete" else grid_edit).add(c.target or "")
            elif c.scope == "lock":
                (lock_on if c.op == "on" else lock_off).add(c.target or "")

        if hc_tracks:
            net = hc_add - hc_del
            scope = f" across {tracks(len(hc_tracks))}" if len(hc_tracks) > 1 else ""
            if net > 0:
                parts.append(f"added {net} hotcue{'' if net == 1 else 's'}{scope}")
            elif net < 0:
                parts.append(f"removed {-net} hotcue{'' if -net == 1 else 's'}{scope}")
            else:  # net zero but there was activity (moves / retypes / add-then-delete)
                parts.append(f"adjusted hotcues{scope}")

        # --- beatgrids & lock: per-track counts ---
        if grid_edit:
            parts.append(f"edited beatgrid on {tracks(len(grid_edit))}")
        if grid_del:
            parts.append(f"deleted beatgrid on {tracks(len(grid_del))}")
        if lock_on:
            parts.append(f"locked {tracks(len(lock_on))}")
        if lock_off:
            parts.append(f"unlocked {tracks(len(lock_off))}")

        # --- path remap ---
        for c in self.changes:
            if c.scope == "library" and c.op == "remap" and c.after:
                parts.append(f"remapped {c.after} file paths")

        if not parts:
            return "Saved changes"
        summary = "; ".join(parts)
        return summary[0].upper() + summary[1:]
