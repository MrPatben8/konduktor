import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api'

/**
 * Pick a FOLDER, by browsing.
 *
 * `/api/fs/list` already returns both directories and openable library files —
 * it was built for the collection picker — so this reuses it and simply ignores
 * the files. A folder browser that listed `.nml` files would be offering
 * something that cannot be chosen.
 *
 * Navigating into a folder also SELECTS it. There is no separate "choose"
 * click, because the thing being picked is the place you are standing: a picker
 * that made you navigate in and then confirm would read as two decisions when
 * there is one.
 */

interface Props {
  value: string
  onChange: (path: string) => void
  onClose: () => void
}

export function FolderPicker({ value, onChange, onClose }: Props) {
  // `undefined` means "wherever the server thinks home is" — the picker does not
  // need to know that path to open on it.
  const [dir, setDir] = useState<string | undefined>(value || undefined)
  const listing = useQuery({
    queryKey: ['fs', dir ?? 'home'],
    queryFn: () => api.listDir(dir),
  })
  const data = listing.data
  const [newFolder, setNewFolder] = useState('')

  const here = data?.path ?? ''

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 p-4">
      <div className="flex h-[70vh] w-full max-w-lg flex-col rounded-lg border border-line bg-ink-900 shadow-xl">
        <div className="border-b border-line px-5 py-3">
          <h2 className="text-sm font-semibold text-text">Choose a folder</h2>
        </div>

        <div className="flex items-center gap-2 border-b border-line bg-ink-850 px-4 py-2">
          <button
            title="Home"
            onClick={() => setDir(data?.home)}
            className="rounded px-2 py-1 text-sm text-muted hover:bg-ink-800 hover:text-text"
          >
            ⌂
          </button>
          <button
            title="Up one level"
            disabled={!data?.parent}
            onClick={() => data?.parent && setDir(data.parent)}
            className="rounded px-2 py-1 text-sm text-muted hover:bg-ink-800 hover:text-text disabled:cursor-not-allowed disabled:opacity-40"
          >
            ↑
          </button>
          <span className="truncate font-mono text-xs text-faint" dir="rtl">
            {here || 'Loading…'}
          </span>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          {listing.isLoading && <div className="p-4 text-sm text-faint">Loading…</div>}
          {data && data.dirs.length === 0 && (
            <div className="p-4 text-sm text-faint">No folders here.</div>
          )}
          {data?.dirs.map((d) => (
            <button
              key={d.path}
              onClick={() => setDir(d.path)}
              className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm text-muted hover:bg-ink-800 hover:text-text"
            >
              <span className="text-faint">📁</span>
              <span className="truncate">{d.name}</span>
            </button>
          ))}
        </div>

        {/* A folder that does not exist yet is a normal thing to want — the
            import creates it, so it only has to be named, not made here. */}
        <div className="flex items-center gap-2 border-t border-line px-4 py-2">
          <span className="text-xs text-faint">New folder</span>
          <input
            value={newFolder}
            placeholder="name…"
            onChange={(e) => setNewFolder(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && newFolder.trim() && here) {
                onChange(`${here}/${newFolder.trim()}`)
                onClose()
              }
            }}
            className="min-w-0 flex-1 rounded-md border border-line bg-ink-950 px-2 py-1 text-xs text-text outline-none focus:border-accent"
          />
        </div>

        <div className="flex justify-end gap-2 border-t border-line px-5 py-3">
          <button
            onClick={onClose}
            className="rounded-md border border-line px-3 py-1.5 text-sm text-muted hover:text-text"
          >
            Cancel
          </button>
          <button
            disabled={!here}
            onClick={() => {
              onChange(newFolder.trim() ? `${here}/${newFolder.trim()}` : here)
              onClose()
            }}
            className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-ink-950 hover:brightness-110 disabled:opacity-40"
          >
            Use this folder
          </button>
        </div>
      </div>
    </div>
  )
}
