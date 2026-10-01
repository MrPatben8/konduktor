"""Marker-level beatgrid commands, for a store whose one primitive is "replace".

A platform that stores the grid as a flat list of beats (Rekordbox's `PQTZ`, and
so OneLibrary's) has no native "move this marker": every marker-level command is
a read-modify-replace of the generic marker list. Promoted here when OneLibrary
became the second store needing exactly that, so "where does a moved marker
clamp" cannot mean two things. (Traktor's store has markers natively and keeps
its own, companion-cue-aware versions.)

A store using it provides `current_markers(track_id)` and
`replace_grid(track_id, markers)`.
"""
from __future__ import annotations

from .adapter import InvalidCommand, NotFound
from .model import GridMarker


class ReplaceGridCommands:
    def current_markers(self, track_id: str) -> list[GridMarker]:  # pragma: no cover
        raise NotImplementedError

    def replace_grid(self, track_id: str, markers: list[GridMarker]) -> None:  # pragma: no cover
        raise NotImplementedError

    def delete_grid(self, track_id: str) -> None:
        self.replace_grid(track_id, [])

    def add_grid_marker(self, track_id: str, start_sec: float, bpm: float | None = None) -> None:
        markers = self.current_markers(track_id)
        if bpm is None:
            # Inherit the tempo governing this point, like Traktor's add does.
            governing = [m for m in markers if m.start <= start_sec]
            bpm = governing[-1].bpm if governing else (markers[0].bpm if markers else None)
        if not bpm:
            raise InvalidCommand("The first marker on an ungridded track needs a tempo")
        if any(abs(m.start - start_sec) < 0.001 for m in markers):
            raise InvalidCommand("There is already a marker here")
        markers.append(GridMarker(start=float(start_sec), bpm=float(bpm)))
        self.replace_grid(track_id, markers)

    def _marker_at(self, track_id: str, index: int) -> tuple[list, int]:
        markers = self.current_markers(track_id)
        if not 0 <= index < len(markers):
            raise NotFound(f"No grid marker {index}")
        return markers, index

    def move_grid_marker(self, track_id: str, index: int, start_sec: float) -> None:
        markers, i = self._marker_at(track_id, index)
        # Clamp between neighbours so the list cannot reorder under the caller.
        low = markers[i - 1].start + 0.001 if i > 0 else 0.0
        high = markers[i + 1].start - 0.001 if i + 1 < len(markers) else None
        target = max(low, float(start_sec))
        if high is not None:
            target = min(target, high)
        markers[i] = markers[i].model_copy(update={"start": target})
        self.replace_grid(track_id, markers)

    def set_grid_marker_bpm(self, track_id: str, index: int, bpm: float) -> None:
        markers, i = self._marker_at(track_id, index)
        if bpm <= 0:
            raise InvalidCommand(f"A tempo must be positive, got {bpm}")
        markers[i] = markers[i].model_copy(update={"bpm": float(bpm)})
        self.replace_grid(track_id, markers)

    def delete_grid_marker(self, track_id: str, index: int) -> None:
        markers, i = self._marker_at(track_id, index)
        del markers[i]
        self.replace_grid(track_id, markers)
