"""Query, facet and statistics layer over the generic model.

This is deliberately platform-independent: it reads nothing but `Track`
attributes. Keeping it in `core` rather than per-adapter is what stops "sort by
artist" quietly meaning different things on different platforms behind one UI —
if a filter needs something the projection cannot express, the fix is to add the
field to the projection, where every adapter must then answer for it.

An adapter owns its `TrackIndex` instance and refreshes it after each command.
"""
from __future__ import annotations

import threading

from .model import Facets, GenreCount, Stats, Track, TrackPage


class TrackIndex:
    """An ordered list of projected tracks plus a primary-key index."""

    def __init__(self, tracks: list[Track] | None = None):
        self._lock = threading.Lock()
        self.tracks: list[Track] = []
        self.by_key: dict[str, Track] = {}
        if tracks is not None:
            self.rebuild(tracks)

    def rebuild(self, tracks: list[Track]) -> None:
        """Replace the whole projection — after a save or a path remap, where
        track ids themselves may have changed."""
        with self._lock:
            self.tracks = tracks
            self.by_key = {t.id: t for t in tracks}

    def all(self) -> list[Track]:
        return self.tracks

    def get(self, track_id: str) -> Track | None:
        return self.by_key.get(track_id)

    def add(self, track: Track) -> None:
        """Put a track the projection has never seen into it.

        `replace` deliberately ignores an unknown id — it exists to refresh, and
        silently inventing a track would hide a bug. Adding is a different
        intent and needs saying out loud, which is why importing a track needs
        this rather than being able to reuse `replace`.

        An id that is already present is refreshed instead of duplicated:
        `by_key` could not represent two, so appending would desynchronise it
        from `tracks`.
        """
        with self._lock:
            if track.id in self.by_key:
                self.replace(track)
                return
            self.tracks.append(track)
            self.by_key[track.id] = track

    def replace(self, track: Track) -> None:
        """Refresh one track's projection in place.

        Mutating the existing object rather than replacing it keeps `tracks`,
        `by_key` and any list a caller is already holding consistent.
        """
        old = self.by_key.get(track.id)
        if old is None:
            return
        for field in type(track).model_fields:
            setattr(old, field, getattr(track, field))

    _SORT_KEYS = {
        "artist": lambda t: (t.artist or "").lower(),
        "title": lambda t: (t.title or "").lower(),
        "album": lambda t: (t.album or "").lower(),
        "genre": lambda t: (t.genre or "").lower(),
        "key": lambda t: (t.key or ""),
        "bpm": lambda t: (t.bpm if t.bpm is not None else -1),
        "rating": lambda t: t.rating,
        "playcount": lambda t: (t.playcount or 0),
        "import_date": lambda t: (t.import_date or ""),
        "length": lambda t: (t.length or 0),
    }

    def query_tracks(
        self,
        q: str | None = None,
        genre: str | None = None,
        key: str | None = None,
        bpm_min: float | None = None,
        bpm_max: float | None = None,
        rating_min: int | None = None,
        has_cues: bool | None = None,
        sort: str = "artist",
        order: str = "asc",
        limit: int = 100,
        offset: int = 0,
    ) -> TrackPage:
        items = self.tracks
        if q:
            ql = q.lower()
            items = [
                t
                for t in items
                if (t.artist and ql in t.artist.lower())
                or (t.title and ql in t.title.lower())
                or (t.album and ql in t.album.lower())
            ]
        if genre:
            items = [t for t in items if t.genre == genre]
        if key:
            items = [t for t in items if t.key == key]
        if bpm_min is not None:
            items = [t for t in items if t.bpm is not None and t.bpm >= bpm_min]
        if bpm_max is not None:
            items = [t for t in items if t.bpm is not None and t.bpm <= bpm_max]
        if rating_min is not None:
            items = [t for t in items if t.rating >= rating_min]
        if has_cues is not None:
            items = [t for t in items if (t.cue_count > 0) == has_cues]

        keyfn = self._SORT_KEYS.get(sort, self._SORT_KEYS["artist"])
        items = sorted(items, key=keyfn, reverse=(order == "desc"))

        total = len(items)
        page = items[offset : offset + limit]
        return TrackPage(total=total, offset=offset, limit=limit, items=page)

    def facets(self) -> Facets:
        genres: dict[str, int] = {}
        keys: dict[str, int] = {}
        bpms = []
        for t in self.tracks:
            if t.genre:
                genres[t.genre] = genres.get(t.genre, 0) + 1
            if t.key:
                keys[t.key] = keys.get(t.key, 0) + 1
            if t.bpm is not None:
                bpms.append(t.bpm)
        return Facets(
            genres=[
                GenreCount(name=k, count=v)
                for k, v in sorted(genres.items(), key=lambda x: -x[1])
            ],
            keys=[
                GenreCount(name=k, count=v)
                for k, v in sorted(keys.items(), key=lambda x: -x[1])
            ],
            bpm_min=min(bpms) if bpms else None,
            bpm_max=max(bpms) if bpms else None,
            total_tracks=len(self.tracks),
        )

    def stats(self, playlist_count: int) -> Stats:
        rating_breakdown = {i: 0 for i in range(6)}
        genres: dict[str, int] = {}
        bpm_buckets: dict[str, int] = {}
        rated = missing_key = missing_genre = missing_bpm = no_cues = 0
        for t in self.tracks:
            rating_breakdown[t.rating] += 1
            if t.rating > 0:
                rated += 1
            if not t.key:
                missing_key += 1
            if not t.genre:
                missing_genre += 1
            else:
                genres[t.genre] = genres.get(t.genre, 0) + 1
            if t.bpm is None:
                missing_bpm += 1
            else:
                lo = int(t.bpm // 10 * 10)
                bucket = f"{lo}-{lo + 10}"
                bpm_buckets[bucket] = bpm_buckets.get(bucket, 0) + 1
            if t.cue_count == 0:
                no_cues += 1
        total = len(self.tracks)
        histogram = [
            {"bucket": b, "count": c}
            for b, c in sorted(bpm_buckets.items(), key=lambda x: int(x[0].split("-")[0]))
        ]
        top = sorted(genres.items(), key=lambda x: -x[1])[:10]
        return Stats(
            total_tracks=total,
            total_playlists=playlist_count,
            rated=rated,
            unrated=total - rated,
            missing_key=missing_key,
            missing_genre=missing_genre,
            missing_bpm=missing_bpm,
            no_cues=no_cues,
            rating_breakdown=rating_breakdown,
            bpm_histogram=histogram,
            top_genres=[GenreCount(name=k, count=v) for k, v in top],
        )
