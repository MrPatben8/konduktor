import { createPortal } from 'react-dom'
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type ExportSet } from '../api'
import { FolderPicker } from './FolderPicker'

/**
 * Create or edit an export: its name, its target platform and where it lands.
 *
 * All three are settled up front, and the destination especially is NOT a
 * detail deferred to export time. A Traktor `<LOCATION>` is a volume name plus
 * a volume-relative path — both derived from where the export is written — so
 * the library file genuinely cannot be built until the destination is known.
 */

interface Props {
  /** The set being edited, or null to create a new one. */
  editing: ExportSet | null
  onClose: () => void
  onSaved: (set: ExportSet, created: boolean) => void
  onError: (msg: string) => void
}

export function ExportDialog({ editing, onClose, onSaved, onError }: Props) {
  const [name, setName] = useState(editing?.name ?? '')
  const [target, setTarget] = useState(editing?.target ?? 'traktor')
  const [destination, setDestination] = useState(editing?.destination ?? '')
  const [browsing, setBrowsing] = useState(false)
  const [busy, setBusy] = useState(false)

  // Every platform, with `installed` standing for "can be an export target".
  // The unsupported ones are shown DISABLED with a reason rather than hidden:
  // an absent option reads as a missing feature, a disabled one as a roadmap.
  const targets = useQuery({ queryKey: ['export-targets'], queryFn: api.exportTargets })

  // Another set writing to the same folder would have its output wiped, because
  // an export clears its destination before writing. Caught while the folder is
  // being chosen — at run time a warning would already be too late.
  const conflict = useQuery({
    queryKey: ['export-conflict', editing?.id ?? null],
    queryFn: () => api.exportContents(editing!.id),
    enabled: !!editing,
  }).data?.destination_conflict

  const valid = name.trim() !== '' && destination.trim() !== ''

  const save = async () => {
    setBusy(true)
    try {
      const body = { name: name.trim(), target, destination: destination.trim() }
      const saved = editing
        ? await api.updateExport(editing.id, body)
        : await api.createExport(body)
      onSaved(saved, !editing)
    } catch (e) {
      onError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return createPortal(
    <>
      {browsing && (
        <FolderPicker
          value={destination}
          onChange={setDestination}
          onClose={() => setBrowsing(false)}
        />
      )}
      <div aria-modal="true" className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-[3px] p-4">
        <div className="w-full max-w-lg glass-overlay">
          <div className="border-b border-line px-5 py-3">
            <h2 className="text-sm font-semibold text-text">
              {editing ? `Edit “${editing.name}”` : 'New export'}
            </h2>
          </div>

          <div className="space-y-4 px-5 py-4 text-sm">
            <div>
              <span className="mb-1 block text-xs text-muted">Name</span>
              <input
                autoFocus
                value={name}
                placeholder="Ibiza — July"
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && valid && !busy) save()
                }}
                className="w-full rounded-md well px-2 py-1.5 text-sm text-text outline-none focus:ring-1 focus:ring-accent"
              />
            </div>

            <div>
              <span className="mb-1 block text-xs text-muted">For</span>
              <div className="space-y-1">
                {(targets.data ?? []).map((p) => (
                  <label
                    key={p.platform}
                    className={`flex items-center gap-2 rounded-md px-2 py-1.5 ${
                      p.installed ? 'cursor-pointer hover:bg-ink-850' : 'cursor-not-allowed'
                    }`}
                  >
                    <input
                      type="radio"
                      disabled={!p.installed}
                      checked={target === p.platform}
                      onChange={() => setTarget(p.platform)}
                      className="accent-accent"
                    />
                    <span className={p.installed ? 'text-text' : 'text-faint'}>{p.name}</span>
                    {!p.installed && (
                      <span className="ml-auto text-[11px] text-faint">
                        Konduktor cannot write a {p.name} library yet
                      </span>
                    )}
                  </label>
                ))}
              </div>
            </div>

            <div>
              <span className="mb-1 block text-xs text-muted">Export to</span>
              <div className="flex items-center gap-2">
                <span
                  title={destination}
                  dir="rtl"
                  className="min-w-0 flex-1 truncate rounded-md well px-2 py-1.5 font-mono text-xs text-text"
                >
                  {destination || 'Choose a folder…'}
                </span>
                <button
                  onClick={() => setBrowsing(true)}
                  className="shrink-0 btn-glass rounded-full px-3 py-1.5 text-sm text-muted hover:text-text"
                >
                  Browse…
                </button>
              </div>
              <div className="mt-1 text-[11px] text-faint">
                The folder gets a <span className="font-mono">collection.nml</span> and a copy of
                every track, in your own folder structure.
              </div>
              {conflict && (
                <div className="mt-2 rounded well px-3 py-2 text-xs text-gold">
                  “{conflict}” already exports here. Exporting replaces whatever the other one
                  wrote.
                </div>
              )}
            </div>
          </div>

          <div className="flex justify-end gap-2 border-t border-line px-5 py-3">
            <button
              onClick={onClose}
              className="btn-glass rounded-full px-3 py-1.5 text-sm text-muted hover:text-text"
            >
              Cancel
            </button>
            <button
              disabled={!valid || busy}
              onClick={save}
              className="rounded-full btn-primary px-3 py-1.5 text-sm font-semibold disabled:opacity-40"
            >
              {busy ? 'Saving…' : editing ? 'Save' : 'Create'}
            </button>
          </div>
        </div>
      </div>
    </>,
    document.body,
  )
}
