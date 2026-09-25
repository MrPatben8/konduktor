import { useEffect, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type Drive, type PlaylistNode } from '../api'
import type { Source } from './Sidebar'
import { Icon } from '../lib/icons'

/**
 * Every drive, in the sidebar: plugged-in ones under Devices, and the computer's
 * own under an expandable Local Storage.
 *
 * A drive carrying a OneLibrary library opens as a device, the way rekordbox and
 * Traktor show a stick: its playlists appear under it and the ordinary track
 * table browses it. EVERY drive — library or not — also has a Files tree, and
 * selecting a folder there shows the audio directly in it (not its subfolders:
 * the tree is how you get to those, and a click on a drive root must never walk
 * the whole drive).
 *
 * The list genuinely changes while the app is open, so unlike everything else in
 * the sidebar this POLLS. Folder levels are fetched only when expanded.
 */

const POLL_MS = 4000

interface Props {
  source: Source
  onSelect: (s: Source) => void
  onError: (msg: string) => void
  onImport: () => void
}

export function DevicesSection({ source, onSelect, onError, onImport }: Props) {
  const qc = useQueryClient()
  // Which rows are expanded, by a key per row kind. Not persisted: a tree
  // reopening deep inside a drive that is no longer plugged in helps nobody.
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const isOpenRow = (key: string) => expanded.has(key)
  const setRow = (key: string, open: boolean) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (open) next.add(key)
      else next.delete(key)
      return next
    })

  const drives = useQuery({
    queryKey: ['drives'],
    queryFn: api.drives,
    refetchInterval: POLL_MS,
  })
  const open = useQuery({ queryKey: ['source'], queryFn: api.source })
  const openPath = open.data?.loaded ? open.data.path : null

  const playlists = useQuery({
    queryKey: ['source-playlists'],
    queryFn: api.sourcePlaylists,
    enabled: !!openPath,
  })

  const openDevice = useMutation({
    mutationFn: (path: string) => api.openSource(path),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['source'] })
      qc.invalidateQueries({ queryKey: ['source-playlists'] })
      onSelect({ kind: 'device' })
    },
    onError: (e: Error) => onError(e.message),
  })

  const eject = useMutation({
    mutationFn: () => api.closeSource(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['source'] })
      qc.removeQueries({ queryKey: ['source-tracks'] })
      qc.removeQueries({ queryKey: ['source-playlists'] })
      onSelect({ kind: 'all' })
    },
    onError: (e: Error) => onError(e.message),
  })

  const list = drives.data ?? []
  const external = list.filter((d) => d.kind === 'external')
  const local = list.filter((d) => d.kind === 'local')

  // A drive can be physically unplugged while it is open. Nothing crashes — the
  // routes just start failing — but leaving it selected would show a library
  // that is not there, so fall back to the collection. The same goes for a
  // folder on it being browsed.
  const stillPresent = !openPath || list.some((d) => d.library?.path === openPath)
  useEffect(() => {
    if (!drives.isSuccess || !openPath || stillPresent || eject.isPending) return
    eject.mutate()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openPath, stillPresent, drives.isSuccess])
  const folderGone =
    source.kind === 'folder' &&
    drives.isSuccess &&
    !list.some((d) => d.path === '/' || isInside(source.path, d.path))
  useEffect(() => {
    if (folderGone) onSelect({ kind: 'all' })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [folderGone])

  const viewingDevice = source.kind === 'device' || source.kind === 'device-playlist'

  const driveRow = (d: Drive, depth: number) => {
    const key = `drive:${d.path}`
    const expandedHere = isOpenRow(key)
    const isOpen = !!d.library && d.library.path === openPath
    const onClick = () => {
      if (d.library) {
        setRow(key, true)
        if (isOpen) onSelect({ kind: 'device' })
        else openDevice.mutate(d.library.path)
      } else {
        setRow(key, !expandedHere)
      }
    }
    return (
      <div key={d.path}>
        <TreeRow
          depth={depth}
          icon={<span className="text-gold"><Icon name="drive" size={15} /></span>}
          label={d.name}
          title={d.path}
          expanded={expandedHere}
          onToggle={() => setRow(key, !expandedHere)}
          onClick={onClick}
          selected={isOpen && source.kind === 'device'}
          trailing={
            isOpen && open.data?.tracks != null ? (
              <span className="text-[10px] text-faint">{open.data.tracks}</span>
            ) : undefined
          }
        />
        {expandedHere && (
          <>
            {isOpen &&
              (playlists.data ?? []).map((n) => (
                <DevicePlaylistRow
                  key={n.id}
                  node={n}
                  depth={depth + 1}
                  source={source}
                  onSelect={onSelect}
                />
              ))}
            <FolderRow
              path={d.path}
              name="Files"
              viewName={d.name}
              icon="folder"
              depth={depth + 1}
              source={source}
              onSelect={onSelect}
              isOpenRow={isOpenRow}
              setRow={setRow}
            />
          </>
        )}
      </div>
    )
  }

  const localKey = 'local-storage'
  const localOpen = isOpenRow(localKey)

  // Shrinkable and self-scrolling, capped at 40%: a deep Files tree must give
  // way before the playlists do, never push them off the sidebar.
  return (
    <div className="flex max-h-[40%] min-h-28 flex-col">
      <div className="mt-3 flex items-center justify-between px-4">
        <span className="text-[10px] font-semibold uppercase tracking-wider text-faint">
          Devices
        </span>
        {openPath && (
          <button
            title="Close the device and release the drive so it can be ejected"
            onClick={() => eject.mutate()}
            className="rounded px-1 text-xs text-faint hover:text-text"
          >
            ⏏
          </button>
        )}
      </div>

      <div className="mt-1 min-h-0 overflow-y-auto px-2 pb-1">
        {external.map((d) => driveRow(d, 0))}

        {local.length > 0 && (
          <>
            <TreeRow
              depth={0}
              icon={<Icon name="desktop" size={15} />}
              label="Local Storage"
              expanded={localOpen}
              onToggle={() => setRow(localKey, !localOpen)}
              onClick={() => setRow(localKey, !localOpen)}
            />
            {localOpen && local.map((d) => driveRow(d, 1))}
          </>
        )}

        {openDevice.isPending && (
          <div className="px-2 py-1.5 text-xs text-faint">Opening…</div>
        )}
        {viewingDevice && openPath && (
          <button
            onClick={onImport}
            className="mt-2 mb-1 w-full rounded-full btn-primary px-2 py-1.5 text-sm font-semibold"
          >
            Import to collection…
          </button>
        )}
      </div>
    </div>
  )
}

function isInside(path: string, root: string): boolean {
  const r = root.endsWith('/') || root.endsWith('\\') ? root : `${root}/`
  return path === root || path.startsWith(r) || path.startsWith(root + '\\')
}

/** Indent per level, capped so a deep folder still has room for its name. */
function indent(depth: number): number {
  return 4 + Math.min(depth, 8) * 12
}

/** One row of the tree: a chevron that expands, and a label that acts. */
function TreeRow({
  depth,
  icon,
  label,
  title,
  expanded,
  onToggle,
  onClick,
  selected = false,
  trailing,
}: {
  depth: number
  icon: ReactNode
  label: string
  title?: string
  expanded: boolean
  onToggle: () => void
  onClick: () => void
  selected?: boolean
  trailing?: ReactNode
}) {
  return (
    <div
      style={{ paddingLeft: indent(depth) }}
      className={`flex w-full items-center gap-1 rounded-md py-1 pr-2 text-sm transition-colors ${
        selected ? 'is-selected text-text' : 'text-muted hover:bg-ink-800 hover:text-text'
      }`}
    >
      <button
        aria-label={expanded ? 'Collapse' : 'Expand'}
        onClick={onToggle}
        className="flex h-4 w-4 shrink-0 items-center justify-center text-faint hover:text-text"
      >
        <Icon name={expanded ? 'chevronDown' : 'chevronRight'} size={12} />
      </button>
      <button onClick={onClick} title={title} className="flex min-w-0 flex-1 items-center gap-2 text-left">
        <span className="flex w-4 shrink-0 justify-center">{icon}</span>
        <span className="flex-1 truncate">{label}</span>
        {trailing}
      </button>
    </div>
  )
}

/**
 * A folder in a Files tree. Clicking it shows its audio in the table (and
 * expands it); the chevron only expands. Its subfolders are fetched the first
 * time it opens, one level at a time.
 */
function FolderRow({
  path,
  name,
  viewName,
  icon,
  depth,
  source,
  onSelect,
  isOpenRow,
  setRow,
}: {
  path: string
  name: string
  /** What the table's header calls this folder, when not its own name. */
  viewName?: string
  icon: 'folder'
  depth: number
  source: Source
  onSelect: (s: Source) => void
  isOpenRow: (key: string) => boolean
  setRow: (key: string, open: boolean) => void
}) {
  const key = `folder:${path}`
  const expanded = isOpenRow(key)
  const children = useQuery({
    queryKey: ['fs-folders', path],
    queryFn: () => api.folders(path),
    enabled: expanded,
    staleTime: 10_000,
  })
  const selected = source.kind === 'folder' && source.path === path
  return (
    <>
      <TreeRow
        depth={depth}
        icon={
          <span className={selected ? 'text-accent' : 'text-faint'}>
            <Icon name={expanded ? 'folderOpen' : icon} size={14} />
          </span>
        }
        label={name}
        title={path}
        expanded={expanded}
        onToggle={() => setRow(key, !expanded)}
        onClick={() => {
          setRow(key, true)
          onSelect({ kind: 'folder', path, name: viewName ?? name })
        }}
        selected={selected}
      />
      {expanded && (
        <>
          {children.isLoading && (
            <div style={{ paddingLeft: indent(depth + 1) + 20 }} className="py-1 text-xs text-faint">
              Loading…
            </div>
          )}
          {children.isError && (
            <div style={{ paddingLeft: indent(depth + 1) + 20 }} className="py-1 text-xs text-pink">
              Can’t read this folder
            </div>
          )}
          {(children.data ?? []).map((c) => (
            <FolderRow
              key={c.path}
              path={c.path}
              name={c.name}
              icon="folder"
              depth={depth + 1}
              source={source}
              onSelect={onSelect}
              isOpenRow={isOpenRow}
              setRow={setRow}
            />
          ))}
        </>
      )}
    </>
  )
}


/**
 * A playlist on the device. Deliberately NOT `NodeRow`: that one carries rename
 * and delete, and every one of a device's nodes reports those as false, so
 * reusing it would mean rendering controls that exist only to be disabled.
 */
function DevicePlaylistRow({
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
  const selected = source.kind === 'device-playlist' && source.id === node.id
  const isFolder = node.kind === 'folder'
  return (
    <>
      <button
        disabled={!node.selectable}
        onClick={() =>
          node.selectable && onSelect({ kind: 'device-playlist', id: node.id, name: node.name })
        }
        style={{ paddingLeft: indent(depth) + 20 }}
        className={`flex w-full items-center gap-2 rounded-md py-1 pr-2 text-left text-sm transition-colors ${
          selected
            ? 'is-selected text-text'
            : node.selectable
              ? 'text-muted hover:bg-ink-800 hover:text-text'
              : 'text-faint'
        }`}
      >
        <span className="flex w-4 justify-center text-faint">
          <Icon name={isFolder ? 'folder' : 'playlist'} size={14} />
        </span>
        <span className="flex-1 truncate">{node.name}</span>
        {!isFolder && <span className="text-[10px] text-faint">{node.count}</span>}
      </button>
      {(node.children ?? []).map((c) => (
        <DevicePlaylistRow
          key={c.id}
          node={c}
          depth={depth + 1}
          source={source}
          onSelect={onSelect}
        />
      ))}
    </>
  )
}
