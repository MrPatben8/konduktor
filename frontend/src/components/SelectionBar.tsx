import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type PlaylistNode } from '../api'

interface Props {
  count: number
  trackIds: string[]
  onClear: () => void
  onDone: (msg: string) => void
  onError: (msg: string) => void
}

function flattenPlaylists(nodes: PlaylistNode[]): PlaylistNode[] {
  return nodes.flatMap((n) => [
    ...(n.can_add_tracks ? [n] : []),
    ...flattenPlaylists(n.children),
  ])
}

// Floating action bar shown when tracks are selected in the explorer.
export function SelectionBar({ count, trackIds, onClear, onDone, onError }: Props) {
  const qc = useQueryClient()
  const [menuOpen, setMenuOpen] = useState(false)
  const { data: playlists } = useQuery({ queryKey: ['playlists'], queryFn: api.playlists })
  const { data: exportSets } = useQuery({ queryKey: ['exports'], queryFn: api.exports })
  const flat = playlists ? flattenPlaylists(playlists) : []
  const sets = exportSets ?? []

  // Adding to an EXPORT does not touch the library — an export set is
  // Konduktor's own curation — so unlike the playlist mutation this dirties
  // nothing and invalidates only the export's own live counts.
  const addToExport = useMutation({
    mutationFn: ({ id, ids }: { id: string; ids: string[] }) =>
      api.addToExport(id, { track_ids: ids }),
    onSuccess: (_res, vars) => {
      qc.invalidateQueries({ queryKey: ['export-contents', vars.id] })
      qc.invalidateQueries({ queryKey: ['export-tracks', vars.id] })
      qc.invalidateQueries({ queryKey: ['export-loose', vars.id] })
      const set = sets.find((s) => s.id === vars.id)
      onDone(`Added ${vars.ids.length} track${vars.ids.length === 1 ? '' : 's'} to ${set?.name ?? 'export'}`)
      setMenuOpen(false)
      onClear()
    },
    onError: (e: Error) => onError(e.message),
  })

  const add = useMutation({
    mutationFn: ({ uuid, ids }: { uuid: string; ids: string[] }) => api.addEntries(uuid, ids),
    onSuccess: (res, vars) => {
      qc.invalidateQueries({ queryKey: ['state'] })
      qc.invalidateQueries({ queryKey: ['playlists'] })
      qc.invalidateQueries({ queryKey: ['playlist', vars.uuid] })
      const pl = flat.find((p) => p.id === vars.uuid)
      onDone(`Added ${res.added} track${res.added === 1 ? '' : 's'} to ${pl?.name ?? 'playlist'}`)
      setMenuOpen(false)
      onClear()
    },
    onError: (e: Error) => onError(e.message),
  })

  if (count === 0) return null

  return (
    <div className="pointer-events-none absolute inset-x-0 bottom-4 z-20 flex justify-center">
      <div className="pointer-events-auto flex items-center gap-3 rounded-full border border-line bg-ink-800 py-2 pl-4 pr-2 shadow-xl">
        <span className="text-sm text-text">
          <span className="font-semibold tabular-nums">{count}</span> selected
        </span>
        <div className="relative">
          <button
            onClick={() => setMenuOpen((o) => !o)}
            className="rounded-full bg-accent px-3 py-1.5 text-sm font-medium text-ink-950 hover:brightness-110"
          >
            Add to… ▾
          </button>
          {menuOpen && (
            <div className="absolute bottom-full right-0 mb-2 max-h-72 w-60 overflow-y-auto rounded-lg border border-line bg-ink-850 p-1 shadow-2xl">
              <div className="px-3 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-wider text-faint">
                Playlists
              </div>
              {flat.length === 0 && (
                <div className="px-3 py-1.5 text-sm text-faint">No playlists yet</div>
              )}
              {flat.map((p) => (
                <button
                  key={p.id}
                  onClick={() => add.mutate({ uuid: p.id, ids: trackIds })}
                  className="flex w-full items-center justify-between gap-2 rounded-md px-3 py-1.5 text-left text-sm text-muted hover:bg-ink-800 hover:text-text"
                >
                  <span className="truncate">{p.name}</span>
                  <span className="shrink-0 text-[10px] tabular-nums text-faint">{p.count}</span>
                </button>
              ))}

              <div className="px-3 pb-1 pt-3 text-[10px] font-semibold uppercase tracking-wider text-faint">
                Exports
              </div>
              {sets.length === 0 && (
                <div className="px-3 py-1.5 text-sm text-faint">No exports yet</div>
              )}
              {sets.map((set) => (
                <button
                  key={set.id}
                  onClick={() => addToExport.mutate({ id: set.id, ids: trackIds })}
                  className="flex w-full items-center gap-2 rounded-md px-3 py-1.5 text-left text-sm text-muted hover:bg-ink-800 hover:text-text"
                >
                  <span className="shrink-0 text-[11px] text-gold">◈</span>
                  <span className="truncate">{set.name}</span>
                </button>
              ))}
            </div>
          )}
        </div>
        <button
          onClick={onClear}
          className="rounded-full px-2 py-1.5 text-sm text-muted hover:bg-ink-700 hover:text-text"
        >
          Clear
        </button>
      </div>
    </div>
  )
}
