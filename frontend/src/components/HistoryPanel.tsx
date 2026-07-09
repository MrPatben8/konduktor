import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type HistoryEntry } from '../api'
import type { ToastMsg } from './Toast'

interface Props {
  onClose: () => void
  onNotify: (kind: ToastMsg['kind'], text: string) => void
  onError: (msg: string) => void
}

function formatWhen(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/**
 * Version history browser for the current collection. Every save is a commit in
 * a purely-local git repo (see backend history.py); this lists them newest-first
 * and lets the user restore any past version (written back as a new forward save)
 * or wipe the entire history. Restore overwrites the collection on disk, so the
 * user must close Traktor first (it rewrites the .nml on exit).
 */
export function HistoryPanel({ onClose, onNotify, onError }: Props) {
  const qc = useQueryClient()
  const history = useQuery({ queryKey: ['history'], queryFn: api.history })
  const [confirmingId, setConfirmingId] = useState<string | null>(null)
  const [clearing, setClearing] = useState(false)

  const restore = useMutation({
    mutationFn: (id: string) => api.restoreVersion(id),
    onSuccess: () => {
      // A restore swaps the whole collection — refetch everything.
      qc.invalidateQueries()
      onNotify('success', 'Restored — the library now reflects that version.')
      setConfirmingId(null)
      onClose()
    },
    onError: (e: Error) => {
      onError(e.message)
      setConfirmingId(null)
    },
  })

  const clear = useMutation({
    mutationFn: () => api.clearHistory(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['history'] })
      onNotify('success', 'Version history deleted.')
      setClearing(false)
    },
    onError: (e: Error) => {
      onError(e.message)
      setClearing(false)
    },
  })

  const entries = history.data ?? []

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 p-6"
      onClick={onClose}
    >
      <div
        className="flex max-h-[85vh] w-full max-w-xl flex-col overflow-hidden rounded-xl border border-line bg-ink-900 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="border-b border-line px-5 py-4">
          <div className="text-[15px] font-semibold tracking-tight">Version History</div>
          <div className="text-xs text-muted">
            Every save is a restorable version of this collection. Close Traktor before
            restoring — it overwrites the collection on exit.
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {history.isLoading ? (
            <div className="py-8 text-center text-sm text-faint">Loading…</div>
          ) : entries.length === 0 ? (
            <div className="py-8 text-center text-sm text-faint">
              No versions yet. Saving a change records the first one.
            </div>
          ) : (
            <ul className="space-y-1.5">
              {entries.map((e: HistoryEntry, i: number) => {
                const isCurrent = i === 0
                const isConfirming = confirmingId === e.id
                return (
                  <li
                    key={e.id}
                    className="group flex items-center gap-3 rounded-md border border-line bg-ink-850 px-3 py-2"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="truncate text-sm text-text">{e.summary}</span>
                        {isCurrent && (
                          <span className="shrink-0 rounded bg-mint/15 px-1.5 py-0.5 text-[10px] font-medium text-mint">
                            current
                          </span>
                        )}
                      </div>
                      <div className="text-[11px] text-faint">
                        {formatWhen(e.timestamp)} · {e.id.slice(0, 8)}
                      </div>
                    </div>
                    {isConfirming ? (
                      <div className="flex shrink-0 items-center gap-1.5">
                        <button
                          onClick={() => restore.mutate(e.id)}
                          disabled={restore.isPending}
                          className="rounded-md bg-gold px-2.5 py-1 text-xs font-medium text-ink-950 hover:brightness-110 disabled:opacity-50"
                        >
                          {restore.isPending ? 'Restoring…' : 'Restore'}
                        </button>
                        <button
                          onClick={() => setConfirmingId(null)}
                          className="rounded-md px-2 py-1 text-xs text-muted hover:bg-ink-800 hover:text-text"
                        >
                          Cancel
                        </button>
                      </div>
                    ) : (
                      <button
                        onClick={() => setConfirmingId(e.id)}
                        disabled={isCurrent}
                        title={isCurrent ? 'This is the current version' : 'Restore this version'}
                        className="shrink-0 rounded-md border border-line px-2.5 py-1 text-xs text-muted opacity-0 transition-opacity hover:bg-ink-800 hover:text-text group-hover:opacity-100 disabled:cursor-not-allowed disabled:opacity-0"
                      >
                        Restore
                      </button>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
        </div>

        {/* Destructive: wipe the whole history. */}
        <div className="border-t border-line px-5 py-3">
          {clearing ? (
            <div className="flex items-center justify-between gap-2">
              <span className="text-[11px] text-gold">
                Permanently delete ALL {entries.length} version
                {entries.length === 1 ? '' : 's'}? This can't be undone.
              </span>
              <div className="flex shrink-0 items-center gap-1.5">
                <button
                  onClick={() => clear.mutate()}
                  disabled={clear.isPending}
                  className="rounded-md bg-gold px-2.5 py-1 text-xs font-medium text-ink-950 hover:brightness-110 disabled:opacity-50"
                >
                  {clear.isPending ? 'Deleting…' : 'Yes, delete all'}
                </button>
                <button
                  onClick={() => setClearing(false)}
                  className="rounded-md px-2 py-1 text-xs text-muted hover:bg-ink-800 hover:text-text"
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <div className="flex items-center justify-between">
              <button
                onClick={() => setClearing(true)}
                disabled={entries.length === 0}
                className="rounded-md border border-gold/50 px-2.5 py-1 text-xs text-gold hover:bg-gold/10 disabled:opacity-40"
              >
                Delete all history…
              </button>
              <button
                onClick={onClose}
                className="rounded-md px-3 py-1.5 text-sm text-muted hover:bg-ink-800 hover:text-text"
              >
                Close
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
