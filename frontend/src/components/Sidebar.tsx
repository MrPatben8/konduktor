import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type PlaylistNode } from '../api'
import { SaveBar } from './SaveBar'

export type Source = { kind: 'all' } | { kind: 'playlist'; id: string; name: string }

interface Props {
  source: Source
  onSelect: (s: Source) => void
  onError: (msg: string) => void
  onOpenHistory: () => void
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
  onError,
}: {
  node: PlaylistNode
  depth: number
  source: Source
  onSelect: (s: Source) => void
  onError: (msg: string) => void
}) {
  const qc = useQueryClient()
  const [open, setOpen] = useState(true)
  const [renaming, setRenaming] = useState(false)
  const [draft, setDraft] = useState(node.name)
  const isFolder = node.type === 'FOLDER'
  const selected = source.kind === 'playlist' && source.id === node.id
  const selectable = node.type === 'PLAYLIST'

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['playlists'] })
    qc.invalidateQueries({ queryKey: ['state'] })
  }

  const rename = useMutation({
    mutationFn: (name: string) => api.renamePlaylist(node.id, name),
    onSuccess: (_d, name) => {
      invalidate()
      if (source.kind === 'playlist' && source.id === node.id)
        onSelect({ kind: 'playlist', id: node.id, name })
    },
    onError: (e: Error) => onError(e.message),
  })

  const del = useMutation({
    mutationFn: () => api.deletePlaylist(node.id),
    onSuccess: () => {
      invalidate()
      if (source.kind === 'playlist' && source.id === node.id) onSelect({ kind: 'all' })
    },
    onError: (e: Error) => onError(e.message),
  })

  return (
    <div>
      <div
        className={`group flex items-center gap-1 rounded-md pr-1 text-sm transition-colors ${
          selected ? 'bg-accent-soft text-text' : 'text-muted hover:bg-ink-800 hover:text-text'
        }`}
      >
        <button
          onClick={() => {
            if (isFolder) setOpen((o) => !o)
            else if (selectable) onSelect({ kind: 'playlist', id: node.id, name: node.name })
          }}
          className={`flex min-w-0 flex-1 items-center gap-2 py-1.5 pl-2 text-left ${
            !selectable && !isFolder ? 'cursor-default opacity-70' : ''
          }`}
          style={{ paddingLeft: `${8 + depth * 14}px` }}
        >
          <span
            className={`w-3 shrink-0 text-center text-[11px] ${
              isFolder ? 'text-faint' : node.type === 'SMARTLIST' ? 'text-pink' : 'text-accent'
            } ${isFolder && open ? 'rotate-90' : ''} transition-transform`}
          >
            {icons[node.type]}
          </span>
          {renaming ? (
            <input
              autoFocus
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onClick={(e) => e.stopPropagation()}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  setRenaming(false)
                  if (draft.trim() && draft !== node.name) rename.mutate(draft.trim())
                } else if (e.key === 'Escape') {
                  setRenaming(false)
                  setDraft(node.name)
                }
              }}
              onBlur={() => {
                setRenaming(false)
                setDraft(node.name)
              }}
              className="min-w-0 flex-1 rounded border border-accent bg-ink-950 px-1 py-0 text-sm text-text outline-none"
            />
          ) : (
            <span className="flex-1 truncate">{node.name}</span>
          )}
        </button>

        {node.type === 'PLAYLIST' && !renaming && (
          <>
            <button
              title="Rename"
              onClick={() => {
                setDraft(node.name)
                setRenaming(true)
              }}
              className="hidden shrink-0 rounded px-1 text-xs text-faint hover:text-text group-hover:block"
            >
              ✎
            </button>
            <button
              title="Delete playlist"
              onClick={() => {
                if (confirm(`Delete playlist "${node.name}"?`)) del.mutate()
              }}
              className="hidden shrink-0 rounded px-1 text-xs text-faint hover:text-pink group-hover:block"
            >
              ×
            </button>
            <span className="shrink-0 rounded bg-ink-800 px-1.5 py-0.5 text-[10px] tabular-nums text-faint group-hover:hidden">
              {node.count}
            </span>
          </>
        )}
      </div>
      {isFolder && open && node.children.length > 0 && (
        <div>
          {node.children.map((c) => (
            <NodeRow
              key={c.id}
              node={c}
              depth={depth + 1}
              source={source}
              onSelect={onSelect}
              onError={onError}
            />
          ))}
        </div>
      )}
    </div>
  )
}

export function Sidebar({ source, onSelect, onError, onOpenHistory }: Props) {
  const qc = useQueryClient()
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const { data: playlists, isLoading } = useQuery({
    queryKey: ['playlists'],
    queryFn: api.playlists,
  })

  const create = useMutation({
    mutationFn: (name: string) => api.createPlaylist(name),
    onSuccess: (pl) => {
      qc.invalidateQueries({ queryKey: ['playlists'] })
      qc.invalidateQueries({ queryKey: ['state'] })
      onSelect({ kind: 'playlist', id: pl.id, name: pl.name })
    },
    onError: (e: Error) => onError(e.message),
  })

  const submitNew = () => {
    setCreating(false)
    if (newName.trim()) create.mutate(newName.trim())
    setNewName('')
  }

  return (
    <aside className="flex h-full w-64 shrink-0 flex-col border-r border-line bg-ink-900">
      <div className="px-2 pt-3">
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

      <div className="mt-3 flex items-center justify-between px-4">
        <span className="text-[10px] font-semibold uppercase tracking-wider text-faint">
          Playlists
        </span>
        <button
          title="New playlist"
          onClick={() => {
            setCreating(true)
            setNewName('')
          }}
          className="rounded px-1 text-sm text-faint hover:text-text"
        >
          +
        </button>
      </div>

      <div className="mt-1 flex-1 overflow-y-auto px-2 pb-4">
        {creating && (
          <input
            autoFocus
            value={newName}
            placeholder="Playlist name…"
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') submitNew()
              else if (e.key === 'Escape') {
                setCreating(false)
                setNewName('')
              }
            }}
            onBlur={submitNew}
            className="mb-1 w-full rounded-md border border-accent bg-ink-950 px-2 py-1.5 text-sm text-text outline-none"
          />
        )}
        {isLoading && <div className="px-2 py-2 text-sm text-faint">Loading…</div>}
        {playlists?.map((n) => (
          <NodeRow
            key={n.id}
            node={n}
            depth={0}
            source={source}
            onSelect={onSelect}
            onError={onError}
          />
        ))}
      </div>

      <button
        onClick={onOpenHistory}
        className="flex items-center gap-2 border-t border-line px-4 py-2 text-left text-xs text-muted hover:bg-ink-800 hover:text-text"
      >
        <span className="text-[11px] text-faint">🕑</span>
        Version history
      </button>

      <SaveBar onError={onError} />
    </aside>
  )
}
