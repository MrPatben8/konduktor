import { createPortal } from 'react-dom'
import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'
import { size } from './ConvertStemsDialog'

/**
 * Settings → Remote audio: this computer's copies of a remote library's audio.
 *
 * Everything that needs a track as a FILE — the deck, analysis, stem
 * separation, an export's copy onto a stick — runs here on a downloaded copy,
 * kept in one cache so a track is fetched once. Least recently used goes first
 * once the cap is reached; clearing it only costs a download the next time.
 */

const CAPS_GB = [2, 5, 10, 20, 50, 100]

export function RemoteCacheSettings({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  const usage = useQuery({ queryKey: ['remote-cache'], queryFn: api.remoteCache })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const u = usage.data
  const capGb = u ? Math.round(u.cap / 1024 ** 3) : null

  const setCap = async (gb: number) => {
    setError(null)
    try {
      const p = await api.patchPrefs({ remoteCacheBytes: gb * 1024 ** 3 })
      qc.setQueryData(['prefs'], p)
      qc.invalidateQueries({ queryKey: ['remote-cache'] })
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const clear = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.clearRemoteCache()
      qc.invalidateQueries({ queryKey: ['remote-cache'] })
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return createPortal(
    <div
      aria-modal="true"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-6 backdrop-blur-[3px]"
      onClick={onClose}
    >
      <div className="flex w-full max-w-md flex-col overflow-hidden glass-overlay" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-line px-5 py-3">
          <h2 className="text-sm font-semibold text-text">Remote audio</h2>
          <button onClick={onClose} className="rounded px-1.5 text-faint hover:text-text" aria-label="Close">
            ×
          </button>
        </div>
        <div className="space-y-4 px-5 py-4 text-sm">
          <p className="text-muted">
            Tracks from a remote library are downloaded to this computer to be played, analysed,
            converted or exported, and kept so each is fetched only once.
          </p>
          <div className="text-text">
            {u ? `${size(u.bytes)} in ${u.files} file${u.files === 1 ? '' : 's'}` : 'Loading…'}
            {u && <span className="text-faint"> · {size(u.free)} free on this disk</span>}
          </div>
          <label className="flex items-center gap-3">
            <span className="text-muted">Keep up to</span>
            <select
              value={capGb ?? ''}
              onChange={(e) => void setCap(Number(e.target.value))}
              className="rounded-lg well px-2 py-1 text-sm text-text outline-none"
            >
              {capGb !== null && !CAPS_GB.includes(capGb) && <option value={capGb}>{capGb} GB</option>}
              {CAPS_GB.map((gb) => (
                <option key={gb} value={gb}>
                  {gb} GB
                </option>
              ))}
            </select>
            <span className="text-faint">— the least recently used go first</span>
          </label>
          {error && <div className="text-xs text-pink">{error}</div>}
        </div>
        <div className="flex justify-end gap-2 border-t border-line px-5 py-3">
          <button
            disabled={busy || !u || u.files === 0}
            onClick={() => void clear()}
            className="btn-glass rounded-full px-4 py-1.5 text-sm text-muted hover:text-text disabled:opacity-40"
          >
            {busy ? 'Clearing…' : 'Clear'}
          </button>
          <button onClick={onClose} className="rounded-full btn-primary px-4 py-1.5 text-sm font-semibold">
            Done
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
