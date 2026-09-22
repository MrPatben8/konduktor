"""The Rekordbox adapter: owns the native database and the generic projection.

Mirrors `adapters.traktor.adapter`. It holds:

  * a ``RekordboxStore`` — the retained NATIVE model (the open ``master.db``),
  * a ``TrackIndex`` — the generic READ projection built from it.

**Track metadata and playlists are writable; cues and the beatgrid are not
yet.** Those two are separate native stores (two DB tables kept consistent, and
the per-track ANLZ analysis files), so they keep refusing until milestone 3 —
and `capabilities.cues.editable` / `grid.editable` say so, which is what the UI
actually gates on. The raise is the backstop, not the mechanism.

Writes are refused entirely, and permanently, on a **cloud-synced** library:
propagating a bad sync state to the user's other machines is not something
version history can undo — especially here, where there is no version history at
all. That check lives at the top of every command path so no future command can
forget it.
"""
from __future__ import annotations

from pathlib import Path

from ...core.adapter import Unsupported
from ...core.capabilities import Capabilities
from ...core.model import GridMarker, PlaylistNode, Track, TrackCues
from ...core.pathmap import PathMapping, common_dir_prefix
from ...core.query import TrackIndex
from . import capabilities as caps
from . import projection
from .store import RekordboxStore


class RekordboxAdapter:
    platform = "rekordbox"

    def __init__(self, path: Path):
        self.path = Path(path)
        self._store = RekordboxStore(self.path)
        self._index = TrackIndex()
        self._cloud_synced = self._store.cloud_synced
        self._rebuild()

    # ---- projection maintenance -----------------------------------------
    def _rebuild(self) -> None:
        counts = self._store.cue_counts()
        self._index.rebuild(
            [
                projection.to_track(row, counts.get(str(row.ID), (0, 0)))
                for row in self._store.iter_content()
            ]
        )

    def _refresh_cues(self, track_id: str) -> TrackCues:
        """Re-project a track AND return its fresh cues.

        Every mutating cue/grid command ends here: the contract is that a command
        returns the refreshed projection, so a route can never serve a stale one.
        """
        self._refresh(track_id)
        return self.track_cues(track_id) or TrackCues()

    def _refresh(self, track_id: str) -> None:
        """Re-project one track after a command, so the UI cannot go stale.

        Every mutating command must end in this — a forgotten refresh is a
        silently stale projection that no byte- or row-level test would catch.
        """
        counts = self._store.cue_counts()
        row = self._store.content(track_id)
        self._index.replace(projection.to_track(row, counts.get(str(row.ID), (0, 0))))

    def close(self) -> None:
        """Release the database connection.

        Rekordbox's library is a live SQLite file, not a document parsed into
        memory: leaving the connection open keeps a file handle (and possibly a
        `-wal`) around, so an adapter that is being replaced must be closed.
        """
        self._store.close()

    def reload(self) -> None:
        self._store._load()
        self._cloud_synced = self._store.cloud_synced
        self._rebuild()

    # ---- identity --------------------------------------------------------
    def capabilities(self) -> Capabilities:
        return caps.capabilities_for(
            self.path,
            cloud_synced=self._cloud_synced,
            editable_fields=sorted(RekordboxStore.EDITABLE_FIELDS),
        )

    @property
    def cloud_synced(self) -> bool:
        return self._cloud_synced

    # ---- read ------------------------------------------------------------
    @property
    def tracks(self) -> list[Track]:
        return self._index.tracks

    @property
    def by_key(self) -> dict[str, Track]:
        return self._index.by_key

    def track(self, track_id: str) -> Track | None:
        return self._index.get(track_id)

    def query_tracks(self, **kw):
        return self._index.query_tracks(**kw)

    def facets(self):
        return self._index.facets()

    def stats(self, playlist_count: int):
        return self._index.stats(playlist_count)

    def playlist_count(self) -> int:
        return self._store.count_playlists()

    def track_cues(self, track_id: str) -> TrackCues | None:
        if self._index.get(track_id) is None:
            return None
        cues = projection.to_track_cues(
            self._store.cues(track_id), self._store.anlz_grid(track_id)
        )
        # The track projection carries an APPROXIMATE grid_marker_count until the
        # grid is actually read (see projection.to_track). This is that moment —
        # correct it, so the library table stops showing the guess.
        track = self._index.get(track_id)
        if track is not None:
            track.grid_marker_count = len(cues.grid_markers)
        return cues

    # ---- playlists -------------------------------------------------------
    def playlist_tree(self) -> list[PlaylistNode]:
        """Rekordbox stores the tree as a flat table with ``ParentID`` links."""
        from pyrekordbox.masterdb import models as tables

        rows = self._store.playlists()
        folder = int(tables.PlaylistType.FOLDER)
        smart = int(tables.PlaylistType.SMART_PLAYLIST)
        writable = not self._cloud_synced

        nodes: dict[str, PlaylistNode] = {}
        parents: dict[str, str] = {}
        for r in rows:
            attribute = int(r.Attribute or 0)
            kind = "folder" if attribute == folder else ("smart" if attribute == smart else "playlist")
            node_id = str(r.ID)
            nodes[node_id] = PlaylistNode(
                id=node_id,
                name=r.Name or "",
                kind=kind,
                count=(0 if kind == "folder" else len(self._store.playlist_song_ids(node_id))),
                children=[],
                # A smart playlist has no static entry list; a folder has no tracks.
                selectable=kind == "playlist",
                # Rekordbox's own working lists are filtered out by the store, so
                # everything reaching here is the user's and may be edited — as
                # long as the library itself is writable at all.
                can_add_tracks=writable and kind == "playlist",
                can_reorder=writable and kind == "playlist",
                can_rename=writable,
                can_delete=writable,
                can_contain_children=kind == "folder",
            )
            parent = str(r.ParentID or "root")
            parents[node_id] = "root" if parent in ("0", "", "root", "None") else parent

        roots: list[PlaylistNode] = []
        for node_id, node in nodes.items():
            parent_id = parents.get(node_id, "root")
            parent = nodes.get(parent_id)
            if parent is None:
                roots.append(node)
            else:
                parent.children.append(node)
        return roots

    def playlist_entries(self, node_id: str) -> list[str] | None:
        ids = self._store.playlist_song_ids(node_id)
        return ids if ids or self._is_playlist(node_id) else None

    def playlist_tracks(self, node_id: str) -> list[Track] | None:
        keys = self.playlist_entries(node_id)
        if keys is None:
            return None
        return [t for k in keys if (t := self._index.get(k)) is not None]

    def _is_playlist(self, node_id: str) -> bool:
        return any(str(r.ID) == str(node_id) for r in self._store.playlists())

    # ---- audio / paths ----------------------------------------------------
    def audio_path(self, track_id: str):
        return self._store.audio_path(track_id)

    def set_path_mapping(self, mapping: PathMapping) -> None:
        self._store.set_path_mapping(mapping)

    def path_prefix_suggestions(self) -> dict:
        paths = self._store.all_audio_paths()
        primary = common_dir_prefix(paths)
        return {"primary": primary, "groups": [{"prefix": primary, "count": len(paths)}] if primary else []}

    def remap_preview(self, mapping: PathMapping) -> dict:
        paths = [Path(p) for p in self._store.all_audio_paths()]
        matched = [p for p in paths if mapping.matches(p)]
        samples = []
        for p in matched[:5]:
            target = mapping.apply(p)
            samples.append({"from": str(p), "to": str(target), "exists": target.exists()})
        existing = sum(1 for p in matched if mapping.apply(p).exists())
        return {
            "total": len(paths),
            "matched": len(matched),
            "existing": existing,
            "samples": samples,
        }

    def remap_locations(self, mapping: PathMapping) -> int:
        raise Unsupported(self._readonly_reason("Rewriting stored paths"))

    # ---- commands: track metadata ----------------------------------------
    def set_track_metadata(self, track_id: str, fields: dict) -> Track | None:
        self._require_writable("Editing track metadata")
        self._store.set_track_metadata(track_id, fields)
        self._refresh(track_id)
        return self._index.get(track_id)

    # ---- commands: playlists ----------------------------------------------
    def create_playlist(self, name: str, parent_id: str | None = None) -> str:
        self._require_writable("Creating playlists")
        return self._store.create_playlist(name, parent_id)

    def create_folder(self, name: str, parent_id: str | None = None) -> str:
        # Rekordbox CAN hold folders (`capabilities.playlists.folders` is
        # True, and pyrekordbox has `add_playlist_folder`), so this is a
        # gap rather than a refusal in principle. Traktor is the only
        # import target for now, so it is the only one that needed it.
        self._refuse("Creating playlist folders")

    def rename_playlist(self, node_id: str, name: str) -> None:
        self._require_writable("Renaming playlists")
        self._store.rename_playlist(node_id, name)

    def delete_playlist(self, node_id: str) -> None:
        self._require_writable("Deleting playlists")
        self._store.delete_playlist(node_id)

    def set_playlist_entries(self, node_id: str, track_ids: list[str]) -> int:
        self._require_writable("Editing playlists")
        return self._store.set_playlist_entries(node_id, track_ids)

    # ---- commands: cues ---------------------------------------------------
    def _check_cue_type(self, cue_type: str) -> None:
        if cue_type not in caps.WRITABLE_CUE_TYPES:
            raise Unsupported(f"Rekordbox has no cue type {cue_type!r}")

    def set_cue(
        self,
        track_id: str,
        *,
        slot: int,
        start_sec: float,
        cue_type: str,
        length_sec: float = 0.0,
        role: str = "hotcue",
        name: str | None = None,
    ) -> TrackCues:
        self._require_writable("Editing cues")
        if role != "hotcue":
            # Rekordbox is the ONLY platform with memory cues, so under the
            # two-platform promotion rule they stay preserved-but-uneditable:
            # they are projected and shown, never written.
            raise Unsupported(
                "Memory cues are shown but cannot be edited — only hot cues are writable"
            )
        self._check_cue_type(cue_type)
        self._store.set_cue(
            track_id, slot=slot, start_sec=start_sec, cue_type=cue_type,
            length_sec=length_sec, name=name,
        )
        return self._refresh_cues(track_id)

    def set_cue_type(self, track_id: str, slot: int, cue_type: str) -> TrackCues:
        self._require_writable("Editing cues")
        self._check_cue_type(cue_type)
        self._store.set_cue_type(track_id, slot, cue_type)
        return self._refresh_cues(track_id)

    def delete_cue(self, track_id: str, slot: int) -> TrackCues:
        self._require_writable("Deleting cues")
        self._store.delete_cue(track_id, slot)
        return self._refresh_cues(track_id)

    def place_cues(self, track_id: str, cues: list, *, overwrite: bool = False) -> TrackCues:
        self._require_writable("Placing cues")
        for cue in cues:
            self._check_cue_type(getattr(cue, "type", "cue"))
        self._store.place_cues(track_id, cues, overwrite=overwrite)
        return self._refresh_cues(track_id)

    # ---- commands: beatgrid -----------------------------------------------
    def _markers_from(self, markers: list) -> list:
        """Accept either GridMarkers or the (start, bpm) tuples routes send."""
        out = []
        for m in markers:
            if isinstance(m, GridMarker):
                out.append(m)
            else:
                start, bpm = m
                out.append(GridMarker(start=float(start), bpm=float(bpm)))
        return out

    def add_grid_marker(self, track_id: str, start_sec: float, bpm: float | None = None) -> TrackCues:
        self._require_writable("Editing the beatgrid")
        self._store.add_grid_marker(track_id, start_sec, bpm)
        return self._refresh_cues(track_id)

    def move_grid_marker(self, track_id: str, index: int, start_sec: float) -> TrackCues:
        self._require_writable("Editing the beatgrid")
        self._store.move_grid_marker(track_id, index, start_sec)
        return self._refresh_cues(track_id)

    def set_grid_marker_bpm(self, track_id: str, index: int, bpm: float) -> TrackCues:
        self._require_writable("Editing the beatgrid")
        self._store.set_grid_marker_bpm(track_id, index, bpm)
        return self._refresh_cues(track_id)

    def delete_grid_marker(self, track_id: str, index: int) -> TrackCues:
        self._require_writable("Editing the beatgrid")
        self._store.delete_grid_marker(track_id, index)
        return self._refresh_cues(track_id)

    def replace_grid(self, track_id: str, markers: list) -> TrackCues:
        self._require_writable("Editing the beatgrid")
        self._store.replace_grid(track_id, self._markers_from(markers))
        return self._refresh_cues(track_id)

    def set_analysed_grid(self, track_id: str, markers: list) -> TrackCues:
        """Write an analysis result the way Rekordbox's own analyser would.

        Rekordbox pairs nothing with a grid marker — the companion cue is a
        Traktor convention — so this is just a replace.
        """
        return self.replace_grid(track_id, markers)

    def delete_grid(self, track_id: str) -> TrackCues:
        self._require_writable("Deleting the beatgrid")
        self._store.delete_grid(track_id)
        return self._refresh_cues(track_id)

    # ---- save -------------------------------------------------------------
    @property
    def dirty(self) -> bool:
        return self._store.dirty

    def save(self):
        self._require_writable("Saving")
        return self._store.save()

    def snapshot(self) -> bytes:
        """The library's bytes, for version history.

        `master.db` alone is NOT a complete snapshot of a Rekordbox library (the
        beatgrids live in separate ANLZ files, and restoring the database
        wholesale would also roll back Rekordbox's own auth and sampler state),
        which is why `capabilities.save.history` is False. Nothing should be
        calling this.
        """
        raise Unsupported("Rekordbox libraries are not versioned by Konduktor")

    # ---- commands: all refused in this milestone -------------------------
    def _readonly_reason(self, what: str) -> str:
        if self._cloud_synced:
            return (
                f"{what} is not possible: this Rekordbox library is synced with "
                "Rekordbox Cloud, and Konduktor will not write to a cloud-synced "
                "library."
            )
        return f"{what} is not supported yet for Rekordbox libraries."

    def _refuse(self, what: str):
        raise Unsupported(self._readonly_reason(what))

    def _require_writable(self, what: str) -> None:
        """Every write passes through here, so the cloud-sync refusal cannot be
        forgotten by a command added later."""
        if self._cloud_synced:
            raise Unsupported(self._readonly_reason(what))

    def add_tracks(self, items: list) -> list[str]:
        # Not a refusal in principle — Rekordbox could receive imported
        # tracks — but adding one means a djmdContent row, its lookup-table
        # foreign keys, a USN, and an ANLZ file built from scratch, which
        # is unexplored (see the export handoff). Traktor is the only
        # import target for now.
        self._refuse("Adding tracks")

    def set_cover_art(self, track_id: str, data: bytes, mime: str) -> None:
        self._refuse("Editing cover art")

    def cover_art(self, track_id: str) -> tuple[bytes, str] | None:
        return None

    def set_grid_lock(self, track_id: str, locked: bool) -> TrackCues:
        # Rekordbox has no per-track grid lock; capabilities.grid.lockable says so.
        raise Unsupported("Rekordbox has no beatgrid lock")
