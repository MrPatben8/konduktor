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
  readonly_reason: 'platform_managed' | null
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

/** A structural event an Auto Hotcues slot can be bound to (`core/structure.py`
 *  EVENTS; keep in lockstep with `schemas.AutoCueEvent`). */
export type AutoCueEvent =
  | 'first_beat' | 'intro_end'
  | 'build_1' | 'drop_1' | 'breakdown_1'
  | 'build_2' | 'drop_2' | 'breakdown_2'
  | 'build_3' | 'drop_3' | 'breakdown_3'
  | 'outro' | 'last_beat'

export interface AutoCueSlot {
  slot: number
  event: AutoCueEvent
  offset_beats: number
  /** Replace a cue already in this slot. */
  overwrite: boolean
}

/** What happened to one requested slot. `not_found`: the track has no such
 *  event (e.g. no third drop); `out_of_range`: the offset moves it outside the
 *  track; `occupied`: a cue is there and overwrite was off; `protected`: a cue
 *  the adapter will not replace (a grid marker's); `duplicate`: that beat
 *  already has a cue — one kept in the bank (the grid cue, a hand-placed cue)
 *  or a new one in a lower slot — whose slot is `duplicate_of`. */
export type AutoCueStatus =
  | 'placed' | 'not_found' | 'out_of_range' | 'occupied' | 'protected' | 'duplicate'

export interface AutoCueOutcome {
  slot: number
  event: AutoCueEvent
  status: AutoCueStatus
  start: number | null
  name: string | null
  duplicate_of: number | null
}

export interface AutoHotcuesResult {
  cues: TrackCues
  outcomes: AutoCueOutcome[]
}

export interface CueCapabilities {
  /** Whether cues can be created or changed. Symmetric with grid.editable: a
   *  platform can be writable overall while its cue store is not implemented. */
  editable: boolean
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

/** Why a library cannot be edited. A fact — lib/platformCopy.ts words it. */
export type ReadonlyCause = 'platform_incomplete' | 'cloud_synced'

export interface Capabilities {
  platform: Platform
  version: string | null
  /** Whether ANY edit can be persisted.
   *
   *  Deliberately not the same as every per-feature flag being false: "the
   *  platform has no such feature" and "this library cannot be written at all"
   *  look identical to a UI that only sees the per-feature flags, and silently
   *  inert controls read as a bug. Gate edit affordances on this AND on the
   *  per-feature flag. */
  writable: boolean
  readonly_cause: ReadonlyCause | null
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
  /** Whether picking this platform's library means picking a file or a folder. */
  selects: 'file' | 'directory'
  installed: boolean
  /** How many libraries this platform has right now; `installed` is `found > 0`. */
  found: number
  /** The library lives on plugged-in media, so `found` changes between calls —
   *  and `found === 0` means "nothing plugged in", not "not installed". */
  removable: boolean
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
  auto: CollectionCandidate | null // best auto-detected; the first of `detected`
  /** EVERY library detected for the platform, best first. One platform can
   *  genuinely have several at once — two Traktor versions, two sticks — and
   *  the picker offers the choice rather than anointing one. */
  detected: CollectionCandidate[]
  recent: CollectionCandidate | null // last opened (may no longer exist)
}

// ---- import sources ----
//
// A SOURCE is a library being read FROM — a plugged-in OneLibrary stick whose
// tracks are about to be imported — open alongside the loaded collection. It is
// deliberately NOT a CollectionStatus: a source is never saved, never edited and
// never the fallback when nothing is loaded, and sharing a type would invite
// components to treat the two as interchangeable.

// ---- import ----
//
// Import is the only operation that can run for minutes, so it is the only one
// that is a JOB rather than a request: start it, then poll the job.

export interface ImportRequest {
  /** Folder the audio is copied into. */
  destination: string
  /** Empty track_ids AND empty playlist_ids means the whole drive. */
  track_ids?: string[]
  playlist_ids?: string[]
  /** Playlists land in a folder named this; null keeps them at the root. */
  folder_name?: string | null
}

export interface ImportPreview {
  tracks: number
  importable: number
  /** Titles whose audio file is not there — reported, then skipped. */
  missing: string[]
  /** Titles the collection already has by filename. A warning, not a block. */
  duplicates: string[]
  playlists: string[]
  total_bytes: number
  destination: string | null
  free_bytes: number | null
  enough_space: boolean | null
}

export type JobState = 'running' | 'done' | 'failed' | 'cancelled'

export interface JobStatus {
  id: string
  kind: string
  state: JobState
  /** 0 until the job knows its size — show an indeterminate bar until positive. */
  total: number
  done: number
  message: string
  result: Record<string, unknown> | null
  error: string | null
  started_at: number
  finished_at: number | null
}

export interface SourceCandidate {
  path: string
  /** What a person recognises, e.g. "OneLibrary — Hardy". */
  label: string
  platform: Platform
  /** Null until it is opened; probing every drive would be slow. */
  tracks: number | null
  modified: number | null
}

export interface SourceStatus {
  loaded: boolean
  path: string | null
  label: string | null
  platform: Platform | null
  tracks: number | null
  playlists: number | null
}

// ---- export sets ----
//
// Konduktor's own curation, never written into the user's library, and keyed by
// the library's stable ID rather than its path — so moving a collection does not
// orphan the sets built against it.

export interface ExportSet {
  id: string
  name: string
  /** Platform id of the export TARGET, e.g. "traktor". */
  target: string
  destination: string
  playlist_ids: string[]
  /** LOOSE tracks only — tracks a referenced playlist supplies are not listed. */
  track_ids: string[]
  created: number
  modified: number
}

export interface ExportPlaylist {
  id: string
  name: string
  /** Deleted from the library since it was added to the set. */
  missing: boolean
  count: number
}

/** What a set holds RIGHT NOW. Never cache it: the references are live, so a
 *  playlist edited after curation changes what this returns. */
export interface ExportContents {
  playlists: ExportPlaylist[]
  loose: number
  /** Deduped across playlists and loose tracks: one track, one file copy. */
  tracks: number
  /** Track ids the library no longer has. Shown, never silently dropped. */
  dangling: string[]
  /** Another set already writes here, and an export CLEARS its destination. */
  destination_conflict: string | null
}

/** What an export would do if run now. Computed fresh every time. */
export interface ExportPreview {
  tracks: number
  exportable: number
  missing: string[]
  playlists: string[]
  total_bytes: number
  destination: string | null
  free_bytes: number | null
  enough_space: boolean | null
  /** A FACT, not a sentence — the UI words it. Null when the export can run. */
  blocked: 'destination_not_empty' | 'nothing_to_export' | 'unsupported_target' | null
  /** The destination already holds a Konduktor export, which will be replaced. */
  replacing: boolean
}

export interface FsPlace {
  /** Groups the row and picks its icon. Never a finished sentence. */
  kind: 'home' | 'music' | 'desktop' | 'documents' | 'downloads' | 'volume' | 'library'
  name: string
  path: string
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
  // Both take the chosen platform, because the picker asks for it FIRST: the
  // shortcuts and the browsable files are only meaningful inside that answer.
  collectionOptions: (platform?: string) =>
    getJSON<CollectionOptions>(`/api/library/options${qs({ platform })}`),
  listDir: (path?: string, platform?: string) =>
    getJSON<FsListing>(`/api/fs/list${qs({ path, platform })}`),
  // Re-asked rather than cached: drives come and go while a dialog is open.
  places: (platform?: string) => getJSON<FsPlace[]>(`/api/fs/places${qs({ platform })}`),

  // ---- export sets ----
  //
  // All scoped to the LOADED library on the server, by its stable id, so
  // nothing here has to carry which collection it means.
  exports: () => getJSON<ExportSet[]>('/api/exports'),
  createExport: (body: { name: string; target: string; destination: string }) =>
    send<ExportSet>('POST', '/api/exports', body),
  updateExport: (id: string, body: Partial<Pick<ExportSet, 'name' | 'target' | 'destination'>>) =>
    send<ExportSet>('PATCH', `/api/exports/${encodeURIComponent(id)}`, body),
  deleteExport: (id: string) =>
    send<{ deleted: boolean }>('DELETE', `/api/exports/${encodeURIComponent(id)}`),
  addToExport: (id: string, body: { track_ids?: string[]; playlist_ids?: string[] }) =>
    send<ExportSet>('POST', `/api/exports/${encodeURIComponent(id)}/add`, body),
  // Removes LOOSE tracks and WHOLE playlists. There is deliberately no way to
  // remove one track from a referenced playlist: the reference is live, so that
  // would need an exclusion list — hidden state deciding future exports.
  removeFromExport: (id: string, body: { track_ids?: string[]; playlist_ids?: string[] }) =>
    send<ExportSet>('POST', `/api/exports/${encodeURIComponent(id)}/remove`, body),
  exportContents: (id: string) =>
    getJSON<ExportContents>(`/api/exports/${encodeURIComponent(id)}/contents`),
  exportTracks: (id: string) =>
    getJSON<Track[]>(`/api/exports/${encodeURIComponent(id)}/tracks`),
  // Only the tracks added individually — the "Other" bucket, which becomes its
  // own playlist in the exported library. Not a filter on exportTracks: that one
  // is everything the export would ship, playlists included and deduped.
  exportLooseTracks: (id: string) =>
    getJSON<Track[]>(`/api/exports/${encodeURIComponent(id)}/loose`),
  exportPlaylistTracks: (id: string, playlistId: string) =>
    getJSON<Track[]>(
      `/api/exports/${encodeURIComponent(id)}/playlists/${encodeURIComponent(playlistId)}/tracks`,
    ),
  // Every platform, with `installed` standing for "can be an export target".
  // Unsupported ones are returned too, so the UI shows them disabled with a
  // reason rather than hiding them — an absent option reads as a missing
  // feature, a disabled one reads as a roadmap.
  exportTargets: () => getJSON<PlatformOption[]>('/api/export-targets'),
  exportPreview: (id: string) =>
    send<ExportPreview>('POST', `/api/exports/${encodeURIComponent(id)}/preview`, {}),
  runExport: (id: string) =>
    send<JobStatus>('POST', `/api/exports/${encodeURIComponent(id)}/run`, {}),

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
  // Analyses the track's structure on its beatgrid and places the template:
  // one event (+ offset in beats) per slot. Every slot reports an outcome.
  autoCues: (trackId: string, slots: AutoCueSlot[]) =>
    send<AutoHotcuesResult>('POST', '/api/tracks/cue/auto', {
      track_id: trackId,
      slots,
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
  // ---- import sources ----
  // `sources()` genuinely changes between calls — a stick is whatever is
  // mounted — so callers are expected to poll it rather than read it once.
  sources: () => getJSON<SourceCandidate[]>('/api/sources'),
  source: () => getJSON<SourceStatus>('/api/source'),
  openSource: (path: string) => send<SourceStatus>('POST', '/api/source/open', { path }),
  /** Releases the drive's file handle — without this a stick will not eject. */
  closeSource: () => send<SourceStatus>('DELETE', '/api/source'),
  sourceCapabilities: () => getJSON<Capabilities>('/api/source/capabilities'),
  sourceTracks: (query: TrackQuery = {}) =>
    getJSON<TrackPage>(`/api/source/tracks${qs(query as Record<string, unknown>)}`),
  sourcePlaylists: () => getJSON<PlaylistNode[]>('/api/source/playlists'),
  sourcePlaylistTracks: (id: string) =>
    getJSON<Track[]>(`/api/source/playlists/${encodeURIComponent(id)}/tracks`),
  sourceTrackCues: (trackId: string) =>
    getJSON<TrackCues>(`/api/source/tracks/cues?track_id=${encodeURIComponent(trackId)}`),
  /** Audition a track off the stick before importing it. */
  sourceAudioUrl: (trackId: string) =>
    `${API_BASE}/api/source/tracks/audio?track_id=${encodeURIComponent(trackId)}`,
  // ---- import ----
  importPreview: (body: ImportRequest) => send<ImportPreview>('POST', '/api/import/preview', body),
  /** Starts the job and returns immediately — poll `job()` for progress. */
  startImport: (body: ImportRequest) => send<JobStatus>('POST', '/api/import', body),
  job: (id: string) => getJSON<JobStatus>(`/api/jobs/${encodeURIComponent(id)}`),
  /** A request, not a kill: the job stops at its next checkpoint and cleans up,
   *  so expect `state` to stay 'running' for a moment after this resolves. */
  cancelJob: (id: string) =>
    send<JobStatus>('POST', `/api/jobs/${encodeURIComponent(id)}/cancel`),
  uploadArt: async (trackId: string, file: File) => {
    const fd = new FormData()
    fd.append('track_id', trackId)
    fd.append('file', file)
    const res = await fetch(`${API_BASE}/api/tracks/art`, { method: 'PUT', body: fd })
    if (!res.ok) throw new Error(`Cover upload failed (${res.status})`)
    return res.json()
  },
}
