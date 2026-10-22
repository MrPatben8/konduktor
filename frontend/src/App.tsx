import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ColumnSizingState, SortingState, VisibilityState } from '@tanstack/react-table'
import { CapabilitiesContext, slotLabeller } from './lib/capabilities'
import { readOnlyNotice, removalNote, writeHint } from './lib/platformCopy'
import { invalidateTrackLists } from './lib/trackQueries'
import { addTracksToPlaylist } from './lib/addTracks'
import { isForeign, useDragPayload, useDropTarget, type TracksOrFiles } from './lib/drag'
import { useNativeFileDrop } from './lib/fileDrop'
import { api, type AnalyzeOptions, type AnalyzeResult, type CueBatchResult, type NormalizeOptions, type NormalizeResult, type AutoCueSlot, type PlaylistNode, type StemBatchResult, type Track, type TrackOrigin } from './api'
import { confirmDiscardUnsaved } from './lib/unsaved'
import { askConfirm } from './lib/confirm'
import {
  COLUMN_MENU,
  MASTERING_COLUMNS,
  DEFAULT_COLUMN_ORDER,
  DEFAULT_COLUMN_VISIBILITY,
} from './lib/trackColumns'
import { Sidebar, type Source } from './components/Sidebar'
import { Toolbar, emptyFilters, type Filters } from './components/Toolbar'
import { TrackTable } from './components/TrackTable'
import { StatusBar } from './components/StatusBar'
import { Toast, type ToastMsg } from './components/Toast'
import { CollectionPicker } from './components/CollectionPicker'
import { ContextMenu, type MenuItem } from './components/ContextMenu'
import { EditTagsDialog } from './components/EditTagsDialog'
import { AnalyzeDialog } from './components/AnalyzeDialog'
import { NormalizeDialog } from './components/NormalizeDialog'
import { AutoCueDialog } from './components/AutoCueDialog'
import { ConfirmDialog, type ConfirmRequest } from './components/ConfirmDialog'
import { PathMappingDialog } from './components/PathMappingDialog'
import { RelocateDialog } from './components/RelocateDialog'
import { HistoryPanel } from './components/HistoryPanel'
import { PrepStrip } from './components/PrepStrip'
import { ImportDialog } from './components/ImportDialog'
import { AddFilesDialog, type AddTarget, type ForeignOrigin } from './components/AddFilesDialog'
import { ConvertStemsDialog } from './components/ConvertStemsDialog'
import { StemReportDialog } from './components/StemReportDialog'
import { SetPositionDialog } from './components/SetPositionDialog'
import { Icon } from './lib/icons'
import { usePrefsWriter } from './lib/prefs'

function applyFilters(tracks: Track[], f: Filters): Track[] {
  const q = f.search.trim().toLowerCase()
  const bpmMin = f.bpmMin ? parseFloat(f.bpmMin) : null
  const bpmMax = f.bpmMax ? parseFloat(f.bpmMax) : null
  return tracks.filter((t) => {
    if (q) {
      const hay = [t.artist, t.title, t.album, t.comment, t.comment2].filter(Boolean).join(' ').toLowerCase()
      if (!hay.includes(q)) return false
    }
    if (f.genre && t.genre !== f.genre) return false
    if (f.key && t.key !== f.key) return false
    if (bpmMin != null && (t.bpm == null || t.bpm < bpmMin)) return false
    if (bpmMax != null && (t.bpm == null || t.bpm > bpmMax)) return false
    if (f.ratingMin > 0 && t.rating < f.ratingMin) return false
    if (f.hasCues === 'yes' && t.cue_count === 0) return false
    if (f.hasCues === 'no' && t.cue_count > 0) return false
    return true
  })
}

export default function App() {
  const qc = useQueryClient()
  const [source, setSource] = useState<Source>({ kind: 'all' })
  const [filters, setFilters] = useState<Filters>(emptyFilters)
  const [sorting, setSorting] = useState<SortingState>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [toast, setToast] = useState<ToastMsg | null>(null)
  const [forcePicker, setForcePicker] = useState(false)
  // `ids` is what the menu acts on: the whole selection when the clicked row is
  // part of it, otherwise just that row.
  const [menu, setMenu] = useState<{ track: Track; ids: string[]; x: number; y: number } | null>(null)
  // Right-click on the table header: the column chooser.
  const [headerMenu, setHeaderMenu] = useState<{ x: number; y: number } | null>(null)
  const [editing, setEditing] = useState<Track | null>(null)
  const [showPaths, setShowPaths] = useState(false)
  const [showHistory, setShowHistory] = useState(false)
  const [prepTrack, setPrepTrack] = useState<Track | null>(null)
  const [deckPlaying, setDeckPlaying] = useState(false)
  // Which library the DECK's track came from — not the view's. Browsing to a
  // playlist while a device or folder track is loaded must not switch the deck
  // to endpoints that do not know that track.
  const [prepOrigin, setPrepOrigin] = useState<TrackOrigin>('collection')
  // The view's origin, read by the load callbacks below (defined before it).
  const viewOriginRef = useRef<TrackOrigin>('collection')
  // The browsed folder's rows that ARE collection tracks: collection id → the
  // file's folder id. A ref, for the callbacks below that outlive a render.
  const folderFilesRef = useRef<Record<string, string>>({})
  // Browsed (or dropped) files being added to the collection (and maybe a
  // playlist/export). `note` says what a drop left out.
  const [adding, setAdding] = useState<{ ids: string[]; target: AddTarget | null; note?: string | null } | null>(null)
  // How many dropped items are being read (a dropped folder is searched all
  // the way down, which can take a while): shown until the dialog opens.
  const [readingDrop, setReadingDrop] = useState(0)
  const [playRequest, setPlayRequest] = useState(0) // bump → deck loads & auto-plays
  const [importing, setImporting] = useState(false)
  // Device tracks dropped on a collection playlist / export: Import, with
  // just those tracks and that target.
  const [importDrop, setImportDrop] = useState<{ ids: string[]; target: AddTarget } | null>(null)
  // Batch analysis (grid, or Auto Hotcues): a confirm step, then the running
  // job, polled into the status bar. ONE at a time across both kinds — the
  // backend refuses a second, since a grid run would move the beats a hotcue
  // run is placing cues on.
  // The Analyze dialog's selection, and whose ids they are (a device's or a
  // folder's loose files can only have their loudness measured).
  const [analyzing, setAnalyzing] = useState<
    { ids: string[]; origin: TrackOrigin; preset?: 'loudness' | 'gain' } | null
  >(null)
  const [cueBatch, setCueBatch] = useState<{ ids: string[]; withoutGrid: number } | null>(null)
  const [batchJob, setBatchJob] = useState<{ id: string; kind: 'grid' | 'cues' | 'stems' | 'normalize' } | null>(null)
  const [normalizing, setNormalizing] = useState<string[] | null>(null)
  // Convert to Stems: the dialog's selection, a finished run's Details, and a
  // running engine download (which outlives the dialog — decided).
  const [stemDialog, setStemDialog] = useState<string[] | null>(null)
  // Set Position…: the tracks being moved within the open playlist.
  const [positioning, setPositioning] = useState<string[] | null>(null)
  const [stemReport, setStemReport] = useState<StemBatchResult | null>(null)
  const [engineJob, setEngineJob] = useState<string | null>(null)
  const [batchCancelling, setBatchCancelling] = useState(false)
  const [cuesRefresh, setCuesRefresh] = useState(0) // bump → deck re-reads its cues
  // Every Remove ▸ action (and the playlist Delete key) confirms first.
  const [confirmReq, setConfirmReq] = useState<ConfirmRequest | null>(null)

  const loadTrack = useCallback((t: Track, origin: TrackOrigin = viewOriginRef.current) => {
    setPrepTrack(t)
    // A folder row the collection holds IS the collection track, so the deck
    // loads it as one: its cues and grid, and editable (decided 2026-10-10).
    setPrepOrigin(origin === 'folder' && folderFilesRef.current[t.id] ? 'collection' : origin)
  }, [])
  const playTrack = useCallback(
    (t: Track) => {
      loadTrack(t)
      setPlayRequest((n) => n + 1)
    },
    [loadTrack],
  )

  // ---- configurable library columns (persisted to userprefs.json) ----
  const [columnVisibility, setColumnVisibility] = useState<VisibilityState>(DEFAULT_COLUMN_VISIBILITY)
  const [columnOrder, setColumnOrder] = useState<string[]>(DEFAULT_COLUMN_ORDER)
  const [columnSizing, setColumnSizing] = useState<ColumnSizingState>({})
  const prefsQuery = useQuery({ queryKey: ['prefs'], queryFn: api.getPrefs })
  const hydratedRef = useRef(false)
  const writePrefs = usePrefsWriter()

  const resetColumns = useCallback(() => {
    setColumnVisibility(DEFAULT_COLUMN_VISIBILITY)
    setColumnOrder(DEFAULT_COLUMN_ORDER)
    setColumnSizing({})
  }, [])
  /** One checkbox row of the header's column chooser. */
  const columnToggle = (id: string, label: string) => ({
    label,
    checked: columnVisibility[id] !== false,
    onToggle: () => setColumnVisibility((v) => ({ ...v, [id]: v[id] === false })),
  })

  // Hydrate the saved layout once, tolerating partial / stale (new columns
  // added since) data by merging against the current defaults.
  useEffect(() => {
    if (hydratedRef.current || !prefsQuery.data) return
    hydratedRef.current = true
    const cols = (prefsQuery.data as { columns?: Record<string, unknown> }).columns
    if (!cols) return
    if (cols.visibility) {
      setColumnVisibility({ ...DEFAULT_COLUMN_VISIBILITY, ...(cols.visibility as VisibilityState) })
    }
    if (Array.isArray(cols.order) && cols.order.length) {
      const saved = cols.order as string[]
      const known = new Set(DEFAULT_COLUMN_ORDER)
      setColumnOrder([
        ...saved.filter((id) => known.has(id)),
        ...DEFAULT_COLUMN_ORDER.filter((id) => !saved.includes(id)),
      ])
    }
    if (cols.sizing && typeof cols.sizing === 'object') {
      setColumnSizing(cols.sizing as ColumnSizingState)
    }
  }, [prefsQuery.data])

  // Persist layout changes (debounced, via the shared prefs writer), but not
  // before hydration so we never clobber saved prefs with the initial defaults.
  useEffect(() => {
    if (!hydratedRef.current) return
    writePrefs({
      columns: { visibility: columnVisibility, order: columnOrder, sizing: columnSizing },
    })
  }, [columnVisibility, columnOrder, columnSizing, writePrefs])

  const collection = useQuery({ queryKey: ['collection'], queryFn: api.collection })
  const loaded = collection.data?.loaded ?? false
  // What the loaded library can persist. Never stale within a session, and the
  // app does not render until it resolves — so no first frame can offer an edit
  // the adapter would reject.
  const capabilities = useQuery({
    queryKey: ['capabilities'],
    queryFn: api.capabilities,
    enabled: loaded,
    staleTime: Infinity,
  })
  const save = capabilities.data?.save
  // The open-time missing-files check. The backend searches as the library
  // opens and holds the result until it is answered, then serves nothing — so
  // "ask once per open" is the backend's fact, and refetching cannot re-ask.
  const relocation = useQuery({
    queryKey: ['relocation'],
    queryFn: api.relocation,
    enabled: loaded,
    refetchInterval: (q) => (q.state.data?.scanning ? 1500 : false),
    refetchOnWindowFocus: false,
  })
  // From the adapter, not a path split: a Serato library is a directory.
  const libraryName = collection.data?.library?.display_name ?? null
  const writeHintText = save ? writeHint(save) : 'save to write it to disk'

  const notify = useCallback((kind: ToastMsg['kind'], text: string, action?: ToastMsg['action']) => {
    setToast({ id: Date.now(), kind, text, action })
  }, [])
  const onError = useCallback((msg: string) => notify('error', msg), [notify])

  // Inline single-field edit from a double-clicked table cell. Only offered when
  // the library can take an edit: passing `undefined` makes those cells inert
  // rather than firing a request the adapter will refuse. Defaults to false, so
  // no edit is ever offered before capabilities have resolved.
  const canEdit = capabilities.data?.writable ?? false
  const editField = useCallback(
    (track: Track, field: keyof Track, value: string | number) => {
      api
        .editTrack(track.id, { [field]: value })
        .then(() => {
          invalidateTrackLists(qc)
          qc.invalidateQueries({ queryKey: ['facets'] })
          notify('success', `Updated ${String(field)} — ${writeHintText}`)
        })
        .catch((e) => onError((e as Error).message))
    },
    [qc, notify, onError, writeHintText],
  )

  // The context menu's Colour ▸ — swatches are this library's own (by hex in
  // the menu, sent as the neutral colour name), applied to the whole selection.
  const trackColors = capabilities.data?.tracks.colors ?? []
  const setTrackColor = (ids: string[], hex: string | null) => {
    const swatch = trackColors.find((s) => s.hex === hex) ?? null
    api
      .setTrackColor(ids, swatch?.color ?? null)
      .then(() => {
        invalidateTrackLists(qc)
        const what = swatch ? `Colour set to ${swatch.label}` : 'Colour cleared'
        notify('success', `${what}${ids.length > 1 ? ` on ${ids.length} tracks` : ''} — ${writeHintText}`)
      })
      .catch((e) => onError((e as Error).message))
  }


  // NOTE: all hooks must run on every render (Rules of Hooks). Data queries are
  // gated with `enabled: loaded` so they don't fire before a collection is open;
  // the loading/picker early-returns live AFTER every hook below.
  const allTracks = useQuery({
    queryKey: ['tracks', 'all'],
    queryFn: () => api.tracks({ limit: 20000, sort: 'artist' }),
    enabled: loaded && source.kind === 'all',
  })
  const playlistTracks = useQuery({
    queryKey: ['playlist', source.kind === 'playlist' ? source.id : null],
    queryFn: () => api.playlistTracks((source as { id: string }).id),
    enabled: loaded && source.kind === 'playlist',
  })

  // An export set being browsed. Its tracks ARE collection tracks — an export is
  // a slice of the loaded library, not another library — so these share the
  // collection's capabilities and stay fully editable, unlike a device.
  const viewingExport =
    source.kind === 'export' ||
    source.kind === 'export-playlist' ||
    source.kind === 'export-other'
  const exportTracks = useQuery({
    queryKey: ['export-tracks', source.kind === 'export' ? source.id : null],
    queryFn: () => api.exportTracks((source as { id: string }).id),
    enabled: source.kind === 'export',
  })
  const exportPlaylistTracks = useQuery({
    queryKey: [
      'export-playlist-tracks',
      source.kind === 'export-playlist' ? source.exportId : null,
      source.kind === 'export-playlist' ? source.id : null,
    ],
    queryFn: () =>
      api.exportPlaylistTracks(
        (source as { exportId: string }).exportId,
        (source as { id: string }).id,
      ),
    enabled: source.kind === 'export-playlist',
  })
  // The loose tracks only. A separate query, not a filter on the root view: the
  // root is everything the export would ship, which is a different answer.
  const exportLooseTracks = useQuery({
    queryKey: ['export-loose', source.kind === 'export-other' ? source.exportId : null],
    queryFn: () => api.exportLooseTracks((source as { exportId: string }).exportId),
    enabled: source.kind === 'export-other',
  })

  // A browsed device. Separate queries and separate cache keys from the
  // collection's: the two libraries can hold tracks with identical ids, and one
  // shared key would serve a stick's track as if it were a collection track.
  const viewingDevice = source.kind === 'device' || source.kind === 'device-playlist'
  const deviceTracks = useQuery({
    queryKey: ['source-tracks', 'all'],
    queryFn: () => api.sourceTracks({ limit: 20000, sort: 'artist' }),
    enabled: viewingDevice && source.kind === 'device',
  })
  const devicePlaylistTracks = useQuery({
    queryKey: ['source-tracks', source.kind === 'device-playlist' ? source.id : null],
    queryFn: () => api.sourcePlaylistTracks((source as { id: string }).id),
    enabled: source.kind === 'device-playlist',
  })
  // The device's own capabilities — read-only — provided to the browsing half of
  // the UI below. That is what makes every existing gate correct here without a
  // single component asking whether it is looking at a device: inline editing,
  // Edit Tags, the rating stars and all twelve deck edit handlers already gate
  // on capabilities, and a device's say `writable: false`.
  const deviceCaps = useQuery({
    queryKey: ['source-capabilities'],
    queryFn: api.sourceCapabilities,
    enabled: viewingDevice || prepOrigin === 'device',
    staleTime: Infinity,
  })

  // A browsed folder of loose files. Read-only like a device, for the same
  // reason: its track ids (absolute paths) are not collection tracks.
  const viewingFolder = source.kind === 'folder'
  const folderPath = source.kind === 'folder' ? source.path : null
  const folderView = useQuery({
    queryKey: ['folder-tracks', folderPath],
    queryFn: () => api.folderTracks(folderPath!),
    enabled: loaded && viewingFolder,
  })
  const folderCaps = useQuery({
    queryKey: ['folder-capabilities', folderPath],
    queryFn: () => api.folderCapabilities(folderPath!),
    enabled: loaded && viewingFolder,
    staleTime: Infinity,
  })
  const heldInFolder = useMemo(
    () => new Set(folderView.data?.in_collection ?? []),
    [folderView.data],
  )
  const folderFiles = useMemo(() => folderView.data?.files ?? {}, [folderView.data])
  folderFilesRef.current = viewingFolder ? folderFiles : {}
  // Adding speaks in FOLDER ids; a row the collection holds carries its
  // collection id, and maps back (the add dialog then reports it as held).
  const toFolderIds = (ids: string[]) => ids.map((id) => folderFiles[id] ?? id)
  viewOriginRef.current = viewingDevice ? 'device' : viewingFolder ? 'folder' : 'collection'
  // Tracks in view belong to ANOTHER library (a device) or to none (a folder),
  // so no collection command may be offered on them: their ids mean nothing to
  // the collection's routes.
  const viewForeign = viewingDevice || viewingFolder
  const openDevice = useQuery({ queryKey: ['source'], queryFn: api.source })
  const exportSets = useQuery({ queryKey: ['exports'], queryFn: api.exports, enabled: loaded })
  const playlists = useQuery({ queryKey: ['playlists'], queryFn: api.playlists, enabled: loaded })
  // Every playlist the context menu's "Add to" can target, flattened out of
  // folders. Each node carries its own `can_add_tracks`, so a read-only library
  // simply offers none.
  const addablePlaylists = useMemo(() => {
    const walk = (nodes: PlaylistNode[]): PlaylistNode[] =>
      nodes.flatMap((n) => [...(n.can_add_tracks ? [n] : []), ...walk(n.children)])
    return walk(playlists.data ?? [])
  }, [playlists.data])
  // Every playlist Remove ▸ From playlist can take tracks out of: the same
  // gate as removing from the open one (its entry list is rewritten).
  const removablePlaylists = useMemo(() => {
    const walk = (nodes: PlaylistNode[]): PlaylistNode[] =>
      nodes.flatMap((n) => [...(n.can_reorder ? [n] : []), ...walk(n.children)])
    return canEdit ? walk(playlists.data ?? []) : []
  }, [playlists.data, canEdit])

  // Export membership. Both are no-ops on the library itself, so neither dirties
  // it nor touches version history — an export set is Konduktor's own data.
  const refreshExport = useCallback(
    (id: string) => {
      qc.invalidateQueries({ queryKey: ['export-contents', id] })
      qc.invalidateQueries({ queryKey: ['export-tracks', id] })
      qc.invalidateQueries({ queryKey: ['export-loose', id] })
    },
    [qc],
  )
  const addToExport = useCallback(
    async (id: string, ids: string[]) => {
      try {
        await api.addToExport(id, { track_ids: ids })
        refreshExport(id)
        const name = exportSets.data?.find((s) => s.id === id)?.name ?? 'export'
        notify('success', `Added ${ids.length} track${ids.length === 1 ? '' : 's'} to ${name}`)
      } catch (e) {
        onError((e as Error).message)
      }
    },
    [refreshExport, exportSets.data, notify, onError],
  )
  // The same path as a drop on a sidebar playlist (lib/addTracks.ts), so the
  // menu and the drag ask about duplicates alike.
  const duplicatesAllowed = !!capabilities.data?.playlists.duplicates
  const addToPlaylist = useCallback(
    async (uuid: string, ids: string[], index: number | null = null) => {
      try {
        const name = addablePlaylists.find((p) => p.id === uuid)?.name ?? 'playlist'
        const msg = await addTracksToPlaylist({ qc, playlist: { id: uuid, name }, ids, duplicatesAllowed, index })
        if (msg) notify('success', msg)
      } catch (e) {
        onError((e as Error).message)
      }
    },
    [qc, addablePlaylists, duplicatesAllowed, notify, onError],
  )
  // Tracks from a device or a Files folder dropped on a collection playlist or
  // export: they are not in the collection yet, so the drop opens the dialog
  // that adds them — with that target filled in, and copy-vs-reference still
  // asked (decided 2026-10-06).
  // Files dragged in from Finder / Explorer (lib/fileDrop.ts). What is in them
  // is read first — folders all the way down — and then they go where a
  // browsed folder's files go: the add dialog, copy-vs-reference asked.
  const readDrop = useCallback(
    async (paths: string[]) => {
      setReadingDrop(paths.length)
      try {
        return await api.folderDropped(paths)
      } catch (e) {
        onError((e as Error).message)
        return null
      } finally {
        setReadingDrop(0)
      }
    },
    [onError],
  )
  const dropFiles = useCallback(
    async (paths: string[], target: AddTarget | null) => {
      const r = await readDrop(paths)
      if (!r) return
      if (!r.tracks.length) {
        onError(
          paths.length === 1 && r.skipped
            ? 'That is not an audio file this library can hold.'
            : 'No audio files this library can hold were found there.',
        )
        return
      }
      const notes: string[] = []
      if (r.skipped)
        notes.push(
          r.skipped === 1
            ? '1 dropped item is not audio this library can hold and was left out.'
            : `${r.skipped} dropped items are not audio this library can hold and were left out.`,
        )
      if (r.truncated) notes.push(`Only the first ${r.tracks.length.toLocaleString()} files are included.`)
      // Files the collection holds are its tracks already: as with a folder's
      // rows, nothing to add first — straight into the target, if there is one.
      const held = r.tracks.filter((t) => r.files[t.id]).map((t) => t.id)
      if (held.length === r.tracks.length) {
        if (target?.kind === 'playlist') void addToPlaylist(target.id, held, target.index)
        else if (target) void addToExport(target.id, held)
        else
          notify(
            'success',
            held.length === 1 ? 'Already in your collection' : `All ${held.length} are already in your collection`,
          )
        return
      }
      setAdding({ ids: r.tracks.map((t) => r.files[t.id] ?? t.id), target, note: notes.join(' ') || null })
    },
    [readDrop, addToPlaylist, addToExport, notify, onError],
  )
  // One dropped audio file on the deck: loaded to listen to, added to nothing.
  const loadDropped = useCallback(
    async (path: string) => {
      const r = await readDrop([path])
      if (!r) return
      const t = r.tracks[0]
      if (!t) {
        onError('That is not an audio file this library can hold.')
        return
      }
      loadTrack(t, r.files[t.id] ? 'collection' : 'folder')
    },
    [readDrop, loadTrack, onError],
  )
  const addForeign = useCallback(
    (origin: ForeignOrigin, ids: string[], target: AddTarget) => {
      if (origin === 'dropped') void dropFiles(ids, target)
      else if (origin === 'folder') {
        const files = folderFilesRef.current
        // Rows that are all collection tracks already: nothing to add first.
        if (ids.every((id) => files[id])) {
          if (target.kind === 'playlist') void addToPlaylist(target.id, ids, target.index)
          else void addToExport(target.id, ids)
        } else setAdding({ ids: ids.map((id) => files[id] ?? id), target })
      } else if (origin === 'device') setImportDrop({ ids, target })
    },
    [addToPlaylist, addToExport, dropFiles],
  )
  const audioFormats = capabilities.data?.tracks.audio_formats
  const isAudioPath = (path: string) =>
    path.lastIndexOf('.') > 0 && !!audioFormats?.includes(path.slice(path.lastIndexOf('.')).toLowerCase())
  const [deckDropRef, deckOver] = useDropTarget<true>({
    hint: (p) =>
      p.kind === 'tracks' || (p.kind === 'files' && p.paths.length === 1 && isAudioPath(p.paths[0])) ? true : null,
    label: (p) => (p.kind === 'tracks' && p.ids.length > 1 ? `Load “${p.lead.title || 'Untitled'}” into the deck` : 'Load into the deck'),
    onDrop: (p) => {
      if (p.kind === 'tracks') loadTrack(p.lead, p.origin)
      else if (p.kind === 'files') void loadDropped(p.paths[0])
    },
  })
  // Files dropped anywhere no other target takes them: the collection. A
  // library that cannot add tracks still takes the drop, to say why not —
  // silently ignoring it would read as the feature being broken.
  const canAddFiles = (capabilities.data?.writable ?? false) && !!capabilities.data?.tracks.addable
  const cannotAddFiles =
    capabilities.data && !capabilities.data.writable
      ? (readOnlyNotice(capabilities.data) ?? 'This library is read-only.')
      : 'Tracks cannot be added to this library.'
  const [rootDropRef, rootOver] = useDropTarget<true>({
    hint: (p) => (p.kind === 'files' ? true : null),
    label: () => (canAddFiles ? 'Add to your collection…' : null),
    onDrop: (p) => {
      if (p.kind !== 'files') return
      if (canAddFiles) void dropFiles(p.paths, null)
      else notify('error', cannotAddFiles)
    },
  })
  const draggingFiles = useDragPayload()?.kind === 'files'
  useNativeFileDrop(loaded && !forcePicker, () =>
    notify(
      'warning',
      'Dropping files works in the Konduktor app, not in a browser (it cannot tell the page where a file is). Browse to it under Files in the sidebar instead.',
    ),
  )
  const removeFromExport = useCallback(
    async (id: string, ids: string[]) => {
      try {
        await api.removeFromExport(id, { track_ids: ids })
        refreshExport(id)
      } catch (e) {
        onError((e as Error).message)
      }
    },
    [refreshExport, onError],
  )

  // What the VIEW can do, as opposed to what the loaded collection can do. While
  // a device is being browsed these are its read-only capabilities, so the
  // browsing half of the UI gates itself correctly with no new conditions.
  // Falls back to the collection's until the device's resolve, so no frame
  // renders without capabilities at all.
  // Until a device's or folder's own capabilities arrive, assume READ-ONLY
  // rather than falling back to the collection's. The fallback is the safe
  // direction on purpose: guessing "writable" for one frame would offer an edit
  // that, if taken, would be sent to the wrong library entirely.
  const readOnlyCaps = (cause: 'platform_incomplete' | 'not_in_library') =>
    capabilities.data ? { ...capabilities.data, writable: false, readonly_cause: cause } : capabilities.data!
  const capsFor = (origin: TrackOrigin) =>
    origin === 'device'
      ? (deviceCaps.data ?? readOnlyCaps('platform_incomplete'))
      : origin === 'folder'
        ? (folderCaps.data ?? readOnlyCaps('not_in_library'))
        : capabilities.data!
  // A folder's table runs on the COLLECTION's: the rows it holds are collection
  // tracks and edit as such, and a loose row asks to be added first
  // (`folderMayEdit`) rather than rendering inert.
  const viewCaps = viewingFolder ? capabilities.data! : capsFor(viewOriginRef.current)
  // The deck follows its TRACK's library, not the view's.
  const deckCaps = capsFor(prepOrigin)

  const deviceLabel = openDevice.data?.label ?? 'device'
  // One name for whatever is on screen, so the header and the status bar cannot
  // disagree about what the user is looking at.
  const viewName =
    source.kind === 'all'
      ? 'All Tracks'
      : source.kind === 'device'
        ? deviceLabel
        : source.name

  const isAll = source.kind === 'all'
  const listed: Track[] = viewingFolder
    ? (folderView.data?.tracks ?? [])
    : viewingDevice
    ? source.kind === 'device'
      ? (deviceTracks.data?.items ?? [])
      : (devicePlaylistTracks.data ?? [])
    : viewingExport
      ? source.kind === 'export'
        ? (exportTracks.data ?? [])
        : source.kind === 'export-other'
          ? (exportLooseTracks.data ?? [])
          : (exportPlaylistTracks.data ?? [])
      : isAll
        ? (allTracks.data?.items ?? [])
        : (playlistTracks.data ?? [])
  // The Mastering columns' values: Konduktor's own store, keyed by the FILE
  // (`/api/loudness`), joined into the rows — and only fetched while one of
  // those columns is shown, since it stats every listed file. Keyed by a hash
  // of the ids, so a list whose membership changes asks again; an analysis
  // invalidates `['loudness']` itself.
  const viewOrigin: TrackOrigin = viewingFolder ? 'folder' : viewingDevice ? 'device' : 'collection'
  const masteringShown = MASTERING_COLUMNS.some((id) => columnVisibility[id] === true)
  const listedIds = useMemo(() => listed.map((t) => t.id), [listed])
  const listedKey = useMemo(() => {
    let h = 5381
    for (const id of listedIds) for (let i = 0; i < id.length; i++) h = ((h << 5) + h + id.charCodeAt(i)) | 0
    return `${listedIds.length}:${h}`
  }, [listedIds])
  const loudness = useQuery({
    queryKey: ['loudness', viewOrigin, listedKey],
    queryFn: () => api.loudness(viewOrigin, listedIds),
    enabled: masteringShown && listedIds.length > 0,
    placeholderData: keepPreviousData,
  })
  const tracks: Track[] = useMemo(() => {
    const values = masteringShown ? loudness.data?.values : undefined
    if (!values) return listed
    return listed.map((t) => (values[t.id] ? { ...t, loudness: values[t.id] } : t))
  }, [listed, loudness.data, masteringShown])
  // The deck holds a SNAPSHOT of its track, taken when it was loaded. When the
  // view it came from refetches (Analyze wrote its key, Edit Tags its title),
  // follow the fresh copy so the deck's KEY readout and header are current.
  // PrepStrip keys its audio on the id, so a new object for the same id
  // reloads nothing.
  useEffect(() => {
    if (!prepTrack) return
    const fresh = tracks.find((t) => t.id === prepTrack.id)
    if (!fresh) return
    const origin =
      viewOriginRef.current === 'folder' && folderFilesRef.current[fresh.id] ? 'collection' : viewOriginRef.current
    if (origin === prepOrigin && JSON.stringify(fresh) !== JSON.stringify(prepTrack)) setPrepTrack(fresh)
    // Only a refetch of the list matters; prepTrack is read, not tracked.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tracks])

  const loading = viewingFolder
    ? folderView.isLoading
    : viewingDevice
    ? source.kind === 'device'
      ? deviceTracks.isLoading
      : devicePlaylistTracks.isLoading
    : viewingExport
      ? source.kind === 'export'
        ? exportTracks.isLoading
        : source.kind === 'export-other'
          ? exportLooseTracks.isLoading
          : exportPlaylistTracks.isLoading
      : isAll
        ? allTracks.isLoading
        : playlistTracks.isLoading
  // Search / filters apply to both the library and playlists (the toolbar is
  // always visible). In a playlist a filtered view disables drag-reorder — see
  // `canReorder` below — so a partial order can't overwrite the full entry list.
  const filtered = useMemo(() => applyFilters(tracks, filters), [tracks, filters])
  const filtersActive =
    filters.search !== '' ||
    filters.genre !== '' ||
    filters.key !== '' ||
    filters.bpmMin !== '' ||
    filters.bpmMax !== '' ||
    filters.ratingMin > 0 ||
    filters.hasCues !== 'any'

  // Playlist reorder / remove: persist the new entry order via setEntries, and
  // optimistically update the cached playlist so the list stays put after a drag
  // (the TrackTable re-syncs to this on the next render).
  const reorderPlaylist = useCallback(
    (ids: string[]) => {
      if (source.kind !== 'playlist') return
      const id = source.id
      qc.setQueryData<Track[]>(['playlist', id], (prev) => {
        if (!prev) return prev
        const byId = new Map(prev.map((t) => [t.id, t]))
        return ids.map((i) => byId.get(i)).filter((t): t is Track => !!t)
      })
      api
        .setEntries(id, ids)
        .then(() => {
          qc.invalidateQueries({ queryKey: ['state'] })
          qc.invalidateQueries({ queryKey: ['playlists'] }) // refresh counts
        })
        .catch((e) => onError((e as Error).message))
    },
    [source, qc, onError],
  )
  const removeFromPlaylist = useCallback(
    (ids: string[]) => {
      const gone = new Set(ids)
      reorderPlaylist(tracks.filter((t) => !gone.has(t.id)).map((t) => t.id))
    },
    [reorderPlaylist, tracks],
  )
  // The open playlist's node, for its own flags: a smart playlist has a track
  // list but no manual order, and each platform says so per node.
  const playlistNode = useMemo(() => {
    if (source.kind !== 'playlist') return null
    const find = (nodes: PlaylistNode[]): PlaylistNode | null => {
      for (const n of nodes) {
        if (n.id === source.id) return n
        const hit = find(n.children)
        if (hit) return hit
      }
      return null
    }
    return find(playlists.data ?? [])
  }, [source, playlists.data])
  const playlistEditable = canEdit && !!playlistNode?.can_reorder
  // Files, a Files folder's rows or a device's dropped on the open playlist's
  // table: added to the collection and — unless the dialog's tick is cleared —
  // into this playlist at the insertion line (decided 2026-10-17). A library
  // that cannot add tracks leaves the drop to App's root target, which says why.
  const playlistDrop = useMemo(() => {
    if (!playlistEditable || !canAddFiles || source.kind !== 'playlist' || !playlistNode?.can_add_tracks) return undefined
    const { id, name } = playlistNode
    return {
      label: `Add to ${name}…`,
      onDrop: (p: TracksOrFiles, index: number | null) => {
        const target: AddTarget = { kind: 'playlist', id, name, index }
        if (p.kind === 'files') void dropFiles(p.paths, target)
        else if (p.origin === 'folder' || p.origin === 'device') addForeign(p.origin, p.ids, target)
      },
    }
  }, [playlistEditable, canAddFiles, source, playlistNode, dropFiles, addForeign])
  // The same where no table is drawn: an empty playlist, or filters hiding
  // every row (never both at once, so they share the one target).
  const [emptyDropRef, emptyDropOver] = useDropTarget<true>({
    hint: (p) => (playlistDrop && isForeign(p) ? true : null),
    label: () => playlistDrop?.label ?? null,
    onDrop: (p) => {
      if (isForeign(p)) playlistDrop?.onDrop(p, null)
    },
  })
  // A playlist's own 1-based positions, so the # column still means "where in
  // the playlist" once the view is sorted or filtered.
  const playlistPositions = useMemo(() => {
    if (source.kind !== 'playlist') return undefined
    const m = new Map<string, number>()
    tracks.forEach((t, i) => !m.has(t.id) && m.set(t.id, i + 1))
    return m
  }, [source.kind, tracks])

  const batchJobStatus = useQuery({
    queryKey: ['job', batchJob?.id ?? null],
    queryFn: () => api.job(batchJob!.id),
    enabled: !!batchJob,
    refetchInterval: (q) => (q.state.data && q.state.data.state !== 'running' ? false : 400),
  })
  const batchDone = batchJobStatus.data?.done ?? 0
  useEffect(() => {
    // Each finished track is an unsaved edit; let the Save button know as they land.
    if (batchDone > 0) qc.invalidateQueries({ queryKey: ['state'] })
  }, [batchDone, qc])
  useEffect(() => {
    const job = batchJobStatus.data
    if (!job || job.state === 'running' || !batchJob || job.id !== batchJob.id) return
    const kind = batchJob.kind
    setBatchJob(null)
    setBatchCancelling(false)
    if (kind === 'stems') {
      finishStemBatch(job.state, job.result as unknown as StemBatchResult | null, job.error)
      return
    }
    if (job.state === 'failed') {
      const what = kind === 'grid' ? 'Analysis' : kind === 'normalize' ? 'Normalize' : 'Auto Hotcues'
      onError(`${what} failed: ${job.error ?? 'unknown error'}`)
      return
    }
    if (!job.result) return
    invalidateTrackLists(qc)
    qc.invalidateQueries({ queryKey: ['facets'] })
    qc.invalidateQueries({ queryKey: ['loudness'] })
    const cancelled = job.state === 'cancelled'
    const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? '' : 's'}`
    let touched: string[]
    let parts: string[]
    let failures: CueBatchResult['failed']
    if (kind === 'normalize') {
      const r = job.result as unknown as NormalizeResult
      touched = r.normalised
      failures = r.failed
      qc.invalidateQueries({ queryKey: ['state'] })
      parts = [
        `${cancelled ? 'Cancelled — normalized' : 'Normalized'} ${plural(r.normalised.length, 'track')}` +
          (r.normalised.length ? ' — Save to keep the new levels, Discard to undo' : ''),
        r.skipped.length ? `${r.skipped.length} left as they were` : '',
      ]
    } else if (kind === 'grid') {
      const r = job.result as unknown as AnalyzeResult
      touched = r.analysed
      failures = r.failed
      const g = r.grid
      const wrote = [
        g.full ? plural(g.full, 'grid') : '',
        g.bpm ? `${plural(g.bpm, 'BPM')} (downbeat kept)` : '',
        g.phase ? `${plural(g.phase, 'grid')} at its BPM` : '',
        r.key.set ? plural(r.key.set, 'key') : '',
        r.loudness?.measured ? `loudness of ${r.loudness.measured}` : '',
        r.gain?.set ? `auto gain of ${r.gain.set}` : '',
      ].filter(Boolean)
      const skipped = [
        g.existing ? `${g.existing} with a grid` : '',
        g.locked ? `${g.locked} locked` : '',
        g.flexible ? `${g.flexible} multi-tempo` : '',
        r.key.existing ? `${r.key.existing} with a key` : '',
        r.loudness?.existing ? `${r.loudness.existing} already measured` : '',
        r.gain?.existing ? `${r.gain.existing} with an auto gain` : '',
      ].filter(Boolean)
      parts = [
        `${cancelled ? 'Cancelled — analyzed' : 'Analyzed'} ${plural(r.analysed.length, 'track')}` +
          (wrote.length ? `: ${wrote.join(', ')}` : ''),
        skipped.length ? `skipped ${skipped.join(', ')}` : '',
        r.key.none ? `no key found in ${r.key.none}` : '',
        r.loudness?.none ? `nothing to measure in ${r.loudness.none} (silent or under 3 s)` : '',
        r.gain?.none ? `no auto gain for ${r.gain.none} (silent or under 3 s)` : '',
      ]
    } else {
      const r = job.result as unknown as CueBatchResult
      touched = r.tracks
      failures = r.failed
      parts = [
        `${cancelled ? 'Cancelled — placed' : 'Placed'} ${plural(r.cues_placed, 'cue')} on ${plural(r.tracks.length, 'track')}`,
        r.grids_created ? `analyzed ${plural(r.grids_created, 'new grid')}` : '',
      ]
    }
    if (failures.length) {
      const f = failures[0]
      parts.push(`${failures.length} failed (${f.title}: ${f.reason}${failures.length > 1 ? ', …' : ''})`)
    }
    if (prepTrack && touched.includes(prepTrack.id)) setCuesRefresh((n) => n + 1)
    notify(failures.length && touched.length === 0 ? 'error' : 'success', parts.filter(Boolean).join(' · '))
    // Only the job's finish matters here; prepTrack is read, not tracked.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batchJobStatus.data])

  const handleOpened = () => {
    setForcePicker(false)
    setSource({ kind: 'all' })
    setSelected(new Set())
    setBatchJob(null) // the backend cancels it; its result is for the old library
    // The deck starts empty: its track belongs to the old library.
    setPrepTrack(null)
    setPlayRequest(0)
    setFilters(emptyFilters)
    setSorting([])
    qc.invalidateQueries() // refetch everything for the newly-opened collection
  }

  // "Open for editing…" on a device: the drive becomes THE library — one library,
  // one dirty state, one SaveBar (decided 2026-10-01). Unsaved edits to the
  // current library are confirmed first, as switching library always is, and the
  // browsing copy is closed so the drive is not open twice.
  const openForEditing = async (path: string) => {
    if (!(await confirmDiscardUnsaved())) return
    try {
      await api.closeSource()
      await api.openCollection(path)
      handleOpened()
    } catch (e) {
      onError((e as Error).message)
    }
  }

  // ---- Convert to Stems ------------------------------------------------------
  const canStems = canEdit && !viewingDevice && !!capabilities.data?.tracks.stem_convertible
  const finishStemBatch = (state: string, r: StemBatchResult | null, error: string | null) => {
    qc.invalidateQueries({ queryKey: ['state'] })
    if (state === 'failed' || !r) {
      onError(`Converting to stems failed: ${error ?? 'unknown error'}`)
      return
    }
    if (r.cancelled) {
      notify('warning', 'Conversion cancelled — nothing was changed')
      return
    }
    invalidateTrackLists(qc)
    qc.invalidateQueries({ queryKey: ['playlists'] })
    qc.invalidateQueries({ queryKey: ['export-contents'] })
    qc.invalidateQueries({ queryKey: ['facets'] })
    // A repointed track's id IS its location, so it changed: follow it with the
    // deck and the selection instead of leaving them on an id that is gone.
    const renamed = r.renamed
    if (prepTrack && renamed[prepTrack.id]) {
      setPrepTrack({ ...prepTrack, id: renamed[prepTrack.id], media_kind: 'stem' })
      setCuesRefresh((n) => n + 1)
    }
    setSelected((sel) => new Set([...sel].map((id) => renamed[id] ?? id)))
    const n = r.converted.length
    const parts = [
      `${n} converted to stems`,
      r.skipped.length ? `${r.skipped.length} skipped` : '',
      r.failed.length ? `${r.failed.length} failed` : '',
    ].filter(Boolean)
    const text = parts.join(' · ') + (n ? ' — Save to keep them' : '')
    const details = r.skipped.length || r.failed.length ? { label: 'Details', onClick: () => setStemReport(r) } : undefined
    notify(n ? 'success' : 'error', text, details)
  }
  // A conversion keeps running if the page reloads; find it again.
  const isRemote = !!collection.data?.library?.remote
  const editState = useQuery({
    queryKey: ['state'],
    queryFn: api.state,
    enabled: loaded,
    retry: false,
    // A remote library's connection changes on its own (another computer
    // takes over, the server drops off the network), so it is polled; a local
    // library's state changes only when this app changes it.
    refetchInterval: isRemote ? 5000 : false,
  })
  const remoteState = editState.data?.remote?.state ?? null
  const remoteShown = useRef<string | null>(null)
  useEffect(() => {
    const previous = remoteShown.current
    remoteShown.current = remoteState
    if (!remoteState || previous === remoteState || previous === null) return
    // What the library will accept changed with it: re-read, so the UI gates
    // itself (read-only while taken over or offline) — never offers an edit.
    qc.invalidateQueries({ queryKey: ['capabilities'] })
    const who = editState.data?.remote?.machine
    if (remoteState === 'taken_over')
      notify('warning', `${who || 'Another computer'} took over this library — it is read-only here now`)
    else if (remoteState === 'offline') notify('warning', 'The server cannot be reached — read-only until it is back')
    else if (remoteState === 'connected' && previous !== 'reconnecting') notify('success', 'Reconnected to the server')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [remoteState])
  const remoteNotices = editState.data?.remote?.notices ?? 0
  const noticesShown = useRef(0)
  useEffect(() => {
    if (remoteNotices <= noticesShown.current) return
    noticesShown.current = remoteNotices
    const text = editState.data?.remote?.notice
    if (text) notify('warning', text)
    // Re-read: the server's library is what it is now, not what this tab holds.
    qc.invalidateQueries()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [remoteNotices])
  useEffect(() => {
    const id = editState.data?.stem_job
    if (id && !batchJob) setBatchJob({ id, kind: 'stems' })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editState.data?.stem_job])
  useEffect(() => {
    const id = editState.data?.normalize_job
    if (id && !batchJob) setBatchJob({ id, kind: 'normalize' })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editState.data?.normalize_job])
  // Tell the user once what crash recovery did about an interrupted conversion.
  const recovery = editState.data?.stem_recovery
  const recoveryShown = useRef<unknown>(null)
  useEffect(() => {
    if (!recovery || recoveryShown.current === recovery) return
    recoveryShown.current = recovery
    const bits = [
      recovery.restored ? `${recovery.restored} original${recovery.restored === 1 ? '' : 's'} put back` : '',
      recovery.committed ? `${recovery.committed} saved conversion${recovery.committed === 1 ? '' : 's'} finished` : '',
      recovery.kept ? `${recovery.kept} converted file${recovery.kept === 1 ? '' : 's'} kept for next time` : '',
    ].filter(Boolean)
    if (bits.length) notify('warning', `An interrupted stem conversion was tidied up: ${bits.join(' · ')}`)
  }, [recovery, notify])
  const engineJobStatus = useQuery({
    queryKey: ['job', engineJob],
    queryFn: () => api.job(engineJob!),
    enabled: !!engineJob,
    refetchInterval: (q) => (q.state.data && q.state.data.state !== 'running' ? false : 500),
  })
  useEffect(() => {
    const job = engineJobStatus.data
    if (!job || job.state === 'running' || job.id !== engineJob) return
    setEngineJob(null)
    qc.invalidateQueries({ queryKey: ['stem-engine'] })
    if (job.state === 'done') notify('success', 'The stem engine is installed')
    else if (job.state === 'failed') onError(job.error || 'The stem engine download failed')
  }, [engineJobStatus.data, engineJob, qc, notify, onError])

  // Hooks must all run BEFORE the early returns below (picker / loading),
  // or the first render with a library has more hooks than the last one and
  // React throws — the whole window goes blank.
  if (collection.isLoading || (loaded && !capabilities.data)) {
    return (
      <div className="flex h-screen w-screen items-center justify-center text-muted">
        Loading…
      </div>
    )
  }

  if (!loaded || forcePicker) {
    return (
      <CollectionPicker
        onOpened={handleOpened}
        onCancel={loaded ? () => setForcePicker(false) : undefined}
      />
    )
  }

  const selectSource = (s: Source) => {
    setSource(s)
    setSorting([])
    setSelected(new Set())
  }

  // TrackTable retargets the selection to a right-clicked row outside it
  // before calling this, but that state update has not landed yet — so the
  // same rule is applied here to decide what the menu acts on. The ids come
  // in the order the view shows them (Set Position can keep that order); a
  // selected track the view hides follows.
  const openMenu = (track: Track, x: number, y: number, selectable: boolean, viewIds: string[]) =>
    setMenu({
      track,
      ids:
        selectable && selected.has(track.id)
          ? [...new Set([...viewIds.filter((id) => selected.has(id)), ...selected])]
          : [track.id],
      x,
      y,
    })

  const runAnalysis = async (ids: string[], opts: AnalyzeOptions) => {
    const job = await api.analyze(ids, opts)
    setAnalyzing(null)
    setBatchCancelling(false)
    setBatchJob({ id: job.id, kind: 'grid' })
  }
  // Offered for ANY selection: loudness goes to Konduktor's own store, so it
  // can always be measured (decided 2026-10-19). The dialog disables BPM /
  // Grid / Key with a reason where they cannot be stored.
  const canAnalyzeGrid = !!capabilities.data?.grid.editable
  const canAnalyzeKey = !!capabilities.data?.tracks.key_writable
  const canAutoGain = !!capabilities.data?.tracks.auto_gain
  /** Why Normalize cannot change these tracks' files, if so. */
  const normalizeRefusal = (ids: string[]): string | null =>
    analyzeRefusal(analyzeOrigin(ids)) ??
    (capabilities.data?.tracks.normalizable ? null : "This library's audio files cannot be normalized")
  const analyzeOrigin = (ids: string[]): TrackOrigin =>
    viewingDevice ? 'device' : viewingFolder && ids.some((id) => !folderFiles[id]) ? 'folder' : 'collection'
  /** Why BPM / Grid / Key cannot be analysed for these tracks at all, if so. */
  const analyzeRefusal = (origin: TrackOrigin): string | null =>
    origin === 'device'
      ? 'A device is read-only: only its loudness can be measured'
      : origin === 'folder'
        ? 'Add these files to your collection first'
        : !canEdit
          ? ((capabilities.data && readOnlyNotice(capabilities.data)) ?? 'This library is read-only')
          : null

  const startCueBatch = (ids: string[]) => {
    const byId = new Map(tracks.map((t) => [t.id, t]))
    const withoutGrid = ids.filter((id) => (byId.get(id)?.grid_marker_count ?? 0) === 0).length
    setCueBatch({ ids, withoutGrid })
  }
  // Rejects on failure, so the dialog stays open and shows it can be retried.
  const runCueBatch = async (ids: string[], slots: AutoCueSlot[]) => {
    const job = await api.autoCuesBatch(ids, slots)
    setBatchCancelling(false)
    setBatchJob({ id: job.id, kind: 'cues' })
  }
  const canAutoCue = canEdit && !viewingDevice && (capabilities.data?.cues.hotcue_slots ?? 0) > 0


  // ---- Remove ▸ -----------------------------------------------------------
  const nTracks = (n: number) => `${n} track${n === 1 ? '' : 's'}`
  const trackNoun = (ids: string[]) => {
    if (ids.length > 1) return nTracks(ids.length)
    const t = tracks.find((x) => x.id === ids[0])
    return t?.title ? `“${t.title}”` : 'this track'
  }
  // Refetch whatever a track list or the deck could be showing. Throws on
  // failure (after reporting), so the confirm dialog stays open for a retry.
  const afterBulk = (ids: string[], cuesChanged: boolean) => {
    invalidateTrackLists(qc)
    qc.invalidateQueries({ queryKey: ['playlists'] })
    qc.invalidateQueries({ queryKey: ['export-contents'] })
    qc.invalidateQueries({ queryKey: ['facets'] })
    if (cuesChanged && prepTrack && ids.includes(prepTrack.id)) setCuesRefresh((n) => n + 1)
  }
  const guard = async (fn: () => Promise<void>) => {
    try {
      await fn()
    } catch (e) {
      onError((e as Error).message)
      throw e
    }
  }

  const confirmRemoveFromPlaylist = (ids: string[]) =>
    setConfirmReq({
      title: 'Remove from playlist',
      body: (
        <p>
          Remove {trackNoun(ids)} from <span className="text-text">{playlistNode?.name ?? 'this playlist'}</span>?
          The {ids.length === 1 ? 'track stays' : 'tracks stay'} in the collection.
        </p>
      ),
      confirmLabel: `Remove ${nTracks(ids.length)}`,
      onConfirm: () => {
        removeFromPlaylist(ids)
        setSelected(new Set())
      },
    })

  // Remove ▸ From playlist ▸ <name>: tracks picked anywhere leave a playlist
  // that is not open — every copy of each. Counted first, so the dialog says
  // how many of the selection it actually holds (and still answers at 0).
  const confirmRemoveFromOther = async (p: PlaylistNode, ids: string[]) => {
    let r
    try {
      r = await api.removeEntries(p.id, ids, true)
    } catch (e) {
      onError((e as Error).message)
      return
    }
    const name = <span className="text-text">{p.name}</span>
    const held = r.tracks
    setConfirmReq({
      title: 'Remove from playlist',
      body: !held ? (
        <p>
          {ids.length === 1 ? <>{trackNoun(ids)} is not</> : <>None of these {ids.length} tracks are</>} in {name}.
        </p>
      ) : (
        <>
          <p>
            Remove {held === ids.length ? trackNoun(ids) : `${held} of the ${ids.length} selected tracks`} from {name}?
            The {held === 1 ? 'track stays' : 'tracks stay'} in the collection.
          </p>
          {held < ids.length && (
            <p className="text-faint">The other {nTracks(ids.length - held)} {ids.length - held === 1 ? 'is' : 'are'} not in it.</p>
          )}
          {r.removed > held && <p className="text-faint">Some are in it more than once; every copy goes.</p>}
        </>
      ),
      confirmLabel: held ? `Remove ${nTracks(held)}` : 'Remove',
      confirmDisabled: !held,
      onConfirm: () =>
        guard(async () => {
          const done = await api.removeEntries(p.id, ids)
          qc.invalidateQueries({ queryKey: ['playlist', p.id] })
          qc.invalidateQueries({ queryKey: ['playlists'] })
          qc.invalidateQueries({ queryKey: ['state'] })
          notify('success', `Removed ${nTracks(done.tracks)} from ${p.name}`)
        }),
    })
  }

  const confirmRemoveFromExport = (exportId: string, ids: string[]) => {
    const name = exportSets.data?.find((x) => x.id === exportId)?.name ?? 'this export'
    setConfirmReq({
      title: 'Remove from export',
      body: (
        <p>
          Remove {trackNoun(ids)} from <span className="text-text">{name}</span>? Your library is not
          changed.
        </p>
      ),
      confirmLabel: `Remove ${nTracks(ids.length)}`,
      onConfirm: () =>
        guard(async () => {
          await removeFromExport(exportId, ids)
          setSelected(new Set())
        }),
    })
  }

  const confirmClearGrids = (ids: string[]) => {
    const locked = tracks.filter((t) => ids.includes(t.id) && t.grid_locked).length
    setConfirmReq({
      title: 'Remove beatgrids',
      body: (
        <>
          <p>
            Delete the beatgrid of {trackNoun(ids)}? Tempo and markers go; the paired beat-1 cue goes
            with them. Hotcues are kept.
          </p>
          {locked > 0 && (
            <p className="text-faint">{locked} locked {locked === 1 ? 'grid is' : 'grids are'} kept.</p>
          )}
        </>
      ),
      confirmLabel: 'Remove grids',
      onConfirm: () =>
        guard(async () => {
          const r = await api.clearGrids(ids)
          afterBulk(ids, true)
          notify(
            'success',
            [`Removed ${r.cleared} grid${r.cleared === 1 ? '' : 's'}`, r.locked ? `${r.locked} locked, kept` : '']
              .filter(Boolean)
              .join(' · '),
          )
        }),
    })
  }

  const confirmClearHotcues = (ids: string[]) =>
    setConfirmReq({
      title: 'Remove hotcues',
      body: (
        <p>
          Clear every hotcue on {trackNoun(ids)} — cues, loops and the grid’s beat-1 cue? The beatgrid
          itself is kept.
        </p>
      ),
      confirmLabel: 'Remove hotcues',
      onConfirm: () =>
        guard(async () => {
          const r = await api.clearHotcues(ids)
          afterBulk(ids, true)
          notify('success', `Removed ${r.cues} hotcue${r.cues === 1 ? '' : 's'} from ${nTracks(r.tracks)}`)
        }),
    })

  const confirmRemoveTracks = (ids: string[]) =>
    setConfirmReq({
      title: 'Remove from collection',
      body: (
        <>
          <p>
            Remove {trackNoun(ids)} from the collection and from every playlist?
          </p>
          {capabilities.data && <p className="text-faint">{removalNote(capabilities.data, ids.length)}</p>}
        </>
      ),
      confirmLabel: `Remove ${nTracks(ids.length)}`,
      onConfirm: () =>
        guard(async () => {
          const r = await api.removeTracks(ids)
          setSelected(new Set())
          if (prepTrack && ids.includes(prepTrack.id)) setPrepTrack(null)
          afterBulk(ids, false)
          notify('success', `Removed ${nTracks(r.removed)} from the collection`)
        }),
    })

  const canClearGrids = canEdit && !viewingDevice && !!capabilities.data?.grid.editable
  const canClearCues = canAutoCue
  const canRemoveTracks = canEdit && !viewingDevice && !!capabilities.data?.tracks.removable
  // The COLLECTION's capabilities, deliberately: adding happens while a folder
  // or device is on screen, whose own capabilities are read-only.
  const canAddTracks = canEdit && !!capabilities.data?.tracks.addable

  // In a folder, only the rows the collection holds can be edited. Anything
  // that would edit a loose file says so and offers to add it, rather than
  // being absent or silently inert (decided 2026-10-10).
  const askToAdd = async (ids: string[]) => {
    const loose = ids.filter((id) => !folderFiles[id])
    if (!loose.length) return
    const first = tracks.find((t) => t.id === loose[0])
    const what =
      loose.length === 1
        ? `“${first?.title || 'This file'}” is not in your collection`
        : `${loose.length} of these files are not in your collection`
    if (!canAddTracks) {
      notify('error', `${what}, and tracks cannot be added to this library.`)
      return
    }
    const add = await askConfirm({
      title: 'Add to your collection first',
      body: `${what} yet. Add ${loose.length === 1 ? 'it' : 'them'} to edit tags, cues or the beatgrid.`,
      confirmLabel: 'Add to Collection…',
      tone: 'primary',
    })
    if (add) setAdding({ ids: loose, target: null })
  }
  const folderEditField = (track: Track, field: keyof Track, value: string | number) => {
    if (folderFiles[track.id]) editField(track, field, value)
    else void askToAdd([track.id])
  }
  const folderMayEdit = (track: Track) => {
    if (folderFiles[track.id]) return true
    void askToAdd([track.id])
    return false
  }
  // The open menu's loose files: every editing item then asks to add them.
  const menuLoose = menu && viewingFolder ? menu.ids.filter((id) => !folderFiles[id]) : []
  const gated = (fn: () => void) => (menuLoose.length ? () => void askToAdd(menuLoose) : fn)
  const exportRootId =
    source.kind === 'export' ? source.id : source.kind === 'export-other' ? source.exportId : null
  const removeItems = (ids: string[]): MenuItem[] => [
    ...(playlistEditable
      ? [{ label: 'From this playlist…', onClick: () => confirmRemoveFromPlaylist(ids) }]
      : []),
    // Any other editable playlist — "A minus B" is: open B, select all, here A.
    ...(() => {
      // A device's ids mean nothing to the collection's playlists.
      if (viewingDevice) return []
      const lists = removablePlaylists.filter((p) => !(source.kind === 'playlist' && p.id === source.id))
      return lists.length
        ? [{
            label: 'From playlist',
            submenu: lists.map((p) => ({
              label: p.name,
              hint: String(p.count),
              onClick: () => void confirmRemoveFromOther(p, ids),
            })),
          }]
        : []
    })(),
    // Only on the export's ROOT view. Inside a referenced playlist there is
    // nothing to remove: the reference is live, and per-track removal would
    // need an exclusion list — hidden state deciding what a future export
    // contains. Remove the whole playlist, or add tracks instead.
    ...(exportRootId ? [{ label: 'From this export…', onClick: () => confirmRemoveFromExport(exportRootId, ids) }] : []),
    // Removing touches cues and grids a running batch is writing; the backend
    // refuses, so the items say why instead of failing on click.
    ...(canClearGrids
      ? [{ label: 'Grids…', hint: batchJob ? 'busy' : undefined, disabled: !!batchJob, onClick: () => confirmClearGrids(ids) }]
      : []),
    ...(canClearCues
      ? [{ label: 'Hotcues…', hint: batchJob ? 'busy' : undefined, disabled: !!batchJob, onClick: () => confirmClearHotcues(ids) }]
      : []),
    ...(canRemoveTracks
      ? [{ label: 'From collection…', danger: true, hint: batchJob ? 'busy' : undefined, disabled: !!batchJob, onClick: () => confirmRemoveTracks(ids) }]
      : []),
  ]

  // The "Add to" submenu. Exports are listed even on a read-only library:
  // adding to one touches no library data — an export is Konduktor's own
  // curation. The playlist being viewed is left out; adding a track to the
  // playlist it came from would only duplicate it.
  const addToItems = (ids: string[]): MenuItem[] => {
    const lists = addablePlaylists.filter((p) => !(source.kind === 'playlist' && p.id === source.id))
    return [
      { heading: 'Playlists', empty: lists.length ? undefined : 'No playlists' },
      ...lists.map((p) => ({
        label: p.name,
        hint: String(p.count),
        onClick: () => addToPlaylist(p.id, ids),
      })),
      { heading: 'Exports', empty: exportSets.data?.length ? undefined : 'No exports yet' },
      ...(exportSets.data ?? []).map((set) => ({
        label: set.name,
        icon: <Icon name="export" size={13} />,
        onClick: () => addToExport(set.id, ids),
      })),
    ]
  }

  // The same targets as "Add to", but each opens the add dialog: a loose file
  // has to join the collection before a playlist or export can hold it.
  const folderAddToItems = (ids: string[]): MenuItem[] => [
    { heading: 'Playlists', empty: addablePlaylists.length ? undefined : 'No playlists' },
    ...addablePlaylists.map((p) => ({
      label: p.name,
      hint: String(p.count),
      onClick: () => setAdding({ ids, target: { kind: 'playlist' as const, id: p.id, name: p.name } }),
    })),
    { heading: 'Exports', empty: exportSets.data?.length ? undefined : 'No exports yet' },
    ...(exportSets.data ?? []).map((set) => ({
      label: set.name,
      icon: <Icon name="export" size={13} />,
      onClick: () => setAdding({ ids, target: { kind: 'export' as const, id: set.id, name: set.name } }),
    })),
  ]

  return (
    <CapabilitiesContext.Provider value={capabilities.data!}>
    {/* Floating glass panels with gaps between them: the ambient backdrop
        (main.tsx) shows through the gutters as well as through the glass. */}
    <div ref={rootDropRef} className="flex h-screen w-screen flex-col gap-3 overflow-hidden p-3">
      <Toast toast={toast} onClose={() => setToast(null)} />
      {draggingFiles && rootOver &&
        createPortal(
          <div aria-hidden className="pointer-events-none fixed inset-0 z-[85] flex items-center justify-center">
            <div className="absolute inset-1.5 rounded-[26px] ring-2 ring-accent" />
            <div className="glass-overlay flex items-center gap-2.5 overflow-hidden rounded-2xl px-5 py-3 text-sm text-text shadow-lg">
              <Icon name="music" size={15} className={canAddFiles ? 'text-accent' : 'text-pink'} />
              {canAddFiles ? 'Drop to add to your collection' : cannotAddFiles}
            </div>
          </div>,
          document.body,
        )}
      {readingDrop > 0 &&
        createPortal(
          <div className="glass-overlay pointer-events-none fixed bottom-14 left-1/2 z-[80] -translate-x-1/2 overflow-hidden rounded-full px-4 py-2 text-xs text-muted shadow-lg">
            Looking for audio in {readingDrop === 1 ? 'the dropped item' : `${readingDrop} dropped items`}…
          </div>,
          document.body,
        )}
      {menu && (
        <ContextMenu
          // Remount per opening: a right-click on another row while a menu is
          // open must not be caught by the old menu's dismiss listener.
          key={`${menu.track.id}:${menu.x}:${menu.y}`}
          x={menu.x}
          y={menu.y}
          items={[
            // Single-track actions are hidden, not disabled, for a multi-selection.
            ...(menu.ids.length === 1
              ? [{ label: 'Load to Deck', onClick: () => loadTrack(menu.track) }]
              : []),
            // Offering "Edit Tags…" on a read-only library would open a dialog
            // whose every save is refused, so it is not offered at all.
            ...(canEdit && !viewingDevice && menu.ids.length === 1
              ? [{ label: 'Edit Tags…', onClick: gated(() => setEditing(menu.track)) }]
              : []),
            ...(canEdit && !viewingDevice && trackColors.length
              ? [
                  {
                    label: 'Colour',
                    submenu: [
                      {
                        swatches: trackColors.map((s) => s.hex),
                        labels: trackColors.map((s) => s.label),
                        noneLabel: 'No Color',
                        // A multi-selection may hold several colours, so none
                        // is marked ('' matches no swatch and is not "none").
                        selected: menu.ids.length === 1 ? menu.track.color_hex : '',
                        onPick: (hex: string | null) =>
                          menuLoose.length ? void askToAdd(menuLoose) : setTrackColor(menu.ids, hex),
                      },
                    ],
                  },
                ]
              : []),
            // No "add it first" detour for loose files: their loudness can be
            // measured where they are; the dialog explains the rest.
            {
              label: 'Analyze…',
              hint: batchJob ? 'busy' : menu.ids.length > 1 ? String(menu.ids.length) : undefined,
              disabled: !!batchJob,
              onClick: () => setAnalyzing({ ids: menu.ids, origin: analyzeOrigin(menu.ids) }),
            },
            // Level work in one place: the same Analyze run, opened for one
            // option (its remembered ticks untouched).
            {
              label: 'Mastering',
              submenu: [
                {
                  label: 'Normalize…',
                  hint: batchJob ? 'busy' : undefined,
                  disabled: !!batchJob || !!normalizeRefusal(menu.ids),
                  title: normalizeRefusal(menu.ids) ?? undefined,
                  onClick: () => setNormalizing(menu.ids),
                },
                {
                  label: 'Auto gain…',
                  hint: batchJob ? 'busy' : undefined,
                  disabled: !!batchJob || analyzeRefusal(analyzeOrigin(menu.ids)) !== null || !canAutoGain,
                  title:
                    analyzeRefusal(analyzeOrigin(menu.ids)) ??
                    (canAutoGain ? undefined : 'This library has no auto gain'),
                  onClick: () =>
                    setAnalyzing({ ids: menu.ids, origin: analyzeOrigin(menu.ids), preset: 'gain' }),
                },
                {
                  label: 'Measure loudness',
                  hint: batchJob ? 'busy' : undefined,
                  disabled: !!batchJob,
                  onClick: () =>
                    setAnalyzing({ ids: menu.ids, origin: analyzeOrigin(menu.ids), preset: 'loudness' }),
                },
              ],
            },
            // The deck's ✨ Auto covers a single loaded track, with its
            // existing cues shown; this is the same template over the selection.
            ...(canAutoCue
              ? [
                  {
                    label: 'Auto Hotcues…',
                    hint: batchJob ? 'busy' : menu.ids.length > 1 ? String(menu.ids.length) : undefined,
                    disabled: !!batchJob,
                    onClick: gated(() => startCueBatch(menu.ids)),
                  },
                ]
              : []),
            // Shown disabled WITH a reason where the platform cannot play stem
            // files as stems, rather than hidden: a missing item reads as a bug.
            ...(canEdit && !viewingDevice
              ? [
                  canStems
                    ? {
                        label: 'Convert to Stems…',
                        hint: batchJob ? 'busy' : menu.ids.length > 1 ? String(menu.ids.length) : undefined,
                        disabled: !!batchJob,
                        onClick: gated(() => setStemDialog(menu.ids)),
                      }
                    : {
                        label: 'Convert to Stems…',
                        disabled: true,
                        title: 'This library\'s DJ app does not play native-instruments stem files as stems',
                        onClick: () => {},
                      },
                ]
              : []),
            // A device's track ids belong to the stick, not the collection, so
            // there is nothing they could be added to. A folder's files CAN be
            // added — through the add dialog, which puts them in the
            // collection first; rows it already holds are collection tracks.
            ...(viewingFolder
              ? !menuLoose.length
                ? [{ label: 'Add to', submenu: addToItems(menu.ids) }]
                : canAddTracks
                ? [
                    { label: 'Add to Collection…', onClick: () => setAdding({ ids: menuLoose, target: null }) },
                    { label: 'Add to', submenu: folderAddToItems(toFolderIds(menu.ids)) },
                  ]
                : [{
                    label: 'Add to Collection…',
                    disabled: true,
                    title: 'Tracks cannot be added to this library',
                    onClick: () => {},
                  }]
              : viewingDevice
                ? []
                : [{ label: 'Add to', submenu: addToItems(menu.ids) }]),
            // Unlike drag-reorder this stays on while sorted or filtered: it
            // moves within the playlist's FULL order, which the # column shows.
            ...(playlistEditable
              ? [{ label: 'Set Position…', onClick: () => setPositioning(menu.ids) }]
              : []),
            ...(() => {
              const items = menuLoose.length ? [] : removeItems(menu.ids)
              return items.length ? [{ label: 'Remove', submenu: items }] : []
            })(),
          ]}
          onClose={() => setMenu(null)}
        />
      )}
      {headerMenu && (
        <ContextMenu
          key={`${headerMenu.x}:${headerMenu.y}`}
          x={headerMenu.x}
          y={headerMenu.y}
          items={[
            { heading: 'Columns' },
            // Default-visible unless explicitly hidden, as in the Columns menu.
            ...COLUMN_MENU.filter((c) => !c.group).map((c) => columnToggle(c.id, c.label)),
            {
              label: 'Mastering',
              submenu: COLUMN_MENU.filter((c) => c.group === 'mastering').map((c) => columnToggle(c.id, c.label)),
            },
            { separator: true as const },
            { label: 'Reset to defaults', onClick: resetColumns },
          ]}
          onClose={() => setHeaderMenu(null)}
        />
      )}
      {stemDialog && (
        <ConvertStemsDialog
          trackIds={stemDialog}
          onClose={() => setStemDialog(null)}
          onStarted={(job) => {
            setBatchCancelling(false)
            setBatchJob({ id: job.id, kind: 'stems' })
          }}
          onDownloading={(job) => setEngineJob(job.id)}
          onError={onError}
        />
      )}
      {normalizing && (
        <NormalizeDialog
          ids={normalizing}
          onRun={async (opts: NormalizeOptions) => {
            const job = await api.normalize(normalizing, opts)
            setNormalizing(null)
            setBatchCancelling(false)
            setBatchJob({ id: job.id, kind: 'normalize' })
          }}
          onClose={() => setNormalizing(null)}
          onError={onError}
        />
      )}
      {stemReport && <StemReportDialog result={stemReport} onClose={() => setStemReport(null)} />}
      {positioning && (
        <SetPositionDialog
          order={tracks.map((t) => t.id)}
          ids={positioning}
          sorting={sorting}
          onMove={reorderPlaylist}
          onClose={() => setPositioning(null)}
        />
      )}
      {editing && (
        <EditTagsDialog
          track={editing}
          onClose={() => setEditing(null)}
          onApplied={(msg) => notify('success', msg)}
          onError={onError}
        />
      )}
      {confirmReq && <ConfirmDialog {...confirmReq} onClose={() => setConfirmReq(null)} />}
      {cueBatch && (
        <AutoCueDialog
          batch={{ count: cueBatch.ids.length, withoutGrid: cueBatch.withoutGrid }}
          slotCount={capabilities.data!.cues.hotcue_slots}
          slotLabel={slotLabeller(capabilities.data!)}
          onRun={(slots) => runCueBatch(cueBatch.ids, slots)}
          onClose={() => setCueBatch(null)}
          onError={onError}
        />
      )}
      {analyzing && (
        <AnalyzeDialog
          ids={analyzing.ids}
          origin={analyzing.origin}
          gridWhy={analyzeRefusal(analyzing.origin) ?? (canAnalyzeGrid ? null : "This library's beatgrids cannot be edited")}
          keyWhy={analyzeRefusal(analyzing.origin) ?? (canAnalyzeKey ? null : 'This library cannot store a key')}
          gainWhy={analyzeRefusal(analyzing.origin) ?? (canAutoGain ? null : 'This library has no auto gain')}
          preset={analyzing.preset}
          onRun={(opts) => runAnalysis(analyzing.ids, opts)}
          onClose={() => setAnalyzing(null)}
          onError={onError}
        />
      )}
      {!!relocation.data?.volumes.length && (
        <RelocateDialog
          volumes={relocation.data.volumes}
          onAnswered={(applied) => {
            qc.setQueryData(['relocation'], { scanning: false, volumes: [] })
            if (!applied) return
            // Every file-backed answer can change: audio, art, the Files
            // tree's "already in the collection" marks.
            qc.invalidateQueries()
            notify(
              'success',
              `Found ${applied.tracks.toLocaleString()} track${applied.tracks === 1 ? '' : 's'} — remapped for this session`,
            )
          }}
          onOpenManual={() => setShowPaths(true)}
          onError={onError}
        />
      )}
      {showPaths && (
        <PathMappingDialog
          onClose={() => setShowPaths(false)}
          onNotify={notify}
          onError={onError}
        />
      )}
      {showHistory && (
        <HistoryPanel
          onClose={() => setShowHistory(false)}
          onNotify={notify}
          onError={onError}
        />
      )}

      {importing && (
        <ImportDialog
          playlistId={source.kind === 'device-playlist' ? source.id : null}
          deviceLabel={deviceLabel}
          onClose={() => setImporting(false)}
          onDone={(msg) => {
            setImporting(false)
            notify('success', msg)
          }}
          onError={onError}
        />
      )}

      {adding && (
        <AddFilesDialog
          trackIds={adding.ids}
          target={adding.target}
          note={adding.note}
          onClose={() => setAdding(null)}
          onDone={(msg) => {
            setAdding(null)
            notify('success', msg)
          }}
          onError={onError}
        />
      )}

      {importDrop && (
        <ImportDialog
          playlistId={null}
          trackIds={importDrop.ids}
          target={importDrop.target}
          deviceLabel={deviceLabel}
          onClose={() => setImportDrop(null)}
          onDone={(msg) => {
            setImportDrop(null)
            notify('success', msg)
          }}
          onError={onError}
        />
      )}

      {/* Prep strip spans the top of the window; the library sits below it.
          A track dropped on it loads (paused) — the row the drag started on. */}
      <div ref={deckDropRef} className="relative shrink-0">
      {deckOver && (
        <div aria-hidden className="pointer-events-none absolute inset-0 z-10 rounded-[22px] ring-2 ring-accent" />
      )}
      <CapabilitiesContext.Provider value={deckCaps}>
        <PrepStrip
          track={prepTrack}
          playRequest={playRequest}
          onError={onError}
          onNotify={notify}
          origin={prepOrigin}
          cuesRefresh={cuesRefresh}
          onPlayingChange={setDeckPlaying}
          // As the context menu's: a library that would refuse the save is
          // not offered the dialog, and a deck track outside the collection
          // (a device's, a loose file's) is not the collection's to edit.
          onEditTags={canEdit && prepOrigin === 'collection' ? setEditing : undefined}
        />
      </CapabilitiesContext.Provider>
      </div>

      <div className="flex min-h-0 flex-1 gap-3">
        <Sidebar
          source={source}
          onSelect={selectSource}
          onError={onError}
          onOpenHistory={() => setShowHistory(true)}
          onImport={() => setImporting(true)}
          onSwitchLibrary={() => setForcePicker(true)}
          onOpenLibrary={(path) => void openForEditing(path)}
          onDone={(msg) => notify('success', msg)}
          onAddForeign={addForeign}
          onOpenPathMapping={() => setShowPaths(true)}
          onDiscarded={() => afterBulk(prepTrack ? [prepTrack.id] : [], true)}
        />

        <CapabilitiesContext.Provider value={viewCaps}>
        <main className="glass relative flex min-w-0 flex-1 flex-col overflow-hidden">
        {/* Search / filters — always visible. Columns are chosen by right-clicking the header. */}
        <Toolbar
          filters={filters}
          onChange={setFilters}
        />
        {(!isAll || viewForeign) && (
          <div className="flex items-center gap-3 border-b border-line px-4 py-2">
            <span
              className={`flex ${
                viewingDevice || viewingFolder ? 'text-gold' : viewingExport ? 'text-gold' : 'text-accent'
              }`}
            >
              <Icon
                name={
                  viewingDevice
                    ? 'drive'
                    : viewingFolder
                      ? 'folderOpen'
                      : viewingExport && source.kind === 'export'
                        ? 'export'
                        : 'playlist'
                }
                size={15}
              />
            </span>
            <span className="font-semibold text-text" title={folderPath ?? undefined}>{viewName}</span>
            {viewingFolder ? (
              <>
                <span className="text-xs text-faint">
                  {filtersActive
                    ? `${filtered.length} of ${tracks.length} files`
                    : `${tracks.length} file${tracks.length === 1 ? '' : 's'}`}
                  {heldInFolder.size > 0 && ` · ${heldInFolder.size} already in your collection`}
                  {' · this folder only'}
                </span>
                <span className="ml-auto flex items-center gap-2">
                  {tracks.length > heldInFolder.size && (
                    <span className="rounded-full bg-gold/10 px-2 py-0.5 text-[10px] uppercase tracking-wide text-gold shadow-[inset_0_0_0_1px_rgb(255_200_97/0.3)]">
                      Not in collection
                    </span>
                  )}
                  {tracks.length > 0 && canAddTracks && (
                    <button
                      onClick={() =>
                        setAdding({
                          // The selection when there is one, else everything in
                          // view that the collection does not already hold.
                          ids: selected.size
                            ? toFolderIds(filtered.filter((t) => selected.has(t.id)).map((t) => t.id))
                            : filtered.filter((t) => !heldInFolder.has(t.id)).map((t) => t.id),
                          target: null,
                        })
                      }
                      disabled={!selected.size && filtered.every((t) => heldInFolder.has(t.id))}
                      className="btn-primary rounded-full px-3 py-1 text-xs font-semibold disabled:opacity-40"
                    >
                      {selected.size ? `Add ${selected.size} to collection…` : 'Add all to collection…'}
                    </button>
                  )}
                </span>
              </>
            ) : viewingExport ? (
              <>
                <span className="text-xs text-faint">
                  {filtersActive
                    ? `${filtered.length} of ${tracks.length} tracks`
                    : `${tracks.length} tracks`}
                  {source.kind === 'export'
                    ? ' · everything this export would ship'
                    : source.kind === 'export-other'
                      ? ' · added individually — these become an “Other” playlist'
                      : ' · live — what ships is whatever this playlist holds at export time'}
                </span>
                <span className="ml-auto flex items-center gap-2">
                  <span className="rounded-full bg-gold/10 px-2 py-0.5 text-[10px] uppercase tracking-wide text-gold shadow-[inset_0_0_0_1px_rgb(255_200_97/0.3)]">
                    Export
                  </span>
                </span>
              </>
            ) : viewingDevice ? (
              <>
                <span className="text-xs text-faint">
                  {filtersActive
                    ? `${filtered.length} of ${tracks.length} tracks`
                    : `${tracks.length} tracks`}
                </span>
                <span className="ml-auto flex items-center gap-2">
                  <span className="rounded-full bg-gold/10 px-2 py-0.5 text-[10px] uppercase tracking-wide text-gold shadow-[inset_0_0_0_1px_rgb(255_200_97/0.3)]">
                    Device · read-only
                  </span>
                  {canAddTracks && <button
                    onClick={() => setImporting(true)}
                    className="btn-primary rounded-full px-3 py-1 text-xs font-semibold"
                  >
                    Import{source.kind === 'device-playlist' ? ' this playlist' : ' everything'}…
                  </button>}
                </span>
              </>
            ) : (
              <span className="text-xs text-faint">
                {!playlistEditable
                  ? `${filtersActive ? `${filtered.length} of ` : ''}${tracks.length} tracks`
                  : filtersActive || sorting.length > 0
                    ? `${filtersActive ? `${filtered.length} of ` : ''}${tracks.length} tracks · clear the ${
                        filtersActive ? 'filter' : 'sort'
                      } to reorder`
                    : `${tracks.length} tracks · drag rows to reorder · Delete to remove`}
              </span>
            )}
          </div>
        )}

        <div className="min-h-0 flex-1">
          {loading ? (
            <div className="flex h-full items-center justify-center text-muted">Loading…</div>
          ) : viewingExport ? (
            /* An export uses the plain table, NOT the playlist one. The playlist
               table's × removes a track from the user's REAL playlist — same
               pixels, opposite meaning — and an export's own removal is offered
               through the context menu instead, on its root view only.
               Selection stays on, so right-click → Add to works from inside an export. */
            filtered.length === 0 ? (
              <div className="flex h-full flex-col items-center justify-center gap-2 text-muted">
                <div className="text-lg">
                  {tracks.length === 0 ? 'Nothing in this export yet' : 'No tracks match'}
                </div>
                <div className="text-sm text-faint">
                  {tracks.length === 0
                    ? 'Right-click tracks in All Tracks and choose Add to, or add a whole playlist.'
                    : 'Try clearing some filters.'}
                </div>
              </div>
            ) : (
              <TrackTable
                tracks={filtered}
                sorting={sorting}
                onSortingChange={setSorting}
                selection={{ selected, onChange: setSelected }}
                onRowContextMenu={(track, x, y, viewIds) => openMenu(track, x, y, true, viewIds)}
                onHeaderContextMenu={(x, y) => setHeaderMenu({ x, y })}
                onPlay={playTrack}
                onEditField={canEdit ? editField : undefined}
                activeTrackId={prepTrack?.id ?? null}
                activePlaying={deckPlaying}
                columnVisibility={columnVisibility}
                columnOrder={columnOrder}
                columnSizing={columnSizing}
                onColumnOrderChange={setColumnOrder}
                onColumnSizingChange={setColumnSizing}
              />
            )
          ) : viewingFolder ? (
            /* A folder uses the plain table too, WITH selection: its bulk
               action is adding the selected files to the collection. The #
               column checks off the files the collection already holds. */
            folderView.isError ? (
              <div className="flex h-full flex-col items-center justify-center gap-1 text-muted">
                <div className="text-lg">Can’t read this folder</div>
                <div className="text-sm text-faint">{(folderView.error as Error).message}</div>
              </div>
            ) : filtered.length === 0 ? (
              <div className="flex h-full flex-col items-center justify-center gap-1 text-muted">
                <div className="text-lg">
                  {tracks.length === 0 ? 'No music here' : 'No tracks match'}
                </div>
                <div className="text-sm text-faint">
                  {tracks.length === 0
                    ? 'This folder has no audio files directly in it — try one of its subfolders.'
                    : 'Try clearing some filters.'}
                </div>
              </div>
            ) : (
              <TrackTable
                tracks={filtered}
                sorting={sorting}
                onSortingChange={setSorting}
                selection={{ selected, onChange: setSelected }}
                marked={{ ids: heldInFolder, title: 'Already in your collection' }}
                dragFrom={{ origin: 'folder' }}
                onRowContextMenu={(track, x, y, viewIds) => openMenu(track, x, y, true, viewIds)}
                onHeaderContextMenu={(x, y) => setHeaderMenu({ x, y })}
                onPlay={playTrack}
                onEditField={canEdit ? folderEditField : undefined}
                mayEdit={folderMayEdit}
                activeTrackId={prepTrack?.id ?? null}
                activePlaying={deckPlaying}
                columnVisibility={columnVisibility}
                columnOrder={columnOrder}
                columnSizing={columnSizing}
                onColumnOrderChange={setColumnOrder}
                onColumnSizingChange={setColumnSizing}
              />
            )
          ) : viewingDevice ? (
            /* A device uses the plain table, not the playlist one: a stick's
               playlists cannot be reordered or have entries removed, so the
               drag handle and the × would exist only to be inert. Selection is
               omitted too: its bulk action, "Add to", targets the loaded
               COLLECTION's playlists, and these ids belong to the device. */
            filtered.length === 0 ? (
              <div className="flex h-full flex-col items-center justify-center gap-1 text-muted">
                <div className="text-lg">
                  {tracks.length === 0 ? 'Nothing here' : 'No tracks match'}
                </div>
                <div className="text-sm text-faint">
                  {tracks.length === 0
                    ? 'This device has no tracks in view.'
                    : 'Try clearing some filters.'}
                </div>
              </div>
            ) : (
              <TrackTable
                tracks={filtered}
                sorting={sorting}
                onSortingChange={setSorting}
                onRowContextMenu={(track, x, y, viewIds) => openMenu(track, x, y, false, viewIds)}
                dragFrom={{ origin: 'device' }}
                onHeaderContextMenu={(x, y) => setHeaderMenu({ x, y })}
                onPlay={playTrack}
                activeTrackId={prepTrack?.id ?? null}
                activePlaying={deckPlaying}
                columnVisibility={columnVisibility}
                columnOrder={columnOrder}
                columnSizing={columnSizing}
                onColumnOrderChange={setColumnOrder}
                onColumnSizingChange={setColumnSizing}
              />
            )
          ) : isAll ? (
            filtered.length === 0 ? (
              <div className="flex h-full flex-col items-center justify-center gap-1 text-muted">
                <div className="text-lg">No tracks match</div>
                <div className="text-sm text-faint">Try clearing some filters.</div>
              </div>
            ) : (
              <TrackTable
                tracks={filtered}
                sorting={sorting}
                onSortingChange={setSorting}
                selection={{ selected, onChange: setSelected }}
                onRowContextMenu={(track, x, y, viewIds) => openMenu(track, x, y, true, viewIds)}
                onHeaderContextMenu={(x, y) => setHeaderMenu({ x, y })}
                onPlay={playTrack}
                onEditField={canEdit ? editField : undefined}
                activeTrackId={prepTrack?.id ?? null}
                activePlaying={deckPlaying}
                columnVisibility={columnVisibility}
                columnOrder={columnOrder}
                columnSizing={columnSizing}
                onColumnOrderChange={setColumnOrder}
                onColumnSizingChange={setColumnSizing}
              />
            )
          ) : tracks.length === 0 ? (
            <div
              ref={emptyDropRef}
              className={`flex h-full flex-col items-center justify-center gap-2 text-muted ${
                emptyDropOver ? 'rounded-lg outline-2 -outline-offset-2 outline-accent' : ''
              }`}
            >
              <div className="text-lg">This playlist is empty</div>
              <div className="text-sm text-faint">
                Go to <span className="text-text">All Tracks</span>, select tracks, and add them here.
              </div>
            </div>
          ) : filtered.length === 0 ? (
            <div
              ref={emptyDropRef}
              className={`flex h-full flex-col items-center justify-center gap-1 text-muted ${
                emptyDropOver ? 'rounded-lg outline-2 -outline-offset-2 outline-accent' : ''
              }`}
            >
              <div className="text-lg">No tracks match</div>
              <div className="text-sm text-faint">Try clearing some filters.</div>
            </div>
          ) : (
            <TrackTable
              tracks={filtered}
              sorting={sorting}
              onSortingChange={setSorting}
              selection={{ selected, onChange: setSelected }}
              positions={playlistPositions}
              reorder={
                playlistEditable
                  ? { enabled: !filtersActive && sorting.length === 0, onReorder: reorderPlaylist }
                  : undefined
              }
              onRemove={playlistEditable ? confirmRemoveFromPlaylist : undefined}
              foreignDrop={playlistDrop}
              dragFrom={{
                origin: 'collection',
                playlistId: playlistEditable && source.kind === 'playlist' ? source.id : undefined,
              }}
              onRowContextMenu={(track, x, y, viewIds) => openMenu(track, x, y, true, viewIds)}
                onHeaderContextMenu={(x, y) => setHeaderMenu({ x, y })}
              onPlay={playTrack}
              onEditField={canEdit ? editField : undefined}
              activeTrackId={prepTrack?.id ?? null}
                activePlaying={deckPlaying}
              columnVisibility={columnVisibility}
              columnOrder={columnOrder}
              columnSizing={columnSizing}
              onColumnOrderChange={setColumnOrder}
              onColumnSizingChange={setColumnSizing}
            />
          )}

        </div>

        </main>
        </CapabilitiesContext.Provider>
      </div>

      {/* A slim capsule of its own under both columns, as on the deck above. */}
      <StatusBar
        showing={filtered.length}
        total={tracks.length}
        sourceName={viewName}
        selected={viewingDevice ? 0 : selected.size}
        job={
          batchJob
            ? {
                label:
                  batchJob.kind === 'grid'
                    ? 'Analyzing'
                    : batchJob.kind === 'stems'
                      ? 'Converting to stems'
                      : batchJob.kind === 'normalize'
                        ? 'Normalizing'
                        : 'Placing hotcues',
                done: batchJobStatus.data?.done ?? 0,
                total: batchJobStatus.data?.total ?? 0,
                detail: batchJobStatus.data?.message,
                fraction: batchJobStatus.data?.fraction,
                status: batchJobStatus.data?.status,
                cancelling: batchCancelling,
                onCancel: () => {
                  setBatchCancelling(true)
                  api.cancelJob(batchJob.id).catch((e) => onError((e as Error).message))
                },
              }
            : engineJob
              ? {
                  label: 'Downloading the stem engine',
                  unit: 'bytes',
                  done: engineJobStatus.data?.done ?? 0,
                  total: engineJobStatus.data?.total ?? 0,
                  detail: engineJobStatus.data?.message,
                  onCancel: () => {
                    api.cancelJob(engineJob).catch((e) => onError((e as Error).message))
                  },
                }
              : null
        }
        loading={loading}
        connection={editState.data?.remote ?? null}
        collectionName={capabilities.data ? libraryName : null}
        onChangeCollection={() => {
          // The same question the sidebar header asks: this button used to
          // switch library without it, silently dropping unsaved edits.
          void confirmDiscardUnsaved().then((ok) => ok && setForcePicker(true))
        }}
      />
    </div>
    </CapabilitiesContext.Provider>
  )
}
