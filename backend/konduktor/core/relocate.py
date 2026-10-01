"""Finding where a library's audio went: the search behind auto path remapping.

A library stores absolute paths, and they go stale in ways a DJ does not think
of as "moving" anything: the collection on an SSD was built on Windows where the
drive was `X:`, and on a Mac the same drive is `/Volumes/MiniSSD`. Every track
then reads as missing, though nothing moved. That is what this module fixes,
and ALL it fixes — a prefix changed, the layout underneath did not. A user who
reorganised their folders needs a relocate-by-filename feature, which this is
not.

The search is by SAMPLING, never by walking a drive: a drive can hold hundreds
of thousands of files, and a stat of a path that is not there costs next to
nothing. For each group of tracks that share a stored root, a sample of tracks
is tried against each candidate root (the mounted drives, the home folder),
dropping leading folders one at a time, and the roots that resolve them are
counted. The winner is then verified against the WHOLE group, because a count
the user is asked to confirm should be the real one.

Which groups to search is the adapter's business, not this module's: only the
adapter knows how its stored paths resolve, including through a mapping the
user already saved. It hands over the groups in which NO track resolves — the
threshold is "none", so a volume missing a few deleted files never prompts.

Two candidates that both resolve most of a group (a drive and its backup clone)
are reported as AMBIGUOUS rather than ranked: which one the user meant is not a
question sample counts can answer, and a wrong guess would silently play and
tag files on the wrong drive.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .pathmap import PathMapping

#: Tracks sampled per group. Enough that a drive holding a partial copy loses
#: clearly to one holding all of it; few enough that a group costs milliseconds.
SAMPLE = 50

#: How many leading folders may be dropped from a stored path. Covers
#: `/Users/<name>/Music/<library>`-deep moves with room to spare.
MAX_DROP = 8

#: A runner-up resolving at least this share of the winner's tracks makes the
#: group ambiguous.
AMBIGUOUS_SHARE = 0.5

_SEP = re.compile(r"[\\/]")


@dataclass
class PathGroup:
    """Tracks whose stored paths share a root the library names as one place.

    `root` is the OS-path prefix every path starts with (`X:`, `/Volumes/SSD`,
    `/`); `label` is how the library itself names that place, for the UI.
    `paths` are the STORED paths — before any mapping.
    """

    label: str
    root: str
    paths: list[str]


@dataclass
class Candidate:
    """A mapping that resolves some of a group, with how many it resolves."""

    mapping: PathMapping
    found: int


@dataclass
class Proposal:
    """What the search concluded about one group."""

    label: str
    root: str
    total: int
    #: Best first. Empty when nothing was found.
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def status(self) -> str:
        if not self.candidates:
            return "not_found"
        if len(self.candidates) > 1:
            return "ambiguous"
        return "found"


def _split(path: str, root: str) -> tuple[list[str], str] | None:
    """A stored path's folders below `root`, and its filename."""
    if not path.startswith(root):
        return None
    segs = [s for s in _SEP.split(path[len(root):]) if s]
    if not segs:
        return None
    return segs[:-1], segs[-1]


def _from_prefix(root: str, path: str, dropped: list[str]) -> str:
    """The stored prefix a mapping must replace: `root` plus the dropped folders,
    spelled with the separator the stored path uses, so it matches it."""
    if not dropped:
        return root
    rest = path[len(root):]
    sep = "\\" if "\\" in rest and "/" not in rest else "/"
    return root.rstrip("/\\") + sep + sep.join(dropped)


def _sample(paths: list[str], n: int) -> list[str]:
    """`n` paths spread evenly over the group, so one album is not the sample."""
    if len(paths) <= n:
        return list(paths)
    step = len(paths) / n
    return [paths[int(i * step)] for i in range(n)]


def _hits(path: str, root: str, roots: list[Path]) -> set[tuple[str, str]]:
    """Every (from, to) mapping that resolves this one stored path, at the
    SHALLOWEST drop that resolves it at all — dropping more folders only makes a
    match likelier to be a coincidence."""
    split = _split(path, root)
    if split is None:
        return set()
    dirs, name = split
    # A bare filesystem root cannot be a mapping's `from` (it would match every
    # path), so a group rooted at `/` starts by dropping one folder. And at
    # least one folder must survive the drop: a filename alone at a drive's
    # root is far too weak to call a match.
    first = 1 if not root.strip("/\\") else 0
    last = min(MAX_DROP, max(len(dirs) - 1, 0))
    for drop in range(first, last + 1):
        found = set()
        for base in roots:
            try:
                if base.joinpath(*dirs[drop:], name).exists():
                    found.add((_from_prefix(root, path, dirs[:drop]), str(base)))
            except OSError:
                continue
        if found:
            return found
    return set()


def _resolves(mapping: PathMapping, paths: list[str]) -> int:
    n = 0
    for p in paths:
        target = mapping.apply(Path(p))
        try:
            if str(target) != p and target.exists():
                n += 1
        except OSError:
            continue
    return n


def search(groups: list[PathGroup], roots: list[Path]) -> list[Proposal]:
    """Where each group's tracks are now, if anywhere among `roots`."""
    roots = list(dict.fromkeys(Path(r) for r in roots))
    out: list[Proposal] = []
    for group in groups:
        sample = _sample(group.paths, SAMPLE)
        votes: dict[tuple[str, str], int] = {}
        for p in sample:
            for hit in _hits(p, group.root, roots):
                votes[hit] = votes.get(hit, 0) + 1
        # One sampled track is a coincidence (a common filename in a common
        # folder name); a group too small to sample three is taken on one.
        floor = min(3, len(sample))
        ranked = sorted(
            (h for h, n in votes.items() if n >= floor), key=lambda h: -votes[h]
        )[:3]
        candidates = []
        for from_, to in ranked:
            mapping = PathMapping.make(from_, to)
            found = _resolves(mapping, group.paths)
            if found:
                candidates.append(Candidate(mapping, found))
        candidates.sort(key=lambda c: -c.found)
        if candidates:
            best = candidates[0].found
            candidates = [c for c in candidates if c.found >= best * AMBIGUOUS_SHARE]
        out.append(Proposal(group.label, group.root, len(group.paths), candidates))
    return out
