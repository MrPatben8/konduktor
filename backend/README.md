# Konduktor — Backend (Phase 1)

FastAPI service that reads a Traktor Pro 4 `collection.nml` and serves a
query-friendly view of the library: tracks (search / filter / sort), library
stats, filter facets, and the playlist tree with track resolution.

**Phase 1 is strictly read-only.** It never writes to your `.nml`. Writing
(playlist editing) arrives in Phase 3 and will operate on a backup-first copy.

## Setup

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> Note: `traktor-nml-utils` is installed from GitHub `master` (v4.0.0), **not**
> PyPI. The PyPI release (3.1.0) cannot parse Traktor Pro 4 (NML v20) files.

## Run

```bash
source .venv/bin/activate
uvicorn konduktor.main:app --reload --port 8000
```

By default it reads `../collection.nml` (the repo-root file). Point it elsewhere:

```bash
KONDUKTOR_NML=/path/to/collection.nml uvicorn konduktor.main:app --port 8000
```

Interactive API docs: http://localhost:8000/docs

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Service + NML path status |
| POST | `/api/reload` | Re-parse the NML from disk |
| GET | `/api/stats` | Totals, rating breakdown, BPM histogram, metadata gaps |
| GET | `/api/facets` | Distinct genres/keys + BPM range (for filter UI) |
| GET | `/api/tracks` | Paginated track list — see query params below |
| GET | `/api/playlists` | Playlist tree (folders / playlists / smart playlists) |
| GET | `/api/playlists/{id}/tracks` | Tracks in a playlist, joined to full metadata |

### `GET /api/tracks` query params

`q` (search artist/title/album), `genre`, `key`, `bpm_min`, `bpm_max`,
`rating_min` (0–5), `has_cues` (bool), `sort`
(`artist|title|album|genre|key|bpm|rating|playcount|import_date|length`),
`order` (`asc|desc`), `limit` (1–1000, default 100), `offset`.

Returns `{ total, offset, limit, items: [Track] }`.
