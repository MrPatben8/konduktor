import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import type { ColumnSizingState, SortingState, VisibilityState } from '@tanstack/react-table'
import { CapabilitiesContext, slotLabeller } from './lib/capabilities'
import { writeHint } from './lib/platformCopy'
import { api, type CueBatchResult, type GridBatchResult, type AutoCueSlot, type PlaylistNode, type Track } from './api'
import {
  COLUMN_MENU,
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
import { AnalyzeGridDialog } from './components/AnalyzeGridDialog'
import { AutoCueDialog } from './components/AutoCueDialog'
import { ConfirmDialog, type ConfirmRequest } from './components/ConfirmDialog'
import { PathMappingDialog } from './components/PathMappingDialog'
import { HistoryPanel } from './components/HistoryPanel'
import { PrepStrip } from './components/PrepStrip'
import { ImportDialog } from './components/ImportDialog'

function applyFilters(tracks: Track[], f: Filters): Track[] {
  const q = f.search.trim().toLowerCase()
  const bpmMin = f.bpmMin ? parseFloat(f.bpmMin) : null
  const bpmMax = f.bpmMax ? parseFloat(f.bpmMax) : null
  return tracks.filter((t) => {
    if (q) {
      const hay = `${t.artist ?? ''} ${t.title ?? ''} ${t.album ?? ''}`.toLowerCase()
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
  const [playRequest, setPlayRequest] = useState(0) // bump → deck loads & auto-plays
  const [importing, setImporting] = useState(false)
  // Batch analysis (grid, or Auto Hotcues): a confirm step, then the running
  // job, polled into the status bar. ONE at a time across both kinds — the
  // backend refuses a second, since a grid run would move the beats a hotcue
  // run is placing cues on.
  const [gridConfirm, setGridConfirm] = useState<
    { ids: string[]; existing: number; locked: number } | null
  >(null)
  const [cueBatch, setCueBatch] = useState<{ ids: string[]; withoutGrid: number } | null>(null)
  const [batchJob, setBatchJob] = useState<{ id: string; kind: 'grid' | 'cues' } | null>(null)
  const [batchCancelling, setBatchCancelling] = useState(false)
  const [cuesRefresh, setCuesRefresh] = useState(0) // bump → deck re-reads its cues
  // Every Remove ▸ action (and the playlist Delete key) confirms first.
  const [confirmReq, setConfirmReq] = useState<ConfirmRequest | null>(null)

  const playTrack = useCallback((t: Track) => {
    setPrepTrack(t)
    setPlayRequest((n) => n + 1)
  }, [])

  // ---- configurable library columns (persisted to userprefs.json) ----
  const [columnVisibility, setColumnVisibility] = useState<VisibilityState>(DEFAULT_COLUMN_VISIBILITY)
  const [columnOrder, setColumnOrder] = useState<string[]>(DEFAULT_COLUMN_ORDER)
  const [columnSizing, setColumnSizing] = useState<ColumnSizingState>({})
  const prefsQuery = useQuery({ queryKey: ['prefs'], queryFn: api.getPrefs })
  const hydratedRef = useRef(false)
  const saveTimer = useRef<number | null>(null)

  const resetColumns = useCallback(() => {
    setColumnVisibility(DEFAULT_COLUMN_VISIBILITY)
    setColumnOrder(DEFAULT_COLUMN_ORDER)
    setColumnSizing({})
  }, [])

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

  // Persist layout changes (debounced), but not before hydration so we never
  // clobber saved prefs with the initial defaults.
  useEffect(() => {
    if (!hydratedRef.current) return
    if (saveTimer.current) window.clearTimeout(saveTimer.current)
    saveTimer.current = window.setTimeout(() => {
      api
        .patchPrefs({
          columns: { visibility: columnVisibility, order: columnOrder, sizing: columnSizing },
        })
        .catch(() => {})
    }, 500)
    return () => {
      if (saveTimer.current) window.clearTimeout(saveTimer.current)
    }
  }, [columnVisibility, columnOrder, columnSizing])

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
  // From the adapter, not a path split: a Serato library is a directory.
  const libraryName = collection.data?.library?.display_name ?? null
  const writeHintText = save ? writeHint(save) : 'save to write it to disk'

  const notify = useCallback((kind: ToastMsg['kind'], text: string) => {
    setToast({ id: Date.now(), kind, text })
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
          qc.invalidateQueries({ queryKey: ['tracks'] })
          qc.invalidateQueries({ queryKey: ['playlist'] })
          qc.invalidateQueries({ queryKey: ['state'] })
          qc.invalidateQueries({ queryKey: ['facets'] })
          notify('success', `Updated ${String(field)} — ${writeHintText}`)
        })
        .catch((e) => onError((e as Error).message))
    },
    [qc, notify, onError, writeHintText],
  )


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
    enabled: viewingDevice,
    staleTime: Infinity,
  })
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
  const addToPlaylist = useCallback(
    async (uuid: string, ids: string[]) => {
      try {
        const res = await api.addEntries(uuid, ids)
        qc.invalidateQueries({ queryKey: ['state'] })
        qc.invalidateQueries({ queryKey: ['playlists'] })
        qc.invalidateQueries({ queryKey: ['playlist', uuid] })
        const name = addablePlaylists.find((p) => p.id === uuid)?.name ?? 'playlist'
        notify('success', `Added ${res.added} track${res.added === 1 ? '' : 's'} to ${name}`)
      } catch (e) {
        onError((e as Error).message)
      }
    },
    [qc, addablePlaylists, notify, onError],
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
  const viewCaps = viewingDevice
    ? (deviceCaps.data ??
      // Until the device's own capabilities arrive, assume READ-ONLY rather
      // than falling back to the collection's. The fallback is the safe
      // direction on purpose: guessing "writable" for one frame would offer an
      // edit that, if taken, would be sent to the wrong library entirely.
      { ...capabilities.data!, writable: false, readonly_cause: 'platform_incomplete' as const })
    : capabilities.data!

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
  const tracks: Track[] = viewingDevice
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
  const loading = viewingDevice
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
    if (job.state === 'failed') {
      const what = kind === 'grid' ? 'Grid analysis' : 'Auto Hotcues'
      onError(`${what} failed: ${job.error ?? 'unknown error'}`)
      return
    }
    if (!job.result) return
    qc.invalidateQueries({ queryKey: ['state'] })
    qc.invalidateQueries({ queryKey: ['tracks'] })
    qc.invalidateQueries({ queryKey: ['playlist'] })
    qc.invalidateQueries({ queryKey: ['export-tracks'] })
    qc.invalidateQueries({ queryKey: ['export-playlist-tracks'] })
    qc.invalidateQueries({ queryKey: ['export-loose'] })
    qc.invalidateQueries({ queryKey: ['facets'] })
    const cancelled = job.state === 'cancelled'
    const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? '' : 's'}`
    let touched: string[]
    let parts: string[]
    let failures: CueBatchResult['failed']
    if (kind === 'grid') {
      const r = job.result as unknown as GridBatchResult
      touched = r.analysed
      failures = r.failed
      parts = [
        `${cancelled ? 'Cancelled — analyzed' : 'Analyzed'} ${plural(r.analysed.length, 'track')}`,
        r.existing ? `skipped ${r.existing} with a grid` : '',
        r.locked ? `${r.locked} locked` : '',
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
    setFilters(emptyFilters)
    setSorting([])
    qc.invalidateQueries() // refetch everything for the newly-opened collection
  }

  if (collection.isLoading || (loaded && !capabilities.data)) {
    return (
      <div className="flex h-screen w-screen items-center justify-center bg-ink-950 text-muted">
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
  // same rule is applied here to decide what the menu acts on.
  const openMenu = (track: Track, x: number, y: number, selectable: boolean) =>
    setMenu({
      track,
      ids: selectable && selected.has(track.id) ? [...selected] : [track.id],
      x,
      y,
    })

  const runGridAnalysis = async (ids: string[], replaceExisting: boolean) => {
    setGridConfirm(null)
    try {
      const job = await api.autoGridBatch(ids, replaceExisting)
      setBatchCancelling(false)
      setBatchJob({ id: job.id, kind: 'grid' })
    } catch (e) {
      onError((e as Error).message)
    }
  }
  // Confirm only when the run would meet existing grids. Counted from the
  // view's tracks, which is where the selection came from.
  const startGridAnalysis = (ids: string[]) => {
    const byId = new Map(tracks.map((t) => [t.id, t]))
    const sel = ids.map((id) => byId.get(id)).filter((t): t is Track => !!t)
    const locked = sel.filter((t) => t.grid_locked).length
    const existing = sel.filter((t) => !t.grid_locked && t.grid_marker_count > 0).length
    if (locked === ids.length) {
      notify('error', `${locked === 1 ? 'That grid is' : `All ${locked} grids are`} locked — nothing to analyze`)
    } else if (existing > 0) {
      setGridConfirm({ ids, existing, locked })
    } else {
      void runGridAnalysis(ids, false)
    }
  }
  const canAnalyzeGrid = canEdit && !viewingDevice && !!capabilities.data?.grid.editable

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
    qc.invalidateQueries({ queryKey: ['state'] })
    qc.invalidateQueries({ queryKey: ['tracks'] })
    qc.invalidateQueries({ queryKey: ['playlist'] })
    qc.invalidateQueries({ queryKey: ['playlists'] })
    qc.invalidateQueries({ queryKey: ['export-tracks'] })
    qc.invalidateQueries({ queryKey: ['export-playlist-tracks'] })
    qc.invalidateQueries({ queryKey: ['export-loose'] })
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
          <p className="text-faint">
            The audio {ids.length === 1 ? 'file stays' : 'files stay'} on disk. Nothing is written until you
            save, and a save can be rolled back from version history.
          </p>
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
  const exportRootId =
    source.kind === 'export' ? source.id : source.kind === 'export-other' ? source.exportId : null
  const removeItems = (ids: string[]): MenuItem[] => [
    ...(playlistEditable
      ? [{ label: 'From this playlist…', onClick: () => confirmRemoveFromPlaylist(ids) }]
      : []),
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
        icon: '◈',
        onClick: () => addToExport(set.id, ids),
      })),
    ]
  }

  return (
    <CapabilitiesContext.Provider value={capabilities.data!}>
    <div className="flex h-screen w-screen flex-col overflow-hidden bg-ink-950">
      <Toast toast={toast} onClose={() => setToast(null)} />
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
              ? [{ label: 'Load to Deck', onClick: () => setPrepTrack(menu.track) }]
              : []),
            // Offering "Edit Tags…" on a read-only library would open a dialog
            // whose every save is refused, so it is not offered at all.
            ...(canEdit && menu.ids.length === 1
              ? [{ label: 'Edit Tags…', onClick: () => setEditing(menu.track) }]
              : []),
            ...(canAnalyzeGrid
              ? [
                  {
                    label: 'Analyze Grid & BPM',
                    hint: batchJob ? 'busy' : menu.ids.length > 1 ? String(menu.ids.length) : undefined,
                    disabled: !!batchJob,
                    onClick: () => startGridAnalysis(menu.ids),
                  },
                ]
              : []),
            // The deck's ✨ Auto covers a single loaded track, with its
            // existing cues shown; this is the same template over the selection.
            ...(canAutoCue
              ? [
                  {
                    label: 'Auto Hotcues…',
                    hint: batchJob ? 'busy' : menu.ids.length > 1 ? String(menu.ids.length) : undefined,
                    disabled: !!batchJob,
                    onClick: () => startCueBatch(menu.ids),
                  },
                ]
              : []),
            // A device's track ids belong to the stick, not the collection, so
            // there is nothing they could be added to.
            ...(viewingDevice ? [] : [{ label: 'Add to', submenu: addToItems(menu.ids) }]),
            ...(() => {
              const items = removeItems(menu.ids)
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
            ...COLUMN_MENU.map((c) => ({
              label: c.label,
              checked: columnVisibility[c.id] !== false,
              onToggle: () =>
                setColumnVisibility((v) => ({ ...v, [c.id]: v[c.id] === false })),
            })),
            { separator: true as const },
            { label: 'Reset to defaults', onClick: resetColumns },
          ]}
          onClose={() => setHeaderMenu(null)}
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
      {gridConfirm && (
        <AnalyzeGridDialog
          total={gridConfirm.ids.length}
          existing={gridConfirm.existing}
          locked={gridConfirm.locked}
          onChoose={(replace) => runGridAnalysis(gridConfirm.ids, replace)}
          onClose={() => setGridConfirm(null)}
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

      {/* Prep strip spans the top of the window; the library sits below it. */}
      <CapabilitiesContext.Provider value={viewCaps}>
        <PrepStrip
          track={prepTrack}
          playRequest={playRequest}
          onError={onError}
          onNotify={notify}
          fromDevice={viewingDevice}
          cuesRefresh={cuesRefresh}
        />
      </CapabilitiesContext.Provider>

      <div className="flex min-h-0 flex-1 overflow-hidden">
        <Sidebar
          source={source}
          onSelect={selectSource}
          onError={onError}
          onOpenHistory={() => setShowHistory(true)}
          onImport={() => setImporting(true)}
          onSwitchLibrary={() => setForcePicker(true)}
          onDone={(msg) => notify('success', msg)}
          onOpenPathMapping={() => setShowPaths(true)}
        />

        <CapabilitiesContext.Provider value={viewCaps}>
        <main className="relative flex min-w-0 flex-1 flex-col">
        {/* Search / filters — always visible. Columns are chosen by right-clicking the header. */}
        <Toolbar
          filters={filters}
          onChange={setFilters}
        />
        {(!isAll || viewingDevice) && (
          <div className="flex items-center gap-3 border-b border-line bg-ink-900 px-4 py-2">
            <span
              className={`text-[11px] ${
                viewingDevice ? 'text-gold' : viewingExport ? 'text-gold' : 'text-accent'
              }`}
            >
              {viewingDevice ? '⬒' : viewingExport ? (source.kind === 'export' ? '◈' : '♫') : '♫'}
            </span>
            <span className="font-semibold text-text">{viewName}</span>
            {viewingExport ? (
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
                  <span className="rounded bg-ink-800 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-gold">
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
                  <span className="rounded bg-ink-800 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-gold">
                    Device · read-only
                  </span>
                  <button
                    onClick={() => setImporting(true)}
                    className="rounded-md bg-accent px-2 py-1 text-xs font-medium text-ink-950 hover:brightness-110"
                  >
                    Import{source.kind === 'device-playlist' ? ' this playlist' : ' everything'}…
                  </button>
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
                onRowContextMenu={(track, x, y) => openMenu(track, x, y, true)}
                onHeaderContextMenu={(x, y) => setHeaderMenu({ x, y })}
                onPlay={playTrack}
                onEditField={canEdit ? editField : undefined}
                activeTrackId={prepTrack?.id ?? null}
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
                onRowContextMenu={(track, x, y) => openMenu(track, x, y, false)}
                onHeaderContextMenu={(x, y) => setHeaderMenu({ x, y })}
                onPlay={playTrack}
                activeTrackId={prepTrack?.id ?? null}
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
                onRowContextMenu={(track, x, y) => openMenu(track, x, y, true)}
                onHeaderContextMenu={(x, y) => setHeaderMenu({ x, y })}
                onPlay={playTrack}
                onEditField={canEdit ? editField : undefined}
                activeTrackId={prepTrack?.id ?? null}
                columnVisibility={columnVisibility}
                columnOrder={columnOrder}
                columnSizing={columnSizing}
                onColumnOrderChange={setColumnOrder}
                onColumnSizingChange={setColumnSizing}
              />
            )
          ) : tracks.length === 0 ? (
            <div className="flex h-full flex-col items-center justify-center gap-2 text-muted">
              <div className="text-lg">This playlist is empty</div>
              <div className="text-sm text-faint">
                Go to <span className="text-text">All Tracks</span>, select tracks, and add them here.
              </div>
            </div>
          ) : filtered.length === 0 ? (
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
              positions={playlistPositions}
              reorder={
                playlistEditable
                  ? { enabled: !filtersActive && sorting.length === 0, onReorder: reorderPlaylist }
                  : undefined
              }
              onRemove={playlistEditable ? confirmRemoveFromPlaylist : undefined}
              onRowContextMenu={(track, x, y) => openMenu(track, x, y, true)}
                onHeaderContextMenu={(x, y) => setHeaderMenu({ x, y })}
              onPlay={playTrack}
              onEditField={canEdit ? editField : undefined}
              activeTrackId={prepTrack?.id ?? null}
              columnVisibility={columnVisibility}
              columnOrder={columnOrder}
              columnSizing={columnSizing}
              onColumnOrderChange={setColumnOrder}
              onColumnSizingChange={setColumnSizing}
            />
          )}

        </div>

        <StatusBar
          showing={filtered.length}
          total={tracks.length}
          sourceName={viewName}
          selected={viewingDevice ? 0 : selected.size}
          job={
            batchJob
              ? {
                  label: batchJob.kind === 'grid' ? 'Analyzing grids' : 'Placing hotcues',
                  done: batchJobStatus.data?.done ?? 0,
                  total: batchJobStatus.data?.total ?? 0,
                  detail: batchJobStatus.data?.message,
                  cancelling: batchCancelling,
                  onCancel: () => {
                    setBatchCancelling(true)
                    api.cancelJob(batchJob.id).catch((e) => onError((e as Error).message))
                  },
                }
              : null
          }
          loading={loading}
          collectionName={capabilities.data ? libraryName : null}
          onChangeCollection={() => setForcePicker(true)}
        />
        </main>
        </CapabilitiesContext.Provider>
      </div>
    </div>
    </CapabilitiesContext.Provider>
  )
}
