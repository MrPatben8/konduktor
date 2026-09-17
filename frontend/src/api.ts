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
  /** The platform's own display string, shown verbatim: "10m", "8A", "Am". */
  key: string | null
  /** Camelot wheel position 1-12 + mode, parsed by the ADAPTER — notation is
   *  platform knowledge, not display formatting. Null when unparseable. */
  key_wheel: number | null
  key_mode: 'major' | 'minor' | null
  rating: number
  playcount: number | null
  length: number | null
  bitrate: number | null
  /** ISO-8601 "YYYY-MM-DD", normalised by the adapter. */
  import_date: string | null
  last_played: string | null
  release_date: string | null
  filepath: string | null
  cue_count: number
  hotcue_count: number
  /** 0 = no beatgrid, 1 = constant tempo, >1 = a flexible (multi-tempo) grid. */
  grid_marker_count: number
  grid_locked: boolean
  media_kind: MediaKind
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

export interface PlaylistNode {
  /** Opaque, adapter-minted. The UI never constructs, parses or prefixes it. */
  id: string
  name: string
  /** For choosing an icon only — every behavioural question is answered by the
   *  flags below, so the UI never infers what a node can do from its kind. */
  kind: PlaylistKind
  count: number
  children: PlaylistNode[]
  selectable: boolean
  can_add_tracks: boolean
  can_reorder: boolean
  can_rename: boolean
  can_delete: boolean
  can_contain_children: boolean
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

export type Platform = string
export type CueRole = 'hotcue' | 'memory'
export type CueType = 'cue' | 'fade_in' | 'fade_out' | 'load' | 'loop'
export type MediaKind = 'audio' | 'stem' | 'video'
export type PlaylistKind = 'folder' | 'playlist' | 'smart'
export type TrackField =
  | 'title' | 'artist' | 'album' | 'genre' | 'label' | 'remixer'
  | 'producer' | 'mix' | 'release_date' | 'comment' | 'rating'

export interface CuePoint {
  name: string | null
  type: CueType
  /** Memory cues have no bank slot. Only Rekordbox has them, so they are
   *  preserved and shown but not editable anywhere yet. */
  role: CueRole
  start: number // seconds
  length: number // seconds (>0 for loops)
  slot: number | null // bank slot; null for a cue not in a bank
  color: string | null // "#RRGGBB" as the platform stored it
  /** False when the adapter refuses commands on this cue. Gate on THIS, never
   *  on a platform-specific reason like grid_marker. */
  editable: boolean
  readonly_reason: 'beatgrid_companion' | 'platform_managed' | null
  /** Index of the grid marker this cue mirrors, where the platform pairs them.
   *  A display hint only; null on platforms that do not. */
  grid_marker: number | null
}

/** One beatgrid marker. Its position in the list IS its index/identity. */
export interface GridMarker {
  start: number // seconds
  bpm: number // governs from this marker until the next one
  name: string | null // Traktor's label — display only, NOT a discriminator
  companion: number | null // hotcue slot of the paired white cue, if any
}

export interface TrackCues {
  /**
   * The beatgrid, ordered by start. Empty = no grid, one marker = constant
   * tempo, more = a flexible (multi-tempo) grid. There is deliberately no
   * scalar bpm/anchor: assuming a single tempo is what made flexible grids
   * render and edit wrongly.
   */
  grid_markers: GridMarker[]
  grid_locked: boolean
  cues: CuePoint[]
}

export interface CueCapabilities {
  hotcue_slots: number
  slot_labels: 'number' | 'letter'
  memory_cues: boolean
  max_memory_cues: number | null
  types: CueType[]
  color: 'none' | 'free' | 'palette'
  palette: string[]
  named: boolean
  loops: 'none' | 'cue_type' | 'separate_bank'
  loop_slots: number | null
}

export interface SaveCapabilities {
  /** Structured facts, never finished sentences — the UI composes the wording
   *  (see lib/platformCopy.ts) so it stays specific without being hard-coded. */
  app_name: string
  library_label: string
  overwrite_risk: 'none' | 'on_exit' | 'while_running'
  history: boolean
}

export interface Capabilities {
  platform: Platform
  version: string | null
  cues: CueCapabilities
  grid: { editable: boolean; flexible: boolean; lockable: boolean }
  tracks: {
    rating_max: number
    editable_fields: TrackField[]
    media_kinds: MediaKind[]
    artwork: boolean
    artwork_note: string | null
  }
  playlists: { folders: boolean; smart: 'none' | 'read_only'; reorder: boolean }
  save: SaveCapabilities
}

export interface LibraryInfo {
  platform: Platform
  name: string // the DJ app's name, e.g. "Traktor"
  library_label: string // e.g. "collection.nml"
  path: string
  display_name: string
  version: string | null
}

export interface PlatformOption {
  platform: Platform
  name: string
  library_label: string
  selects: 'file' | 'directory'
  installed: boolean
}

export interface EditState {
  dirty: boolean
  library: LibraryInfo
}

export interface CollectionStatus {
  loaded: boolean
  path: string | null
  library: LibraryInfo | null
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
  commit: string | null
  playlists: number
}

export interface HistoryEntry {
  id: string
  timestamp: string
  summary: string
}

export interface PathMapping {
  from: string
  to: string
}

export interface RemapSample {
  from: string
  to: string
  exists: boolean
}

export interface RemapPreview {
  total: number
  matched: number
  existing: number
  samples: RemapSample[]
}

export interface RemapResult {
  rewritten: number
  commit: string | null
}

export interface PrefixSuggestions {
  primary: string
  groups: { prefix: string; count: number }[]
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
  collection: () => getJSON<CollectionStatus>('/api/library'),
  openCollection: (path: string) =>
    send<CollectionStatus>('POST', '/api/library/open', { path }),
  collectionOptions: () => getJSON<CollectionOptions>('/api/library/options'),
  listDir: (path?: string) =>
    getJSON<FsListing>(`/api/fs/list${path ? `?path=${encodeURIComponent(path)}` : ''}`),

  // ---- path remapping (per-collection OS-path prefix translation) ----
  getPathMapping: () => getJSON<PathMapping>('/api/library/path-mapping'),
  suggestPrefix: () =>
    getJSON<PrefixSuggestions>('/api/library/path-mapping/suggest'),
  putPathMapping: (m: PathMapping) =>
    send<PathMapping>('PUT', '/api/library/path-mapping', m),
  previewRemap: (from: string, to: string) =>
    getJSON<RemapPreview>(
      `/api/library/path-mapping/preview?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`,
    ),
  // Write-back: permanently rewrite matching LOCATIONs in the .nml (committed to history).
  remapPaths: (from: string, to: string) =>
    send<RemapResult>('POST', '/api/library/remap-paths', { from, to }),

  capabilities: () => getJSON<Capabilities>('/api/capabilities'),
  platforms: () => getJSON<PlatformOption[]>('/api/platforms'),
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
  renamePlaylist: (nodeId: string, name: string) =>
    send<{ status: string }>('PATCH', `/api/playlists/${encodeURIComponent(nodeId)}`, { name }),
  deletePlaylist: (nodeId: string) =>
    send<{ status: string }>('DELETE', `/api/playlists/${encodeURIComponent(nodeId)}`),
  setEntries: (nodeId: string, trackIds: string[]) =>
    send<{ status: string; count: number }>('PUT', `/api/playlists/${encodeURIComponent(nodeId)}/entries`, {
      track_ids: trackIds,
    }),
  addEntries: (nodeId: string, trackIds: string[]) =>
    send<{ status: string; added: number; count: number }>(
      'POST',
      `/api/playlists/${encodeURIComponent(nodeId)}/add`,
      { track_ids: trackIds },
    ),
  getPrefs: () => getJSON<Record<string, unknown>>('/api/prefs'),
  patchPrefs: (patch: Record<string, unknown>) =>
    send<Record<string, unknown>>('PATCH', '/api/prefs', patch),
  save: () => send<SaveResult>('POST', '/api/save'),

  // ---- version history ----
  history: () => getJSON<HistoryEntry[]>('/api/history'),
  restoreVersion: (id: string) =>
    send<CollectionStatus>('POST', `/api/history/${id}/restore`),
  clearHistory: () => send<{ status: string }>('DELETE', '/api/history'),

  editTrack: (trackId: string, fields: Record<string, string | number | null>) =>
    send<{ status: string }>('PATCH', '/api/tracks', { track_id: trackId, fields }),
  artUrl: (trackId: string) =>
    `${API_BASE}/api/tracks/art?track_id=${encodeURIComponent(trackId)}`,
  audioUrl: (trackId: string) =>
    `${API_BASE}/api/tracks/audio?track_id=${encodeURIComponent(trackId)}`,
  trackCues: (trackId: string) =>
    getJSON<TrackCues>(`/api/tracks/cues?track_id=${encodeURIComponent(trackId)}`),
  createCue: (
    trackId: string,
    slot: number,
    start: number,
    type: CueType,
    length = 0,
    name?: string,
  ) =>
    send<TrackCues>('POST', '/api/tracks/cue', {
      track_id: trackId,
      slot,
      start,
      type,
      length,
      name,
    }),
  // Backend analyses the audio and places structural hotcues into empty slots.
  autoCues: (trackId: string, maxCues?: number) =>
    send<TrackCues>('POST', '/api/tracks/cue/auto', {
      track_id: trackId,
      max_cues: maxCues,
    }),
  // Backend detects tempo + first beat: sets BPM, hotcue 1, and grid anchor.
  autoGrid: (trackId: string) =>
    send<TrackCues>('POST', '/api/tracks/grid/auto', { track_id: trackId }),
  setCueType: (trackId: string, slot: number, type: CueType) =>
    send<TrackCues>('PATCH', '/api/tracks/cue', { track_id: trackId, slot, type }),
  deleteCue: (trackId: string, slot: number) =>
    send<TrackCues>(
      'DELETE',
      `/api/tracks/cue?track_id=${encodeURIComponent(trackId)}&slot=${slot}`,
    ),
  /** Retempo and/or move one marker. `index` is its position in grid_markers;
   *  a move is clamped between its neighbours rather than reordering them. */
  setGridMarker: (trackId: string, index: number, patch: { bpm?: number; start?: number }) =>
    send<TrackCues>('PATCH', '/api/tracks/grid/marker', {
      track_id: trackId,
      index,
      ...patch,
    }),
  /** Add a marker. Omitting `bpm` inherits the tempo governing that position. */
  addGridMarker: (trackId: string, start: number, bpm?: number) =>
    send<TrackCues>('POST', '/api/tracks/grid/marker', { track_id: trackId, start, bpm }),
  deleteGridMarker: (trackId: string, index: number) =>
    send<TrackCues>(
      'DELETE',
      `/api/tracks/grid/marker?track_id=${encodeURIComponent(trackId)}&index=${index}`,
    ),
  /** Replace the whole grid (the deck's Reset). `[]` clears it. */
  replaceGridMarkers: (trackId: string, markers: GridMarker[]) =>
    send<TrackCues>('PUT', '/api/tracks/grid', { track_id: trackId, markers }),
  deleteGrid: (trackId: string) =>
    send<TrackCues>('DELETE', `/api/tracks/grid?track_id=${encodeURIComponent(trackId)}`),
  setGridLock: (trackId: string, locked: boolean) =>
    send<TrackCues>('PATCH', '/api/tracks/grid/lock', { track_id: trackId, locked }),
  uploadArt: async (trackId: string, file: File) => {
    const fd = new FormData()
    fd.append('track_id', trackId)
    fd.append('file', file)
    const res = await fetch(`${API_BASE}/api/tracks/art`, { method: 'PUT', body: fd })
    if (!res.ok) throw new Error(`Cover upload failed (${res.status})`)
    return res.json()
  },
}
