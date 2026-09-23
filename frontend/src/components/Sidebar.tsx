import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type PlaylistKind, type PlaylistNode } from '../api'
import { useCaps } from '../lib/capabilities'
import { SaveBar } from './SaveBar'
import { DevicesSection } from './DevicesSection'
import { ExportsSection } from './ExportsSection'

/**
 * Which view the main table is showing.
 *
 * `device*` variants are a browsed import SOURCE — a plugged-in stick — rather
 * than the loaded collection. They are a view like any other, so the table, the
 * toolbar and the deck need no special case; what differs is that a device is
 * read-only, and that is carried by its CAPABILITIES rather than by this type,
 * so no component has to ask "am I looking at a device?" before deciding what to
 * offer.
 */
export type Source =
  | { kind: 'all' }
  | { kind: 'playlist'; id: string; name: string }
  | { kind: 'device' }
  | { kind: 'device-playlist'; id: string; name: string }
  // An export set. `export` is its root — every track it would ship, deduped —
  // and `export-playlist` is one referenced playlist inside it, read live from
  // the library rather than from the set, which is the reference doing its job.
  | { kind: 'export'; id: string; name: string }
  | { kind: 'export-playlist'; exportId: string; id: string; name: string }
  // The loose tracks — those added individually rather than via a playlist.
  // A view of its own, NOT the export root: the root is everything the export
  // would ship, which is a different question and a different answer.
  | { kind: 'export-other'; exportId: string; name: string }

interface Props {
  source: Source
  onSelect: (s: Source) => void
  onError: (msg: string) => void
  onOpenHistory: () => void
  onImport: () => void
  onSwitchLibrary: () => void
  onDone: (msg: string) => void
}

// Kind picks the icon; every behavioural question is answered by the node's own
// flags, so nothing here infers what a node can do from what it is called.
/** Every selectable playlist at or under a node, in tree order. */
function playlistIdsUnder(node: PlaylistNode): string[] {
  const here = node.kind === 'folder' ? [] : [node.id]
  return [...here, ...(node.children ?? []).flatMap(playlistIdsUnder)]
}

const icons: Record<PlaylistKind, string> = {
  folder: '▸',
  playlist: '♫',
  smart: '✦',
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
  const [adding, setAdding] = useState(false)
  const [draft, setDraft] = useState(node.name)
  const exportSets = useQuery({ queryKey: ['exports'], queryFn: api.exports }).data ?? []

  // A FOLDER contributes its nested playlists; a playlist contributes itself.
  // Folders are flattened here rather than stored as a folder reference: the
  // export stores playlist ids, and a folder is a shape in the tree, not a
  // thing with tracks.
  const addToExport = useMutation({
    mutationFn: (setId: string) =>
      api.addToExport(setId, { playlist_ids: playlistIdsUnder(node) }),
    onSuccess: (_r, setId) => {
      qc.invalidateQueries({ queryKey: ['export-contents', setId] })
      qc.invalidateQueries({ queryKey: ['export-tracks', setId] })
    },
    onError: (e: Error) => onError(e.message),
  })
  const isFolder = node.kind === 'folder'
  const selected = source.kind === 'playlist' && source.id === node.id
  const selectable = node.selectable

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
              isFolder ? 'text-faint' : node.kind === 'smart' ? 'text-pink' : 'text-accent'
            } ${isFolder && open ? 'rotate-90' : ''} transition-transform`}
          >
            {icons[node.kind]}
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

        {/* Each action gates on ITS OWN flag: a platform may allow renaming but
            not deleting. The count shows either way — it is information, not an
            action, and hiding it on a read-only library loses real data. */}
        {/* Add this playlist (or folder) to an export. Not gated on the
            library being writable: an export set is Konduktor's own curation
            and adding to one changes nothing in the user's library. */}
        {!renaming && exportSets.length > 0 && (
          <div className="relative shrink-0">
            <button
              title="Add to an export"
              onClick={() => setAdding((a) => !a)}
              className="hidden rounded px-1 text-xs text-faint hover:text-gold group-hover:block"
            >
              ◈
            </button>
            {adding && (
              <div className="absolute right-0 top-full z-30 mt-1 w-48 rounded-md border border-line bg-ink-850 p-1 shadow-2xl">
                {exportSets.map((set) => (
                  <button
                    key={set.id}
                    onClick={() => {
                      setAdding(false)
                      addToExport.mutate(set.id)
                    }}
                    className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm text-muted hover:bg-ink-800 hover:text-text"
                  >
                    <span className="shrink-0 text-[11px] text-gold">◈</span>
                    <span className="truncate">{set.name}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
        {!renaming && node.can_rename && (
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
        )}
        {!renaming && node.can_delete && (
          <button
            title="Delete playlist"
            onClick={() => {
              if (confirm(`Delete playlist "${node.name}"?`)) del.mutate()
            }}
            className="hidden shrink-0 rounded px-1 text-xs text-faint hover:text-pink group-hover:block"
          >
            ×
          </button>
        )}
        {!renaming && !isFolder && (
          <span
            className={`shrink-0 rounded bg-ink-800 px-1.5 py-0.5 text-[10px] tabular-nums text-faint ${
              node.can_rename || node.can_delete ? 'group-hover:hidden' : ''
            }`}
          >
            {node.count}
          </span>
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

export function Sidebar({
  source,
  onSelect,
  onError,
  onOpenHistory,
  onImport,
  onSwitchLibrary,
  onDone,
}: Props) {
  const qc = useQueryClient()
  // Both share a cache entry with App and SaveBar, so the header never
  // disagrees with what is loaded or with whether it has been saved.
  const library = useQuery({ queryKey: ['collection'], queryFn: api.collection }).data?.library
  const dirty = useQuery({ queryKey: ['state'], queryFn: api.state }).data?.dirty ?? false

  // Switching library throws away every unsaved edit — the adapter holds them in
  // its native model, and opening another library replaces it. Worth a confirm:
  // until now there was no way to switch at all, so this hazard is new.
  const switchLibrary = () => {
    if (
      dirty &&
      !confirm('You have unsaved changes. Opening a different library will discard them.')
    )
      return
    onSwitchLibrary()
  }
  // There is no per-node flag for "you may create a NEW playlist" — the node
  // flags describe existing nodes — so this is the library-level gate.
  const canCreate = useCaps().writable
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
      {/* Which library is open — and the way to open a different one. Until
          this existed the picker was a one-way door: it ran once at startup and
          nothing could summon it again, so changing library meant restarting.
          It doubles as the only place the app says what it currently has open. */}
      <button
        onClick={switchLibrary}
        title={library ? `${library.path} — click to open a different library` : undefined}
        className="group flex items-center gap-2 border-b border-line px-3 py-2.5 text-left hover:bg-ink-800"
      >
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium text-text">
            {library?.display_name ?? 'No library'}
          </div>
          <div className="truncate text-[11px] text-faint">
            {library
              ? `${library.name}${library.version ? ` ${library.version}` : ''}`
              : 'Choose one'}
          </div>
        </div>
        <span className="shrink-0 text-xs text-faint transition-colors group-hover:text-accent">
          ⇄
        </span>
      </button>

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
        {canCreate && (
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
        )}
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

      <ExportsSection source={source} onSelect={onSelect} onDone={onDone} onError={onError} />

      <DevicesSection
        source={source}
        onSelect={onSelect}
        onError={onError}
        onImport={onImport}
      />

      <button
        onClick={onOpenHistory}
        className="flex items-center justify-center gap-2 border-t border-line px-4 py-2 text-center text-xs text-muted hover:bg-ink-800 hover:text-text"
      >
        Collection Version History
      </button>

      <SaveBar onError={onError} />
    </aside>
  )
}
