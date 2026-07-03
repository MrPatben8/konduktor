import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type PlaylistNode } from '../api'

export type Source = { kind: 'all' } | { kind: 'playlist'; id: string; name: string }

interface Props {
  source: Source
  onSelect: (s: Source) => void
}

const icons: Record<string, string> = {
  FOLDER: '▸',
  PLAYLIST: '♫',
  SMARTLIST: '✦',
}

function NodeRow({
  node,
  depth,
  source,
  onSelect,
}: {
  node: PlaylistNode
  depth: number
  source: Source
  onSelect: (s: Source) => void
}) {
  const [open, setOpen] = useState(true)
  const isFolder = node.type === 'FOLDER'
  const selected = source.kind === 'playlist' && source.id === node.id
  const selectable = node.type === 'PLAYLIST'

  return (
    <div>
      <button
        onClick={() => {
          if (isFolder) setOpen((o) => !o)
          else if (selectable) onSelect({ kind: 'playlist', id: node.id, name: node.name })
        }}
        className={`group flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors ${
          selected
            ? 'bg-accent-soft text-text'
            : 'text-muted hover:bg-ink-800 hover:text-text'
        } ${!selectable && !isFolder ? 'cursor-default opacity-70' : ''}`}
        style={{ paddingLeft: `${8 + depth * 14}px` }}
      >
        <span
          className={`w-3 shrink-0 text-center text-[11px] ${
            isFolder ? 'text-faint' : node.type === 'SMARTLIST' ? 'text-pink' : 'text-accent'
          } ${isFolder && open ? 'rotate-90' : ''} transition-transform`}
        >
          {icons[node.type]}
        </span>
        <span className="flex-1 truncate">{node.name}</span>
        {node.type === 'PLAYLIST' && (
          <span className="shrink-0 rounded bg-ink-800 px-1.5 py-0.5 text-[10px] tabular-nums text-faint group-hover:bg-ink-700">
            {node.count}
          </span>
        )}
      </button>
      {isFolder && open && node.children.length > 0 && (
        <div>
          {node.children.map((c) => (
            <NodeRow
              key={c.id}
              node={c}
              depth={depth + 1}
              source={source}
              onSelect={onSelect}
            />
          ))}
        </div>
      )}
    </div>
  )
}

export function Sidebar({ source, onSelect }: Props) {
  const { data: playlists, isLoading } = useQuery({
    queryKey: ['playlists'],
    queryFn: api.playlists,
  })

  return (
    <aside className="flex h-full w-64 shrink-0 flex-col border-r border-line bg-ink-900">
      <div className="flex items-center gap-2 px-4 py-4">
        <div className="flex h-7 w-7 items-center justify-center rounded-md bg-accent text-sm font-bold text-ink-950">
          K
        </div>
        <div className="text-[15px] font-semibold tracking-tight">konduktor</div>
      </div>

      <div className="px-2">
        <button
          onClick={() => onSelect({ kind: 'all' })}
          className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors ${
            source.kind === 'all'
              ? 'bg-accent-soft text-text'
              : 'text-muted hover:bg-ink-800 hover:text-text'
          }`}
        >
          <span className="w-3 text-center text-[11px] text-mint">◈</span>
          <span className="flex-1">All Tracks</span>
        </button>
      </div>

      <div className="mt-3 px-4 text-[10px] font-semibold uppercase tracking-wider text-faint">
        Playlists
      </div>
      <div className="mt-1 flex-1 overflow-y-auto px-2 pb-4">
        {isLoading && <div className="px-2 py-2 text-sm text-faint">Loading…</div>}
        {playlists?.map((n) => (
          <NodeRow
            key={n.id}
            node={n}
            depth={0}
            source={source}
            onSelect={onSelect}
          />
        ))}
      </div>
    </aside>
  )
}
