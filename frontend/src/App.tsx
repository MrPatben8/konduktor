import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { SortingState } from '@tanstack/react-table'
import { api, type Track } from './api'
import { Sidebar, type Source } from './components/Sidebar'
import { Toolbar, emptyFilters, type Filters } from './components/Toolbar'
import { TrackTable } from './components/TrackTable'
import { StatusBar } from './components/StatusBar'

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
  const [source, setSource] = useState<Source>({ kind: 'all' })
  const [filters, setFilters] = useState<Filters>(emptyFilters)
  const [sorting, setSorting] = useState<SortingState>([])

  const allTracks = useQuery({
    queryKey: ['tracks', 'all'],
    queryFn: () => api.tracks({ limit: 20000, sort: 'artist' }),
    enabled: source.kind === 'all',
  })

  const playlistTracks = useQuery({
    queryKey: ['playlist', source.kind === 'playlist' ? source.id : null],
    queryFn: () => api.playlistTracks((source as { id: string }).id),
    enabled: source.kind === 'playlist',
  })

  const tracks: Track[] =
    source.kind === 'all' ? (allTracks.data?.items ?? []) : (playlistTracks.data ?? [])
  const loading = source.kind === 'all' ? allTracks.isLoading : playlistTracks.isLoading

  const filtered = useMemo(() => applyFilters(tracks, filters), [tracks, filters])

  const sourceName = source.kind === 'all' ? 'All Tracks' : source.name

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-ink-950">
      <Sidebar
        source={source}
        onSelect={(s) => {
          setSource(s)
          // Playlists carry a deliberate order; reset sort to preserve it.
          setSorting([])
        }}
      />
      <main className="flex min-w-0 flex-1 flex-col">
        <Toolbar filters={filters} onChange={setFilters} />
        <div className="min-h-0 flex-1">
          {loading ? (
            <div className="flex h-full items-center justify-center text-muted">
              Loading library…
            </div>
          ) : filtered.length === 0 ? (
            <div className="flex h-full flex-col items-center justify-center gap-1 text-muted">
              <div className="text-lg">No tracks match</div>
              <div className="text-sm text-faint">Try clearing some filters.</div>
            </div>
          ) : (
            <TrackTable tracks={filtered} sorting={sorting} onSortingChange={setSorting} />
          )}
        </div>
        <StatusBar
          showing={filtered.length}
          total={tracks.length}
          sourceName={sourceName}
          loading={loading}
        />
      </main>
    </div>
  )
}
