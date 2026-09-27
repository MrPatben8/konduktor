# Empty rekordbox device library (`export.pdb`, `exportExt.pdb`)

The legacy "Device Library" a Pioneer USB stick carries — the one Rekordbox
lists as **Device Library** and older players (CDJ-2000NXS2, many XDJs) read —
exactly as **Rekordbox 7.2.18 wrote it for an empty stick**: it creates both
files the first time it mounts a drive that has none.

They are the starting point of the "Rekordbox Export" target
(`adapters/rekordbox/device_export.py`), for the same reason `seed.sql` exists:
the format has fixed tables that are the same on every stick and cannot be
derived — the colour list (table 6), the browse columns (16), two menu tables
(17, 18) and a history header (19) in `export.pdb`; rekordbox's default MyTag
names in `exportExt.pdb`. Every data table (tracks, artists, albums, genres,
labels, keys, playlists, artwork) holds only its header page, with a blank page
reserved after it for its first rows — the allocation rule the writer follows.

**No user data**: the only strings are colour names, column names, the default
MyTag names, the creation date (`2026-09-27`, rewritten on every export) and the
database version `1000`. Pages 13-14 and 33-38 are byte-identical to the same
pages of a real rekordbox device export of eight tracks.
