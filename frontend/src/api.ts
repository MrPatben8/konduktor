// Typed client for the Konduktor backend (Phase 1 read-only API).

export interface Track {
  id: string
  artist: string | null
  title: string | null
  album: string | null
  genre: string | null
  label: string | null
  remixer: string | null
  producer: string | null
  mix: string | null
  comment: string | null
  bpm: number | null
  key: string | null
  rating: number
  playcount: number | null
  length: number | null
  bitrate: number | null
  import_date: string | null
  last_played: string | null
  release_date: string | null
  filepath: string | null
  cue_count: number
  hotcue_count: number
  has_grid: boolean
  is_stem: boolean
}

export interface TrackPage {
  total: number
  offset: number
  limit: number
  items: Track[]
}

export interface GenreCount {
  name: string
  count: number
}

export interface Facets {
  genres: GenreCount[]
  keys: GenreCount[]
  bpm_min: number | null
  bpm_max: number | null
  total_tracks: number
}

export interface Stats {
  total_tracks: number
  total_playlists: number
  rated: number
  unrated: number
  missing_key: number
  missing_genre: number
  missing_bpm: number
  no_cues: number
  rating_breakdown: Record<string, number>
  bpm_histogram: { bucket: string; count: number }[]
  top_genres: GenreCount[]
}

export type PlaylistType = 'FOLDER' | 'PLAYLIST' | 'SMARTLIST'

export interface PlaylistNode {
  id: string
  name: string
  type: PlaylistType
  uuid: string | null
  count: number
  children: PlaylistNode[]
}

export type SortField =
  | 'artist'
  | 'title'
  | 'album'
  | 'genre'
  | 'key'
  | 'bpm'
  | 'rating'
  | 'playcount'
  | 'import_date'
  | 'length'

export interface TrackQuery {
  q?: string
  genre?: string
  key?: string
  bpm_min?: number
  bpm_max?: number
  rating_min?: number
  has_cues?: boolean
  sort?: SortField
  order?: 'asc' | 'desc'
  limit?: number
  offset?: number
}

export interface CuePoint {
  name: string | null
  type: number // 0 cue, 1 fade-in, 2 fade-out, 3 load, 4 grid, 5 loop
  start: number // seconds
  length: number // seconds (>0 for loops)
  hotcue: number // -1 if not a hotcue
  color: string | null // "#RRGGBB"
}

export interface TrackCues {
  bpm: number | null
  grid_anchor: number | null // seconds
  locked: boolean
  cues: CuePoint[]
}

export interface EditState {
  dirty: boolean
  nml_path: string
}

export interface CollectionStatus {
  loaded: boolean
  path: string | null
  tracks: number | null
  playlists: number | null
}

export interface CollectionCandidate {
  path: string
  label: string
  version: string | null
  modified: number | null // epoch seconds
  exists: boolean
}

export interface CollectionOptions {
  auto: CollectionCandidate | null // best auto-detected (latest Traktor version)
  recent: CollectionCandidate | null // last opened (may no longer exist)
}

export interface FsEntry {
  name: string
  path: string
}

export interface FsListing {
  path: string
  parent: string | null
  home: string
  dirs: FsEntry[]
  files: FsEntry[]
}

export interface SaveResult {
  saved: boolean
  backup: string | null
  playlists: number
}

// API origin. In the packaged desktop app the frontend is served from
// tauri://localhost, so it can't use relative /api paths — the Tauri shell
// injects window.__KONDUKTOR_API__ (the sidecar's http://127.0.0.1:<port>)
// before any app script runs. In the browser dev server it's undefined, so we
// fall back to relative URLs and let Vite proxy /api to the backend.
export const API_BASE: string =
  (typeof window !== 'undefined' &&
    (window as unknown as { __KONDUKTOR_API__?: string }).__KONDUKTOR_API__) ||
  ''

async function getJSON<T>(url: string): Promise<T> {
  const res = await fetch(API_BASE + url)
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${url}`)
  return res.json() as Promise<T>
}

async function send<T>(method: string, url: string, body?: unknown): Promise<T> {
  const res = await fetch(API_BASE + url, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const j = await res.json()
      if (j.detail) detail = j.detail
    } catch {
      /* ignore */
    }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

function qs(params: Record<string, unknown>): string {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') p.set(k, String(v))
  }
  const s = p.toString()
  return s ? `?${s}` : ''
}

export const api = {
  // ---- collection selection ----
  collection: () => getJSON<CollectionStatus>('/api/collection'),
  openCollection: (path: string) =>
    send<CollectionStatus>('POST', '/api/collection/open', { path }),
  collectionOptions: () => getJSON<CollectionOptions>('/api/collection/options'),
  listDir: (path?: string) =>
    getJSON<FsListing>(`/api/fs/list${path ? `?path=${encodeURIComponent(path)}` : ''}`),

  stats: () => getJSON<Stats>('/api/stats'),
  facets: () => getJSON<Facets>('/api/facets'),
  playlists: () => getJSON<PlaylistNode[]>('/api/playlists'),
  tracks: (query: TrackQuery) =>
    getJSON<TrackPage>(`/api/tracks${qs(query as Record<string, unknown>)}`),
  playlistTracks: (id: string) =>
    getJSON<Track[]>(`/api/playlists/${id}/tracks`),

  // ---- writes ----
  state: () => getJSON<EditState>('/api/state'),
  createPlaylist: (name: string, parentId?: string) =>
    send<PlaylistNode>('POST', '/api/playlists', { name, parent_id: parentId ?? null }),
  renamePlaylist: (uuid: string, name: string) =>
    send<{ status: string }>('PATCH', `/api/playlists/${uuid}`, { name }),
  deletePlaylist: (uuid: string) =>
    send<{ status: string }>('DELETE', `/api/playlists/${uuid}`),
  setEntries: (uuid: string, trackIds: string[]) =>
    send<{ status: string; count: number }>('PUT', `/api/playlists/${uuid}/entries`, {
      track_ids: trackIds,
    }),
  addEntries: (uuid: string, trackIds: string[]) =>
    send<{ status: string; added: number; count: number }>(
      'POST',
      `/api/playlists/${uuid}/add`,
      { track_ids: trackIds },
    ),
  getPrefs: () => getJSON<Record<string, unknown>>('/api/prefs'),
  patchPrefs: (patch: Record<string, unknown>) =>
    send<Record<string, unknown>>('PATCH', '/api/prefs', patch),
  save: () => send<SaveResult>('POST', '/api/save'),
  editTrack: (trackId: string, fields: Record<string, string | number | null>) =>
    send<{ status: string }>('PATCH', '/api/tracks', { track_id: trackId, fields }),
  artUrl: (trackId: string) =>
    `${API_BASE}/api/tracks/art?track_id=${encodeURIComponent(trackId)}`,
  audioUrl: (trackId: string) =>
    `${API_BASE}/api/tracks/audio?track_id=${encodeURIComponent(trackId)}`,
  trackCues: (trackId: string) =>
    getJSON<TrackCues>(`/api/tracks/cues?track_id=${encodeURIComponent(trackId)}`),
  createHotcue: (
    trackId: string,
    slot: number,
    start: number,
    type: number,
    length = 0,
    name?: string,
  ) =>
    send<TrackCues>('POST', '/api/tracks/hotcue', {
      track_id: trackId,
      slot,
      start,
      type,
      length,
      name,
    }),
  // Backend analyses the audio and places structural hotcues into empty slots.
  autoHotcues: (trackId: string, maxCues?: number) =>
    send<TrackCues>('POST', '/api/tracks/auto-hotcues', {
      track_id: trackId,
      max_cues: maxCues,
    }),
  setHotcueType: (trackId: string, slot: number, type: number) =>
    send<TrackCues>('PATCH', '/api/tracks/hotcue', { track_id: trackId, slot, type }),
  deleteHotcue: (trackId: string, slot: number) =>
    send<TrackCues>(
      'DELETE',
      `/api/tracks/hotcue?track_id=${encodeURIComponent(trackId)}&slot=${slot}`,
    ),
  setGrid: (trackId: string, patch: { bpm?: number; anchor?: number }) =>
    send<TrackCues>('PATCH', '/api/tracks/grid', { track_id: trackId, ...patch }),
  deleteGrid: (trackId: string) =>
    send<TrackCues>('DELETE', `/api/tracks/grid?track_id=${encodeURIComponent(trackId)}`),
  setLock: (trackId: string, locked: boolean) =>
    send<TrackCues>('PATCH', '/api/tracks/lock', { track_id: trackId, locked }),
  uploadArt: async (trackId: string, file: File) => {
    const fd = new FormData()
    fd.append('track_id', trackId)
    fd.append('file', file)
    const res = await fetch(`${API_BASE}/api/tracks/art`, { method: 'PUT', body: fd })
    if (!res.ok) throw new Error(`Cover upload failed (${res.status})`)
    return res.json()
  },
}
