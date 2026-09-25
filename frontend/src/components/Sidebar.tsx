import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type PlaylistKind, type PlaylistNode } from '../api'
import { useCaps } from '../lib/capabilities'
import { SaveBar } from './SaveBar'
import { SettingsMenu } from './SettingsMenu'
import { DevicesSection } from './DevicesSection'
import { ExportsSection } from './ExportsSection'
import { ConfirmDialog, type ConfirmRequest } from './ConfirmDialog'
import { ContextMenu, type MenuItem } from './ContextMenu'
import { Icon, type IconName } from '../lib/icons'

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
  onOpenPathMapping: () => void
}

// Kind picks the icon; every behavioural question is answered by the node's own
// flags, so nothing here infers what a node can do from what it is called.
/** Every selectable playlist at or under a node, in tree order. */
function playlistIdsUnder(node: PlaylistNode): string[] {
  const here = node.kind === 'folder' ? [] : [node.id]
  return [...here, ...(node.children ?? []).flatMap(playlistIdsUnder)]
}

/** The confirm wording for deleting a node. A folder says what goes with it,
 *  since the whole subtree is deleted and the row only shows the folder. */
function deleteRequest(node: PlaylistNode) {
  if (node.kind !== 'folder')
    return {
      title: 'Delete playlist',
      body: `Delete "${node.name}"? Its tracks stay in your collection.`,
      confirmLabel: 'Delete playlist',
    }
  const playlists = playlistIdsUnder(node).length
  const inside =
    playlists === 0
      ? 'It is empty.'
      : `The ${playlists === 1 ? 'playlist' : `${playlists} playlists`} inside it will be deleted too; their tracks stay in your collection.`
  return {
    title: 'Delete folder',
    body: `Delete "${node.name}"? ${inside}`,
    confirmLabel: 'Delete folder',
  }
}

const icons: Record<PlaylistKind, IconName> = {
  folder: 'chevronRight',
  playlist: 'playlist',
  smart: 'smart',
}

/** What the sidebar is naming in place: a new node, and the folder it goes in
 *  (`null` = the top level). Nothing is created until the name is committed. */
type Draft = { kind: 'playlist' | 'folder'; parentId: string | null }

/** Actions a row can ask of the sidebar. They live there, not in the row, so
 *  the right-click menu and the hover buttons run the very same code. */
interface RowActions {
  onContextMenu: (e: React.MouseEvent, node: PlaylistNode) => void
  onRename: (node: PlaylistNode, name: string) => void
  onDelete: (node: PlaylistNode) => void
  onAddToExport: (node: PlaylistNode, setId: string) => void
  /** The row's export button: the export list as a menu under it. */
  onPickExport: (e: React.MouseEvent<HTMLElement>, node: PlaylistNode) => void
  onCommitDraft: (name: string) => void
  onCancelDraft: () => void
}

/** The inline name field for a node being created. Enter or clicking away
 *  commits a non-empty name; Esc (or an empty name) creates nothing. */
function DraftRow({
  draft,
  depth,
  actions,
}: {
  draft: Draft
  depth: number
  actions: RowActions
}) {
  const [name, setName] = useState('')
  const done = useRef(false)
  const finish = (commit: boolean) => {
    if (done.current) return
    done.current = true
    if (commit && name.trim()) actions.onCommitDraft(name.trim())
    else actions.onCancelDraft()
  }
  return (
    <div
      className="flex items-center gap-2 py-1 pr-1 text-sm"
      style={{ paddingLeft: `${8 + depth * 14}px` }}
    >
      <span
        className={`flex w-4 shrink-0 justify-center ${
          draft.kind === 'folder' ? 'text-faint' : 'text-accent'
        }`}
      >
        <Icon name={icons[draft.kind]} size={draft.kind === 'folder' ? 12 : 15} />
      </span>
      <input
        autoFocus
        value={name}
        placeholder={draft.kind === 'folder' ? 'Folder name…' : 'Playlist name…'}
        onChange={(e) => setName(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') finish(true)
          else if (e.key === 'Escape') finish(false)
        }}
        onBlur={() => finish(true)}
        className="min-w-0 flex-1 rounded-md bg-well px-1 py-0 text-sm text-text outline-none ring-1 ring-accent"
      />
    </div>
  )
}

function NodeRow({
  node,
  depth,
  source,
  onSelect,
  renamingId,
  setRenamingId,
  draft,
  exportSets,
  actions,
}: {
  node: PlaylistNode
  depth: number
  source: Source
  onSelect: (s: Source) => void
  renamingId: string | null
  setRenamingId: (id: string | null) => void
  draft: Draft | null
  exportSets: { id: string; name: string }[]
  actions: RowActions
}) {
  const [open, setOpen] = useState(true)
  const renaming = renamingId === node.id
  const [renameDraft, setRenameDraft] = useState(node.name)
  const isFolder = node.kind === 'folder'
  const selected = source.kind === 'playlist' && source.id === node.id
  const selectable = node.selectable
  // A folder being created into shows its children, or the draft would be
  // typed into a collapsed folder the user cannot see.
  const draftHere = draft !== null && draft.parentId === node.id
  const expanded = isFolder && (open || draftHere)

  const startRename = () => {
    setRenameDraft(node.name)
    setRenamingId(node.id)
  }

  return (
    <div>
      <div
        onContextMenu={(e) => actions.onContextMenu(e, node)}
        className={`group flex items-center gap-1 rounded-[10px] pr-1 text-sm transition-colors ${
          selected ? 'is-selected font-medium text-text' : 'text-muted hover:bg-ink-800 hover:text-text'
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
            className={`flex w-4 shrink-0 justify-center ${
              isFolder ? 'text-faint' : node.kind === 'smart' ? 'text-pink' : selected ? 'text-accent' : 'text-muted'
            } ${expanded ? 'rotate-90' : ''} transition-transform`}
          >
            <Icon
              name={icons[node.kind]}
              size={isFolder ? 12 : 15}
              strokeWidth={isFolder ? 2.4 : 1.8}
            />
          </span>
          {isFolder && (
            <span className="-ml-1 flex shrink-0 text-muted">
              <Icon name={expanded ? 'folderOpen' : 'folder'} size={15} />
            </span>
          )}
          {renaming ? (
            <input
              autoFocus
              value={renameDraft}
              onChange={(e) => setRenameDraft(e.target.value)}
              onClick={(e) => e.stopPropagation()}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  setRenamingId(null)
                  if (renameDraft.trim() && renameDraft.trim() !== node.name)
                    actions.onRename(node, renameDraft.trim())
                } else if (e.key === 'Escape') {
                  setRenamingId(null)
                }
              }}
              onBlur={() => setRenamingId(null)}
              className="min-w-0 flex-1 rounded-md bg-well px-1 py-0 text-sm text-text outline-none ring-1 ring-accent"
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
          <button
            title="Add to an export"
            onClick={(e) => actions.onPickExport(e, node)}
            className="hidden shrink-0 rounded px-1 text-faint hover:text-gold group-hover:block"
          >
            <Icon name="export" size={13} />
          </button>
        )}
        {!renaming && node.can_rename && (
          <button
            title="Rename"
            onClick={startRename}
            className="hidden shrink-0 rounded px-1 text-faint hover:text-text group-hover:block"
          >
            <Icon name="pencil" size={13} />
          </button>
        )}
        {!renaming && node.can_delete && (
          <button
            title={isFolder ? 'Delete folder' : 'Delete playlist'}
            onClick={() => actions.onDelete(node)}
            className="hidden shrink-0 rounded px-1 text-faint hover:text-pink group-hover:block"
          >
            <Icon name="close" size={13} />
          </button>
        )}
        {!renaming && !isFolder && (
          <span
            className={`shrink-0 px-1.5 font-mono text-[11px] text-faint ${
              node.can_rename || node.can_delete ? 'group-hover:hidden' : ''
            }`}
          >
            {node.count}
          </span>
        )}
      </div>
      {expanded && (
        <div>
          {draftHere && <DraftRow draft={draft} depth={depth + 1} actions={actions} />}
          {node.children.map((c) => (
            <NodeRow
              key={c.id}
              node={c}
              depth={depth + 1}
              source={source}
              onSelect={onSelect}
              renamingId={renamingId}
              setRenamingId={setRenamingId}
              draft={draft}
              exportSets={exportSets}
              actions={actions}
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
  onOpenPathMapping,
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
  const foldersSupported = useCaps().playlists.folders
  const { data: playlists, isLoading } = useQuery({
    queryKey: ['playlists'],
    queryFn: api.playlists,
  })
  const exportSets = useQuery({ queryKey: ['exports'], queryFn: api.exports }).data ?? []

  const [draft, setDraft] = useState<Draft | null>(null)
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [menu, setMenu] = useState<{ x: number; y: number; items: MenuItem[] } | null>(null)
  const [confirmReq, setConfirmReq] = useState<ConfirmRequest | null>(null)

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['playlists'] })
    qc.invalidateQueries({ queryKey: ['state'] })
  }

  const create = useMutation({
    mutationFn: ({ kind, parentId, name }: Draft & { name: string }) =>
      kind === 'folder'
        ? api.createFolder(name, parentId ?? undefined)
        : api.createPlaylist(name, parentId ?? undefined),
    onSuccess: (node) => {
      invalidate()
      if (node.kind === 'playlist') onSelect({ kind: 'playlist', id: node.id, name: node.name })
    },
    onError: (e: Error) => onError(e.message),
  })

  const rename = useMutation({
    mutationFn: ({ node, name }: { node: PlaylistNode; name: string }) =>
      api.renamePlaylist(node.id, name),
    onSuccess: (_d, { node, name }) => {
      invalidate()
      if (source.kind === 'playlist' && source.id === node.id)
        onSelect({ kind: 'playlist', id: node.id, name })
    },
    onError: (e: Error) => onError(e.message),
  })

  const del = useMutation({
    mutationFn: (node: PlaylistNode) => api.deletePlaylist(node.id),
    onSuccess: (_d, node) => {
      invalidate()
      // A folder takes its nested playlists with it, so the open view may be
      // one of those rather than the node itself.
      if (source.kind === 'playlist' && playlistIdsUnder(node).concat(node.id).includes(source.id))
        onSelect({ kind: 'all' })
    },
    onError: (e: Error) => onError(e.message),
  })

  // A FOLDER contributes its nested playlists; a playlist contributes itself.
  // Folders are flattened here rather than stored as a folder reference: the
  // export stores playlist ids, and a folder is a shape in the tree, not a
  // thing with tracks.
  const addToExport = useMutation({
    mutationFn: ({ node, setId }: { node: PlaylistNode; setId: string }) =>
      api.addToExport(setId, { playlist_ids: playlistIdsUnder(node) }),
    onSuccess: (_r, { setId }) => {
      qc.invalidateQueries({ queryKey: ['export-contents', setId] })
      qc.invalidateQueries({ queryKey: ['export-tracks', setId] })
    },
    onError: (e: Error) => onError(e.message),
  })

  const startDraft = (kind: Draft['kind'], parentId: string | null) => {
    setRenamingId(null)
    setDraft({ kind, parentId })
  }

  // Creating is the library-level gate (there is no per-node "may create"
  // flag); WHERE is the node's own `can_contain_children`; and a folder also
  // needs the platform to have folders at all.
  const createItems = (parentId: string | null): MenuItem[] =>
    canCreate
      ? [
          { label: 'New Playlist', icon: <Icon name="playlist" size={13} />, onClick: () => startDraft('playlist', parentId) },
          ...(foldersSupported
            ? [{ label: 'New Folder', icon: <Icon name="folder" size={13} />, onClick: () => startDraft('folder', parentId) }]
            : []),
        ]
      : []

  const confirmDelete = (node: PlaylistNode) =>
    setConfirmReq({ ...deleteRequest(node), onConfirm: async () => void (await del.mutateAsync(node)) })

  const nodeItems = (node: PlaylistNode): MenuItem[] => {
    const groups: MenuItem[][] = [
      node.can_contain_children ? createItems(node.id) : [],
      exportSets.length > 0
        ? [
            {
              label: 'Add to export',
              submenu: exportSets.map((set) => ({
                label: set.name,
                icon: <Icon name="export" size={13} />,
                onClick: () => addToExport.mutate({ node, setId: set.id }),
              })),
            },
          ]
        : [],
      [
        ...(node.can_rename
          ? [{ label: 'Rename', onClick: () => { setDraft(null); setRenamingId(node.id) } }]
          : []),
        ...(node.can_delete
          ? [
              {
                label: node.kind === 'folder' ? 'Delete Folder…' : 'Delete Playlist…',
                danger: true,
                onClick: () => confirmDelete(node),
              },
            ]
          : []),
      ],
    ]
    return groups
      .filter((g) => g.length > 0)
      .flatMap((g, i) => (i === 0 ? g : [{ separator: true } as MenuItem, ...g]))
  }

  const openMenu = (e: React.MouseEvent, items: MenuItem[]) => {
    e.preventDefault()
    e.stopPropagation()
    if (items.length > 0) setMenu({ x: e.clientX, y: e.clientY, items })
  }

  const actions: RowActions = {
    onContextMenu: (e, node) => openMenu(e, nodeItems(node)),
    onRename: (node, name) => rename.mutate({ node, name }),
    onDelete: confirmDelete,
    onAddToExport: (node, setId) => addToExport.mutate({ node, setId }),
    // Opened as the shared ContextMenu rather than a popover inside the row: the
    // playlist list scrolls, and a popover inside it was clipped by its edges.
    onPickExport: (e, node) => {
      e.stopPropagation()
      const r = e.currentTarget.getBoundingClientRect()
      setMenu({
        x: r.left,
        y: r.bottom + 4,
        items: [
          { heading: 'Add to export' },
          ...exportSets.map((set) => ({
            label: set.name,
            icon: <Icon name="export" size={13} />,
            onClick: () => addToExport.mutate({ node, setId: set.id }),
          })),
        ],
      })
    },
    onCommitDraft: (name) => {
      if (draft) create.mutate({ ...draft, name })
      setDraft(null)
    },
    onCancelDraft: () => setDraft(null),
  }

  return (
    <aside className="glass flex h-full w-64 shrink-0 flex-col">
      {/* Which library is open — and the way to open a different one. Until
          this existed the picker was a one-way door: it ran once at startup and
          nothing could summon it again, so changing library meant restarting.
          It doubles as the only place the app says what it currently has open. */}
      <button
        onClick={switchLibrary}
        title={library ? `${library.path} — click to open a different library` : undefined}
        className="btn-glass group m-2.5 mb-0 flex items-center gap-2.5 rounded-[14px] px-2.5 py-2 text-left"
      >
        <span
          aria-hidden
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-[13px] font-bold text-white shadow-[inset_0_1px_0_rgb(255_255_255/0.4)]"
          style={{ background: 'linear-gradient(135deg, var(--amb-1), var(--amb-3))' }}
        >
          {(library?.name ?? 'K').slice(0, 1).toUpperCase()}
        </span>
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
        <span className="shrink-0 text-faint transition-colors group-hover:text-accent">
          <Icon name="switch" size={14} strokeWidth={2} />
        </span>
      </button>

      <div className="px-2 pt-3">
        <button
          onClick={() => onSelect({ kind: 'all' })}
          className={`flex w-full items-center gap-2 rounded-[10px] px-2 py-1.5 text-left text-sm transition-colors ${
            source.kind === 'all'
              ? 'is-selected font-medium text-text'
              : 'text-muted hover:bg-ink-800 hover:text-text'
          }`}
        >
          <span className={`flex w-4 justify-center ${source.kind === 'all' ? 'text-accent' : ''}`}>
            <Icon name="music" size={15} />
          </span>
          <span className="flex-1">All Tracks</span>
        </button>
      </div>

      <div className="mt-3 px-4">
        <span className="text-[11px] font-semibold uppercase tracking-wider text-faint">
          Playlists
        </span>
      </div>

      {/* Right-click here (below the rows) creates at the top level; rows
          stop the event so they get their own menu. */}
      <div
        className="mt-1 flex-1 overflow-y-auto px-2 pb-4"
        onContextMenu={(e) => openMenu(e, createItems(null))}
      >
        {draft?.parentId === null && <DraftRow draft={draft} depth={0} actions={actions} />}
        {isLoading && <div className="px-2 py-2 text-sm text-faint">Loading…</div>}
        {playlists?.map((n) => (
          <NodeRow
            key={n.id}
            node={n}
            depth={0}
            source={source}
            onSelect={onSelect}
            renamingId={renamingId}
            setRenamingId={setRenamingId}
            draft={draft}
            exportSets={exportSets}
            actions={actions}
          />
        ))}
      </div>

      {menu && <ContextMenu {...menu} onClose={() => setMenu(null)} />}
      {confirmReq && <ConfirmDialog {...confirmReq} onClose={() => setConfirmReq(null)} />}

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
        <Icon name="history" size={13} />
        Collection Version History
      </button>

      <SaveBar onError={onError} trailing={<SettingsMenu up onOpenPathMapping={onOpenPathMapping} />} />
    </aside>
  )
}
