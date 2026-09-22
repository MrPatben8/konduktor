import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import type { ColumnSizingState, SortingState, VisibilityState } from '@tanstack/react-table'
import { CapabilitiesContext } from './lib/capabilities'
import { writeHint } from './lib/platformCopy'
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
  const [menu, setMenu] = useState<{ track: Track; x: number; y: number } | null>(null)
  const [editing, setEditing] = useState<Track | null>(null)
  const [showPaths, setShowPaths] = useState(false)
  const [showHistory, setShowHistory] = useState(false)
  const [prepTrack, setPrepTrack] = useState<Track | null>(null)
  const [playRequest, setPlayRequest] = useState(0) // bump → deck loads & auto-plays
  const [importing, setImporting] = useState(false)

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
    : isAll
      ? (allTracks.data?.items ?? [])
      : (playlistTracks.data ?? [])
  const loading = viewingDevice
    ? source.kind === 'device'
      ? deviceTracks.isLoading
      : devicePlaylistTracks.isLoading
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
    <CapabilitiesContext.Provider value={capabilities.data!}>
    <div className="flex h-screen w-screen flex-col overflow-hidden bg-ink-950">
      <Toast toast={toast} onClose={() => setToast(null)} />
      {menu && (
        <ContextMenu
          x={menu.x}
          y={menu.y}
          items={[
            { label: 'Load to Deck', onClick: () => setPrepTrack(menu.track) },
            // Offering "Edit Tags…" on a read-only library would open a dialog
            // whose every save is refused, so it is not offered at all.
            ...(canEdit
              ? [{ label: 'Edit Tags…', onClick: () => setEditing(menu.track) }]
              : []),
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
        />
      </CapabilitiesContext.Provider>

      <div className="flex min-h-0 flex-1 overflow-hidden">
        <Sidebar
          source={source}
          onSelect={selectSource}
          onError={onError}
          onOpenHistory={() => setShowHistory(true)}
          onImport={() => setImporting(true)}
        />

        <CapabilitiesContext.Provider value={viewCaps}>
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
        {(!isAll || viewingDevice) && (
          <div className="flex items-center gap-3 border-b border-line bg-ink-900 px-4 py-2">
            <span className={`text-[11px] ${viewingDevice ? 'text-gold' : 'text-accent'}`}>
              {viewingDevice ? '⬒' : '♫'}
            </span>
            <span className="font-semibold text-text">{viewName}</span>
            {viewingDevice ? (
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
                {filtersActive
                  ? `${filtered.length} of ${tracks.length} tracks · × to remove · clear the filter to reorder`
                  : `${tracks.length} tracks · drag ⠿ to reorder · × to remove`}
              </span>
            )}
          </div>
        )}

        <div className="min-h-0 flex-1">
          {loading ? (
            <div className="flex h-full items-center justify-center text-muted">Loading…</div>
          ) : viewingDevice ? (
            /* A device uses the plain table, not the playlist one: a stick's
               playlists cannot be reordered or have entries removed, so the
               drag handle and the × would exist only to be inert. Selection is
               omitted for the same reason — SelectionBar adds to the loaded
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
                onRowContextMenu={(track, x, y) => setMenu({ track, x, y })}
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
                selection={{
                  selected,
                  onToggle: toggle,
                  onToggleAll: toggleAll,
                  allSelected: selected.size > 0 && selected.size === filtered.length,
                }}
                onRowContextMenu={(track, x, y) => setMenu({ track, x, y })}
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
            <PlaylistTable
              tracks={filtered}
              onRowContextMenu={(track, x, y) => setMenu({ track, x, y })}
              onPlay={playTrack}
              onEditField={canEdit ? editField : undefined}
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
          sourceName={viewName}
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
