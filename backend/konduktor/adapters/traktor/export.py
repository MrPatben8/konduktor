"""Writing a fresh Traktor `collection.nml` from nothing.

## How it works, and why it is not a second writer

The obvious shape — build an `Nml` object graph by hand and render it — would
mean a SECOND definition of "generic Track plus cues plus grid becomes a Traktor
ENTRY", living beside `TraktorStore.add_entry` and the cue/grid commands. Two
definitions drift, and the new one would inherit none of `test_save_fidelity`'s
coverage, none of `replace_grid`'s companion-cue handling, and none of the
lossiness rules the import feature already settled.

So instead: **write an empty but valid collection, open it, and replay the
ordinary commands onto it.** The skeleton below is the smallest NML Traktor
accepts, verified against a real Traktor Pro 4 file.

That is a deliberate amendment to the original design note, which said an
exporter should not "create then replay commands". Its stated reason was that
doing so would make "an empty new file" and "the user's real library" share code
paths, when the safety of the architecture rests on the write target always
being a retained native model parsed from a real file. Here that invariant holds
literally: the write target IS parsed from a real file — a skeleton Konduktor
wrote a moment earlier — and the import feature has already proved the approach
end to end in Traktor 4.5. The exporter still owns everything genuinely new: the
skeleton, the playlist tree, and the `$ROOT` wrapper.

## The skeleton

Read off Ben's own collection rather than assumed. Note there is **no
`<MUSICFOLDERS>` element at all**, so it is not required, and `<PLAYLISTS>` must
wrap a `$ROOT` FOLDER node — playlists cannot sit at the top level.
"""
from __future__ import annotations

import logging
from pathlib import Path

from ...core.adapter import NewTrack
from ...core.export import ExportPayload
from .adapter import TraktorAdapter
from .capabilities import capabilities_for

log = logging.getLogger(__name__)

#: The smallest collection Traktor Pro 4 accepts. `PROGRAM` names the writer;
#: Traktor itself writes "Traktor Pro 4" here and does not appear to care, but
#: claiming to BE Traktor would be dishonest in a file a user may inspect.
SKELETON = (
    '<?xml version="1.0" encoding="UTF-8" standalone="no" ?>\n'
    '<NML VERSION="20"><HEAD COMPANY="www.native-instruments.com" '
    'PROGRAM="Konduktor"></HEAD>\n'
    '<COLLECTION ENTRIES="0"></COLLECTION>\n'
    '<SETS ENTRIES="0"></SETS>\n'
    '<PLAYLISTS><NODE TYPE="FOLDER" NAME="$ROOT"><SUBNODES COUNT="0">'
    "</SUBNODES>\n</NODE>\n</PLAYLISTS>\n"
    '<INDEXING><SORTING_INFO PATH="$COLLECTION"></SORTING_INFO>\n'
    "</INDEXING>\n</NML>\n"
)

#: Loose tracks — added individually rather than through a playlist — would
#: otherwise be reachable only by search in the exported library.
OTHER_PLAYLIST = "Other"


class TraktorExporter:
    platform = "traktor"
    library_filename = "collection.nml"

    def capabilities(self):
        """What a Traktor library can hold — answered with no library to read.

        `editable_fields` is the full safe set: an exported collection is
        brand new, so nothing about it is read-only.
        """
        return capabilities_for(
            Path(self.library_filename),
            [
                "title", "artist", "album", "genre", "label", "remixer",
                "producer", "mix", "release_date", "comment", "rating",
            ],
        )

    def write(self, payload: ExportPayload, destination: Path) -> Path:
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        library = destination / self.library_filename
        library.write_text(SKELETON, encoding="utf-8")

        adapter = TraktorAdapter(library)

        # The tracks, with their cues and grids, through the ordinary command
        # path — so an exported beatgrid is written by the same `replace_grid`
        # the deck's Reset uses.
        new_ids = adapter.add_tracks(
            [NewTrack(track=t.track, audio_path=t.destination, cues=t.cues)
             for t in payload.tracks]
        )
        # Source id -> the id it got here. Both are Traktor primary keys derived
        # from a LOCATION, and the location changed, so they are never equal.
        moved = {t.source_id: new_id for t, new_id in zip(payload.tracks, new_ids)}

        self._write_playlists(adapter, payload, moved)
        adapter.save()
        return library

    def _write_playlists(self, adapter, payload: ExportPayload, moved: dict) -> None:
        """Recreate the tree under one folder named after the export.

        Nested under the export's own name for the same reason import nests a
        drive's playlists under the drive's name: a collection swapped onto
        another machine should say where its playlists came from.
        """
        root = adapter.create_folder(payload.name)
        folders: dict[tuple[str, ...], str] = {(): root}

        for playlist in payload.playlists:
            parent = self._folder_for(adapter, folders, tuple(playlist.folders), root)
            node_id = adapter.create_playlist(playlist.name, parent)
            entries = [moved[t] for t in playlist.track_ids if t in moved]
            if entries:
                adapter.set_playlist_entries(node_id, entries)

        loose = self._loose(payload, moved)
        if loose:
            node_id = adapter.create_playlist(OTHER_PLAYLIST, root)
            adapter.set_playlist_entries(node_id, loose)

    @staticmethod
    def _folder_for(adapter, folders: dict, path: tuple[str, ...], root: str) -> str:
        """Create (once) the folder chain a playlist sits under, and return it."""
        for depth in range(1, len(path) + 1):
            branch = path[:depth]
            if branch not in folders:
                folders[branch] = adapter.create_folder(branch[-1], folders[branch[:-1]])
        return folders.get(path, root)

    @staticmethod
    def _loose(payload: ExportPayload, moved: dict) -> list[str]:
        """Tracks no exported playlist accounts for, in payload order."""
        in_playlists = {t for p in payload.playlists for t in p.track_ids}
        return [
            moved[t.source_id]
            for t in payload.tracks
            if t.source_id not in in_playlists and t.source_id in moved
        ]
