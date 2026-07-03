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
    ...(n.type === 'PLAYLIST' ? [n] : []),
    ...flattenPlaylists(n.children),
  ])
}

// Floating action bar shown when tracks are selected in the explorer.
export function SelectionBar({ count, trackIds, onClear, onDone, onError }: Props) {
  const qc = useQueryClient()
  const [menuOpen, setMenuOpen] = useState(false)
  const { data: playlists } = useQuery({ queryKey: ['playlists'], queryFn: api.playlists })
  const flat = playlists ? flattenPlaylists(playlists) : []

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
            Add to playlist ▾
          </button>
          {menuOpen && (
            <div className="absolute bottom-full right-0 mb-2 max-h-72 w-56 overflow-y-auto rounded-lg border border-line bg-ink-850 p-1 shadow-2xl">
              {flat.length === 0 && (
                <div className="px-3 py-2 text-sm text-faint">No playlists yet</div>
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
