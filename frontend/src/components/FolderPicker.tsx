import { useState } from 'react'
import { FileBrowser, useFsListing } from './FileBrowser'

/**
 * Pick a FOLDER, by browsing.
 *
 * The browsing itself is `FileBrowser` — the same one the collection picker
 * uses. All this adds is the modal frame and the confirm row, which is the part
 * that genuinely differs: here you are choosing somewhere to PUT files, so the
 * target can be a folder that does not exist yet.
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
  const [newFolder, setNewFolder] = useState('')
  // Shares FileBrowser's query, so "where am I" cannot disagree with what is
  // on screen — and the confirm row below needs it to name its target.
  const here = useFsListing(dir).data?.path ?? ''

  const confirm = () => {
    onChange(newFolder.trim() ? `${here}/${newFolder.trim()}` : here)
    onClose()
  }

  return (
    <div aria-modal="true" className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 p-4">
      <div className="flex h-[70vh] w-full max-w-2xl flex-col overflow-hidden rounded-lg border border-line bg-ink-900 shadow-xl">
        <div className="border-b border-line px-5 py-3">
          <h2 className="text-sm font-semibold text-text">Choose a folder</h2>
        </div>

        <FileBrowser
          mode="directory"
          path={dir}
          onNavigate={setDir}
          footer={
            /* A folder that does not exist yet is a normal thing to want — the
               import creates it, so it only has to be named, not made here. */
            <div className="flex items-center gap-2 border-t border-line px-4 py-2">
              <span className="shrink-0 text-xs text-faint">New folder</span>
              <input
                value={newFolder}
                placeholder="name…"
                onChange={(e) => setNewFolder(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && newFolder.trim() && here) confirm()
                }}
                className="min-w-0 flex-1 rounded-md border border-line bg-ink-950 px-2 py-1 text-xs text-text outline-none focus:border-accent"
              />
            </div>
          }
        />

        <div className="flex items-center gap-2 border-t border-line px-5 py-3">
          {/* Always show where the files will actually land, so the effect of
              typing a new-folder name never has to be inferred. */}
          <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-faint" dir="rtl">
            {here ? (newFolder.trim() ? `${here}/${newFolder.trim()}` : here) : ''}
          </span>
          <button
            onClick={onClose}
            className="shrink-0 rounded-md border border-line px-3 py-1.5 text-sm text-muted hover:text-text"
          >
            Cancel
          </button>
          <button
            disabled={!here}
            onClick={confirm}
            className="shrink-0 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-ink-950 hover:brightness-110 disabled:opacity-40"
          >
            Use this folder
          </button>
        </div>
      </div>
    </div>
  )
}
