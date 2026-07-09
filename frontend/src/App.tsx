import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import type { ColumnSizingState, SortingState, VisibilityState } from '@tanstack/react-table'
import { api, type Track } from './api'
import {
  DEFAULT_COLUMN_ORDER,
  DEFAULT_COLUMN_VISIBILITY,
} from './lib/trackColumns'
import { Sidebar, type Source } from './components/Sidebar'
import { Toolbar, emptyFilters, type Filters } from './components/Toolbar'
import { TrackTable, PlaylistTable } from './components/TrackTable'
import { SelectionBar } from './components/SelectionBar'
import { StatusBar } from './components/StatusBar'
import { Toast, type ToastMsg } from './components/Toast'
import { CollectionPicker } from './components/CollectionPicker'
import { ContextMenu } from './components/ContextMenu'
import { EditTagsDialog } from './components/EditTagsDialog'
import { PathMappingDialog } from './components/PathMappingDialog'
import { HistoryPanel } from './components/HistoryPanel'
import { PrepStrip } from './components/PrepStrip'

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
  const [menu, setMenu] = useState<{ track: Track; x: number; y: number } | null>(null)
  const [editing, setEditing] = useState<Track | null>(null)
  const [showPaths, setShowPaths] = useState(false)
  const [showHistory, setShowHistory] = useState(false)
  const [prepTrack, setPrepTrack] = useState<Track | null>(null)
  const [playRequest, setPlayRequest] = useState(0) // bump → deck loads & auto-plays

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

  const notify = useCallback((kind: ToastMsg['kind'], text: string) => {
    setToast({ id: Date.now(), kind, text })
  }, [])
  const onError = useCallback((msg: string) => notify('error', msg), [notify])

  // Inline single-field edit from a double-clicked table cell.
  const editField = useCallback(
    (track: Track, field: keyof Track, value: string | number) => {
      api
        .editTrack(track.id, { [field]: value })
        .then(() => {
          qc.invalidateQueries({ queryKey: ['tracks'] })
          qc.invalidateQueries({ queryKey: ['playlist'] })
          qc.invalidateQueries({ queryKey: ['state'] })
          qc.invalidateQueries({ queryKey: ['facets'] })
          qc.invalidateQueries({ queryKey: ['stats'] })
          notify('success', `Updated ${String(field)} — Save to write to Traktor`)
        })
        .catch((e) => onError((e as Error).message))
    },
    [qc, notify, onError],
  )

  const collection = useQuery({ queryKey: ['collection'], queryFn: api.collection })
  const loaded = collection.data?.loaded ?? false

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

  const isAll = source.kind === 'all'
  const tracks: Track[] = isAll ? (allTracks.data?.items ?? []) : (playlistTracks.data ?? [])
  const loading = isAll ? allTracks.isLoading : playlistTracks.isLoading
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
    (trackId: string) =>
      reorderPlaylist(tracks.filter((t) => t.id !== trackId).map((t) => t.id)),
    [reorderPlaylist, tracks],
  )

  const handleOpened = () => {
    setForcePicker(false)
    setSource({ kind: 'all' })
    setSelected(new Set())
    setFilters(emptyFilters)
    setSorting([])
    qc.invalidateQueries() // refetch everything for the newly-opened collection
  }

  if (collection.isLoading) {
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

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  const toggleAll = () =>
    setSelected((prev) =>
      prev.size === filtered.length ? new Set() : new Set(filtered.map((t) => t.id)),
    )

  return (
    <div className="flex h-screen w-screen flex-col overflow-hidden bg-ink-950">
      <Toast toast={toast} onClose={() => setToast(null)} />
      {menu && (
        <ContextMenu
          x={menu.x}
          y={menu.y}
          items={[
            { label: 'Load to Deck', onClick: () => setPrepTrack(menu.track) },
            { label: 'Edit Tags…', onClick: () => setEditing(menu.track) },
          ]}
          onClose={() => setMenu(null)}
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

      {/* Prep strip spans the top of the window; the library sits below it. */}
      <PrepStrip track={prepTrack} playRequest={playRequest} onError={onError} onNotify={notify} />

      <div className="flex min-h-0 flex-1 overflow-hidden">
        <Sidebar
          source={source}
          onSelect={selectSource}
          onError={onError}
          onOpenHistory={() => setShowHistory(true)}
        />

        <main className="relative flex min-w-0 flex-1 flex-col">
        {/* Search / filters / column settings — always visible. */}
        <Toolbar
          filters={filters}
          onChange={setFilters}
          columnVisibility={columnVisibility}
          onColumnVisibilityChange={setColumnVisibility}
          onResetColumns={resetColumns}
          onOpenPathMapping={() => setShowPaths(true)}
        />
        {!isAll && (
          <div className="flex items-center gap-3 border-b border-line bg-ink-900 px-4 py-2">
            <span className="text-[11px] text-accent">♫</span>
            <span className="font-semibold text-text">{source.name}</span>
            <span className="text-xs text-faint">
              {filtersActive
                ? `${filtered.length} of ${tracks.length} tracks · × to remove · clear the filter to reorder`
                : `${tracks.length} tracks · drag ⠿ to reorder · × to remove`}
            </span>
          </div>
        )}

        <div className="min-h-0 flex-1">
          {loading ? (
            <div className="flex h-full items-center justify-center text-muted">Loading…</div>
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
                selection={{
                  selected,
                  onToggle: toggle,
                  onToggleAll: toggleAll,
                  allSelected: selected.size > 0 && selected.size === filtered.length,
                }}
                onRowContextMenu={(track, x, y) => setMenu({ track, x, y })}
                onPlay={playTrack}
                onEditField={editField}
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
            <PlaylistTable
              tracks={filtered}
              onRowContextMenu={(track, x, y) => setMenu({ track, x, y })}
              onPlay={playTrack}
              onEditField={editField}
              activeTrackId={prepTrack?.id ?? null}
              columnVisibility={columnVisibility}
              columnOrder={columnOrder}
              columnSizing={columnSizing}
              onColumnOrderChange={setColumnOrder}
              onColumnSizingChange={setColumnSizing}
              onReorder={reorderPlaylist}
              onRemove={removeFromPlaylist}
              canReorder={!filtersActive}
            />
          )}

          {isAll && (
            <SelectionBar
              count={selected.size}
              trackIds={[...selected]}
              onClear={() => setSelected(new Set())}
              onDone={(msg) => notify('success', msg)}
              onError={onError}
            />
          )}
        </div>

        <StatusBar
          showing={filtered.length}
          total={tracks.length}
          sourceName={isAll ? 'All Tracks' : source.name}
          loading={loading}
          collectionName={collection.data?.path?.split('/').pop() ?? null}
          onChangeCollection={() => setForcePicker(true)}
        />
        </main>
      </div>
    </div>
  )
}
