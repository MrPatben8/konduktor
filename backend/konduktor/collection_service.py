"""Loads a Traktor collection and serves query-friendly views of it.

Phase 1 is read-only. The service parses the .nml once into memory, builds a
flat list of Track objects, a primary-key index (so playlist entries can be
joined to full track metadata), and the playlist tree.
"""
from __future__ import annotations

import threading
from pathlib import Path

from traktor_nml_utils import TraktorCollection

from .schemas import (
    Facets,
    GenreCount,
    PlaylistNode,
    Stats,
    Track,
    TrackPage,
)


def _primary_key(location) -> str:
    """Reconstruct the Traktor primary key used by playlist entries."""
    vol = location.volume or ""
    d = location.dir or ""
    f = location.file or ""
    return f"{vol}{d}{f}"


def _display_path(location) -> str:
    """Human-readable OS-ish path from Traktor's '/:'-separated dir."""
    d = (location.dir or "").replace("/:", "/")
    f = location.file or ""
    return f"{d}{f}"


def _rating_stars(ranking) -> int:
    if not ranking:
        return 0
    return max(0, min(5, round(ranking / 51)))


class CollectionService:
    def __init__(self, nml_path: Path):
        self.nml_path = Path(nml_path)
        self._lock = threading.Lock()
        self.tracks: list[Track] = []
        self.by_key: dict[str, Track] = {}
        self._playlist_entries: dict[str, list[str]] = {}  # id -> ordered primary keys
        self._tree: list[PlaylistNode] = []
        self._playlist_count = 0
        self.load()

    # ---- loading -------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            collection = TraktorCollection(path=self.nml_path)
            self.tracks = [self._to_track(e) for e in collection.nml.collection.entry]
            self.by_key = {t.id: t for t in self.tracks}
            self._playlist_entries = {}
            self._playlist_count = 0
            counter = {"n": 0}
            root = collection.nml.playlists.node if collection.nml.playlists else None
            self._tree = self._build_tree(root, counter) if root else []

    def _to_track(self, e) -> Track:
        info = e.info
        loc = e.location
        cues = e.cue_v2 or []
        hotcues = sum(
            1 for c in cues if c.hotcue is not None and c.hotcue >= 0
        )
        has_grid = any(getattr(c, "grid", None) is not None for c in cues)
        return Track(
            id=_primary_key(loc) if loc else (e.title or ""),
            artist=e.artist,
            title=e.title,
            album=e.album.title if e.album else None,
            genre=info.genre if info else None,
            label=info.label if info else None,
            remixer=info.remixer if info else None,
            producer=info.producer if info else None,
            comment=info.comment if info else None,
            bpm=e.tempo.bpm if e.tempo else None,
            key=info.key if info else None,
            rating=_rating_stars(info.ranking if info else None),
            playcount=info.playcount if info else None,
            length=info.playtime if info else None,
            bitrate=info.bitrate if info else None,
            import_date=info.import_date if info else None,
            last_played=info.last_played if info else None,
            release_date=info.release_date if info else None,
            filepath=_display_path(loc) if loc else None,
            cue_count=len(cues),
            hotcue_count=hotcues,
            has_grid=has_grid,
        )

    def _build_tree(self, node, counter, is_root=True) -> list[PlaylistNode]:
        """Return the children of the $ROOT folder as the top-level tree."""
        if node is None:
            return []
        # Root ($ROOT) is a wrapper folder; expose its children as top level.
        if is_root and node.subnodes:
            out = []
            for child in node.subnodes.node:
                out.append(self._node_to_model(child, counter))
            return out
        return [self._node_to_model(node, counter)]

    def _node_to_model(self, node, counter) -> PlaylistNode:
        ntype = node.type or "FOLDER"
        if ntype == "PLAYLIST" or (node.playlist is not None):
            nid = f"pl-{counter['n']}"
            counter["n"] += 1
            self._playlist_count += 1
            keys = []
            if node.playlist and node.playlist.entry:
                for en in node.playlist.entry:
                    if en.primarykey and en.primarykey.key:
                        keys.append(en.primarykey.key)
            self._playlist_entries[nid] = keys
            return PlaylistNode(
                id=nid,
                name=node.name or "(unnamed)",
                type="PLAYLIST",
                uuid=node.playlist.uuid if node.playlist else None,
                count=len(keys),
            )
        if ntype == "SMARTLIST" or getattr(node, "smartlist", None) is not None:
            # Smart playlists are dynamic (rule-based); no static entry list.
            nid = f"sl-{counter['n']}"
            counter["n"] += 1
            return PlaylistNode(id=nid, name=node.name or "(smart)", type="SMARTLIST")
        # FOLDER
        nid = f"fld-{counter['n']}"
        counter["n"] += 1
        children = []
        if node.subnodes:
            for child in node.subnodes.node:
                children.append(self._node_to_model(child, counter))
        return PlaylistNode(
            id=nid, name=node.name or "(folder)", type="FOLDER", children=children
        )

    # ---- queries -------------------------------------------------------
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

    def stats(self) -> Stats:
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
            total_playlists=self._playlist_count,
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

    def playlist_tree(self) -> list[PlaylistNode]:
        return self._tree

    def playlist_tracks(self, playlist_id: str) -> list[Track] | None:
        if playlist_id not in self._playlist_entries:
            return None
        out = []
        for k in self._playlist_entries[playlist_id]:
            t = self.by_key.get(k)
            if t is not None:
                out.append(t)
        return out
