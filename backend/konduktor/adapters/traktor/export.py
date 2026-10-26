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
from ...core.export import OTHER_PLAYLIST, ExportPayload, WrittenLibrary, write_playlist_tree
from .adapter import TraktorAdapter
from . import file_block
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

class TraktorExporter:
    platform = "traktor"
    menu_order = 20
    library_filename = "collection.nml"
    drive_root = False
    #: `LOCK` — honoured from `TrackCues.grid_locked` by `add_tracks`.
    locks_grids = True
    #: `<LOUDNESS>` from `ExportTrack.loudness` (kit G1: an entry with no
    #: AUDIO_ID keeps the PERCEIVED_DB the deck reads through Traktor's first
    #: analysis, which overwrites only ANALYZED_DB).
    writes_auto_gain = True

    @staticmethod
    def prepare_copy(path: Path) -> bool:
        """Remove Traktor's own block from a copy (`file_block`): left in, it
        is restored over this entry's grid when Traktor opens the collection."""
        return file_block.strip(path)

    def capabilities(self):
        """What a Traktor library can hold — answered with no library to read.

        `editable_fields` is the full safe set: an exported collection is
        brand new, so nothing about it is read-only.
        """
        return capabilities_for(
            Path(self.library_filename),
            [
                "title", "artist", "album", "genre", "label", "remixer",
                "producer", "mix", "release_date", "comment", "comment2", "rating",
            ],
        )

    def write(self, payload: ExportPayload, destination: Path) -> WrittenLibrary:
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
        # The auto gain, through the same command an Analyze uses.
        for t, new_id in zip(payload.tracks, new_ids):
            if t.loudness is not None:
                adapter.set_auto_gain(new_id, t.loudness)

        self._write_playlists(adapter, payload, moved)
        adapter.save()
        # One file; nothing else to report.
        return WrittenLibrary(library=library)

    def _write_playlists(self, adapter, payload: ExportPayload, moved: dict) -> None:
        """Recreate the tree — at the top level, or with `payload.root_folder`
        under one folder named after the export (None = the top level)."""
        root = adapter.create_folder(payload.name) if payload.root_folder else None
        write_playlist_tree(adapter, payload.playlists, [t.source_id for t in payload.tracks],
                            moved, root)
