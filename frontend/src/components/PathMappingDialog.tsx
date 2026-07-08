import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type RemapPreview } from '../api'
import type { ToastMsg } from './Toast'

interface Props {
  onClose: () => void
  onNotify: (kind: ToastMsg['kind'], text: string) => void
  onError: (msg: string) => void
}

const inputCls =
  'w-full rounded-md border border-line bg-ink-850 px-2.5 py-1.5 text-sm text-text outline-none focus:border-accent placeholder:text-faint'

/**
 * Per-collection OS-path remapping editor. Lets the user translate the stored
 * audio-file path prefix to a locally-reachable one (portable USB / relocated
 * library). Saving the mapping is non-destructive — the .nml is never touched;
 * only how Konduktor resolves files for playback/analysis/tags changes.
 */
export function PathMappingDialog({ onClose, onNotify, onError }: Props) {
  const qc = useQueryClient()
  const saved = useQuery({ queryKey: ['pathMapping'], queryFn: api.getPathMapping })
  const suggest = useQuery({ queryKey: ['prefixSuggest'], queryFn: api.suggestPrefix })

  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const hydrated = useRef(false)
  const [preview, setPreview] = useState<RemapPreview | null>(null)
  const [saving, setSaving] = useState(false)
  const [browsing, setBrowsing] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [committing, setCommitting] = useState(false)

  // Hydrate once: prefer the saved mapping; otherwise prefill `from` with the
  // auto-detected prefix. Defer until the suggestion resolves when we'd need it.
  useEffect(() => {
    if (hydrated.current || !saved.data) return
    if (!saved.data.from && suggest.isLoading) return
    hydrated.current = true
    setTo(saved.data.to)
    setFrom(saved.data.from || suggest.data?.primary || '')
  }, [saved.data, suggest.isLoading, suggest.data])

  // Live validation preview (debounced) — how many tracks match / exist.
  useEffect(() => {
    if (!from.trim() || !to.trim()) {
      setPreview(null)
      return
    }
    const t = window.setTimeout(() => {
      api
        .previewRemap(from.trim(), to.trim())
        .then(setPreview)
        .catch(() => setPreview(null))
    }, 350)
    return () => window.clearTimeout(t)
  }, [from, to])

  const saveMapping = async () => {
    setSaving(true)
    try {
      await api.putPathMapping({ from: from.trim(), to: to.trim() })
      // Re-resolve everything that depends on the file path.
      qc.invalidateQueries({ queryKey: ['pathMapping'] })
      qc.invalidateQueries({ queryKey: ['tracks'] })
      const cleared = !from.trim() || !to.trim()
      onNotify('success', cleared ? 'Path mapping cleared.' : 'Path mapping saved.')
      onClose()
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Failed to save mapping')
    } finally {
      setSaving(false)
    }
  }

  const commit = async () => {
    setCommitting(true)
    try {
      const res = await api.remapPaths(from.trim(), to.trim())
      // Locations changed, so track ids changed — refetch library + state.
      qc.invalidateQueries({ queryKey: ['tracks'] })
      qc.invalidateQueries({ queryKey: ['state'] })
      if (res.rewritten === 0) {
        onNotify('warning', 'No tracks matched — nothing was rewritten.')
      } else {
        onNotify(
          'success',
          `Rewrote ${res.rewritten} track path${res.rewritten === 1 ? '' : 's'}.` +
            (res.backup ? ` Backup: ${res.backup.split('/').pop()}` : ''),
        )
        onClose()
      }
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Failed to rewrite paths')
    } finally {
      setCommitting(false)
      setConfirming(false)
    }
  }

  const canCommit = !!from.trim() && !!to.trim()

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
          <div className="text-[15px] font-semibold tracking-tight">Path Remapping</div>
          <div className="text-xs text-muted">
            Translate the collection's stored file paths to where they live on this machine.
          </div>
        </div>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-4">
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted">Stored prefix (from)</label>
            <input
              value={from}
              onChange={(e) => setFrom(e.target.value)}
              placeholder="/Volumes/OLD_USB/Music"
              className={inputCls}
            />
            <div className="text-[11px] text-faint">
              The path prefix as saved in the collection (may not exist on this machine).
            </div>
            {suggest.data && suggest.data.groups.length > 0 && (
              <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
                <span className="text-[11px] text-faint">Detected:</span>
                {suggest.data.groups
                  .filter((g) => g.prefix && g.prefix !== from)
                  .map((g) => (
                    <button
                      key={g.prefix}
                      onClick={() => setFrom(g.prefix)}
                      title={`${g.count} tracks`}
                      className="max-w-full truncate rounded border border-line px-1.5 py-0.5 text-[11px] text-accent hover:bg-ink-800"
                    >
                      {g.prefix} · {g.count}
                    </button>
                  ))}
              </div>
            )}
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted">Local prefix (to)</label>
            <div className="flex gap-2">
              <input
                value={to}
                onChange={(e) => setTo(e.target.value)}
                placeholder="/Users/you/Music"
                className={inputCls}
              />
              <button
                onClick={() => setBrowsing((b) => !b)}
                className="shrink-0 rounded-md border border-line px-3 py-1.5 text-sm text-muted hover:bg-ink-800 hover:text-text"
              >
                {browsing ? 'Close' : 'Browse…'}
              </button>
            </div>
            {browsing && (
              <FolderBrowser
                initial={to.trim() || undefined}
                onPick={(path) => {
                  setTo(path)
                  setBrowsing(false)
                }}
              />
            )}
          </div>

          {/* Validation preview */}
          {preview && (
            <div className="rounded-md border border-line bg-ink-850 px-3 py-2 text-xs">
              {preview.matched === 0 ? (
                <span className="text-gold">
                  No tracks match “{from.trim()}”. Check the stored prefix.
                </span>
              ) : (
                <span className={preview.existing === preview.matched ? 'text-mint' : 'text-gold'}>
                  {preview.matched} of {preview.total} tracks match; {preview.existing} of{' '}
                  {preview.matched} exist at the target.
                </span>
              )}
            </div>
          )}

          {/* Write-back: permanent, destructive move. Clearly separated. */}
          <div className="mt-2 space-y-2 rounded-md border border-gold/40 bg-gold/5 px-3 py-3">
            <div className="text-xs font-semibold text-gold">Make permanent</div>
            <p className="text-[11px] leading-relaxed text-muted">
              Rewrites the matching file paths directly in the collection (a backup is
              written first). Use this only when you've moved the library for good — it
              bakes in <span className="text-text">this machine's</span> paths and will
              break the collection on other machines/OSes.
            </p>
            {confirming ? (
              <div className="flex items-center gap-2">
                <span className="text-[11px] text-gold">Rewrite {preview?.matched ?? 0} paths?</span>
                <button
                  onClick={commit}
                  disabled={committing || !canCommit}
                  className="rounded-md bg-gold px-2.5 py-1 text-xs font-medium text-ink-950 hover:brightness-110 disabled:opacity-50"
                >
                  {committing ? 'Rewriting…' : 'Yes, rewrite'}
                </button>
                <button
                  onClick={() => setConfirming(false)}
                  className="rounded-md px-2.5 py-1 text-xs text-muted hover:bg-ink-800 hover:text-text"
                >
                  Cancel
                </button>
              </div>
            ) : (
              <button
                onClick={() => setConfirming(true)}
                disabled={!canCommit}
                className="rounded-md border border-gold/50 px-2.5 py-1 text-xs text-gold hover:bg-gold/10 disabled:opacity-40"
              >
                Rewrite collection paths…
              </button>
            )}
          </div>
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-line px-5 py-3">
          <button
            onClick={onClose}
            className="rounded-md px-3 py-1.5 text-sm text-muted hover:bg-ink-800 hover:text-text"
          >
            Cancel
          </button>
          <button
            onClick={saveMapping}
            disabled={saving}
            className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-ink-950 hover:brightness-110 disabled:opacity-50"
          >
            {saving ? 'Saving…' : 'Save mapping'}
          </button>
        </div>
      </div>
    </div>
  )
}

/** A compact directory-only browser for choosing the local prefix. */
function FolderBrowser({
  initial,
  onPick,
}: {
  initial?: string
  onPick: (path: string) => void
}) {
  const [dir, setDir] = useState<string | undefined>(initial)
  const listing = useQuery({ queryKey: ['fs', dir ?? '~'], queryFn: () => api.listDir(dir) })
  const data = listing.data

  return (
    <div className="mt-2 overflow-hidden rounded-md border border-line bg-ink-850">
      <div className="flex items-center gap-1 border-b border-line px-2 py-1.5">
        <button
          onClick={() => setDir(data?.home)}
          className="rounded px-2 py-0.5 text-xs text-muted hover:bg-ink-800 hover:text-text"
        >
          Home
        </button>
        <button
          onClick={() => data?.parent && setDir(data.parent)}
          disabled={!data?.parent}
          className="rounded px-2 py-0.5 text-xs text-muted hover:bg-ink-800 hover:text-text disabled:opacity-40"
        >
          Up
        </button>
        <span className="ml-1 flex-1 truncate text-right text-[11px] text-faint" dir="rtl">
          {data?.path ?? '…'}
        </span>
      </div>
      <div className="max-h-48 overflow-y-auto">
        {data?.dirs.length ? (
          data.dirs.map((d) => (
            <button
              key={d.path}
              onClick={() => setDir(d.path)}
              className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm text-text hover:bg-ink-800"
            >
              <span className="text-faint">📁</span>
              <span className="truncate">{d.name}</span>
            </button>
          ))
        ) : (
          <div className="px-3 py-2 text-xs text-faint">No subfolders.</div>
        )}
      </div>
      <div className="border-t border-line px-2 py-1.5">
        <button
          onClick={() => data?.path && onPick(data.path)}
          disabled={!data?.path}
          className="w-full rounded-md bg-ink-800 px-3 py-1.5 text-sm text-text hover:bg-ink-700 disabled:opacity-50"
        >
          Use this folder
        </button>
      </div>
    </div>
  )
}
