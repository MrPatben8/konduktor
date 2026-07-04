import { useCallback, useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import type { SortingState } from '@tanstack/react-table'
import { api, type Track } from './api'
import { Sidebar, type Source } from './components/Sidebar'
import { Toolbar, emptyFilters, type Filters } from './components/Toolbar'
import { TrackTable } from './components/TrackTable'
import { PlaylistEditor } from './components/PlaylistEditor'
import { SelectionBar } from './components/SelectionBar'
import { StatusBar } from './components/StatusBar'
import { Toast, type ToastMsg } from './components/Toast'
import { CollectionPicker } from './components/CollectionPicker'
import { ContextMenu } from './components/ContextMenu'
import { EditTagsDialog } from './components/EditTagsDialog'
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
  const [prepTrack, setPrepTrack] = useState<Track | null>(null)
  const [playRequest, setPlayRequest] = useState(0) // bump → deck loads & auto-plays

  const playTrack = useCallback((t: Track) => {
    setPrepTrack(t)
    setPlayRequest((n) => n + 1)
  }, [])

  const notify = useCallback((kind: ToastMsg['kind'], text: string) => {
    setToast({ id: Date.now(), kind, text })
  }, [])
  const onError = useCallback((msg: string) => notify('error', msg), [notify])

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
  const filtered = useMemo(() => (isAll ? applyFilters(tracks, filters) : tracks), [
    isAll,
    tracks,
    filters,
  ])

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

      {/* Prep strip spans the top of the window; the library sits below it. */}
      <PrepStrip track={prepTrack} playRequest={playRequest} onError={onError} />

      <div className="flex min-h-0 flex-1 overflow-hidden">
        <Sidebar source={source} onSelect={selectSource} onError={onError} />

        <main className="relative flex min-w-0 flex-1 flex-col">
        {isAll ? (
          <Toolbar filters={filters} onChange={setFilters} />
        ) : (
          <div className="flex items-center gap-3 border-b border-line bg-ink-900 px-4 py-3">
            <span className="text-[11px] text-accent">♫</span>
            <span className="font-semibold text-text">{source.name}</span>
            <span className="text-xs text-faint">
              {tracks.length} tracks · drag ⠿ to reorder · × to remove
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
                activeTrackId={prepTrack?.id ?? null}
              />
            )
          ) : (
            <PlaylistEditor
              uuid={(source as { id: string }).id}
              tracks={tracks}
              onError={onError}
              onRowContextMenu={(track, x, y) => setMenu({ track, x, y })}
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
