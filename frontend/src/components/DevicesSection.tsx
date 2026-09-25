import { useEffect } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type PlaylistNode } from '../api'
import type { Source } from './Sidebar'
import { Icon } from '../lib/icons'

/**
 * Plugged-in libraries, in the sidebar, the way rekordbox and Traktor show them.
 *
 * The list genuinely changes while the app is open — a device is whatever stick
 * is mounted — so unlike everything else in the sidebar this POLLS. It is a
 * directory stat per mount point, which is cheap enough to do every few seconds
 * and the only way a drive can appear without the user reloading.
 *
 * Browsing a device reuses the ordinary track table and deck: the adapter
 * projects a drive into the same generic model the collection uses, so there is
 * nothing here that needs its own table, its own row or its own player.
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

  const devices = useQuery({
    queryKey: ['sources'],
    queryFn: api.sources,
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

  // A drive can be physically unplugged while it is open. Nothing crashes — the
  // routes just start failing — but leaving it selected would show a library
  // that is not there, so fall back to the collection.
  const stillPresent =
    !openPath || (devices.data ?? []).some((d) => d.path === openPath)
  useEffect(() => {
    if (!openPath || stillPresent || eject.isPending) return
    eject.mutate()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openPath, stillPresent])

  const list = devices.data ?? []
  if (list.length === 0 && !openPath) return null

  const viewingDevice = source.kind === 'device' || source.kind === 'device-playlist'

  return (
    <>
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

      <div className="mt-1 px-2">
        {list.map((d) => {
          const isOpen = d.path === openPath
          return (
            <div key={d.path}>
              <button
                onClick={() =>
                  isOpen ? onSelect({ kind: 'device' }) : openDevice.mutate(d.path)
                }
                className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors ${
                  isOpen && source.kind === 'device'
                    ? 'is-selected text-text'
                    : 'text-muted hover:bg-ink-800 hover:text-text'
                }`}
              >
                <span className="flex w-4 justify-center text-gold"><Icon name="drive" size={15} /></span>
                <span className="flex-1 truncate" title={d.path}>
                  {d.label}
                </span>
                {isOpen && open.data?.tracks != null && (
                  <span className="text-[10px] text-faint">{open.data.tracks}</span>
                )}
              </button>

              {isOpen &&
                (playlists.data ?? []).map((n) => (
                  <DevicePlaylistRow
                    key={n.id}
                    node={n}
                    depth={1}
                    source={source}
                    onSelect={onSelect}
                  />
                ))}
            </div>
          )
        })}

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
        style={{ paddingLeft: 8 + depth * 12 }}
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
