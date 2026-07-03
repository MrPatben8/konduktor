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

export interface EditState {
  dirty: boolean
  nml_path: string
}

export interface SaveResult {
  saved: boolean
  backup: string | null
  playlists: number
}

async function getJSON<T>(url: string): Promise<T> {
  const res = await fetch(url)
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${url}`)
  return res.json() as Promise<T>
}

async function send<T>(method: string, url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
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
  save: () => send<SaveResult>('POST', '/api/save'),
}
