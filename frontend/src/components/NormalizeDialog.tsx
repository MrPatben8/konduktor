import { createPortal } from 'react-dom'
import { useEffect, useMemo, useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { api, type NormalizeOptions, type NormalizePreview, type NormalizeRow } from '../api'
import { LevelInput, useAppSetting } from './AppSettings'
import { Icon } from '../lib/icons'

interface Props {
  ids: string[]
  onRun: (opts: NormalizeOptions) => Promise<void>
  onClose: () => void
  onError: (msg: string) => void
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`
const db = (v: number) => `${v > 0 ? '+' : ''}${v.toFixed(1)} dB`

/** What a row's outcome means, in the dialog's words. */
function rowNote(r: NormalizeRow): string {
  if (r.skip) return r.skip
  switch (r.outcome) {
    case 'apply': return db(r.gain_db ?? 0)
    case 'capped':
      return r.gain_db ? `${db(r.gain_db)} — ${r.short_by?.toFixed(1)} dB short (peak ceiling)` : 'no headroom under the ceiling'
    case 'at_target': return 'already at the target'
    case 'hot_peak_left': return 'peak already over the ceiling — left alone'
    case 'unsupported': return 'this format cannot be changed losslessly'
    case 'unmeasured': return 'measured first, then levelled'
    default: return ''
  }
}

/**
 * The track menu's Mastering ▸ Normalize… (decided 2026-10-19): change the
 * selected files' audio LEVEL in place, toward a target in the chosen measure,
 * a raise stopping at the true-peak ceiling. Every number comes from the
 * backend's own plan (`/api/tracks/normalize/preview`). The defaults are the
 * app settings; changes here apply to this run only. The originals are parked
 * until Save — and Save makes a lossless file's change permanent, which the
 * dialog says before it runs.
 */
export function NormalizeDialog({ ids, onRun, onClose, onError }: Props) {
  const defaultMeasure = useAppSetting('levelMeasure')
  const defaultTarget = useAppSetting('levelTarget')
  const defaultCeiling = useAppSetting('levelCeiling')
  const [opts, setOpts] = useState<NormalizeOptions | null>(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    if (!opts) setOpts({ measure: defaultMeasure, target: defaultTarget, ceiling: defaultCeiling })
  }, [opts, defaultMeasure, defaultTarget, defaultCeiling])

  const preview = useQuery({
    queryKey: ['normalize-preview', ids, opts],
    queryFn: () => api.normalizePreview(ids, opts!),
    enabled: !!opts,
    placeholderData: keepPreviousData,
  })
  const p: NormalizePreview | undefined = preview.data
  const count = p?.tracks_to_process ?? 0

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && !busy && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [busy, onClose])

  const summary = useMemo(() => {
    if (!p) return []
    const rows = p.rows
    const up = rows.filter((r) => (r.gain_db ?? 0) > 0).length
    const down = rows.filter((r) => (r.gain_db ?? 0) < 0).length
    const c = p.counts
    const lines: string[] = []
    if (up || down)
      lines.push(`${plural(up + down, 'track')} change: ${[up && `${up} raised`, down && `${down} lowered`].filter(Boolean).join(', ')}`)
    if (p.to_measure) lines.push(`${plural(p.to_measure, 'track')} measured first`)
    if (c.capped) lines.push(`${plural(c.capped, 'track')} held short by the peak ceiling`)
    if (c.at_target) lines.push(`${c.at_target} already at the target`)
    if (c.hot_peak_left) lines.push(`${plural(c.hot_peak_left, 'hot peak')} left alone`)
    if (c.unsupported) lines.push(`${plural(c.unsupported, 'file')} in a format that cannot be changed losslessly`)
    if (c.skipped) lines.push(`${c.skipped} skipped`)
    const t = p.transfer
    if (t && (t.download || t.upload)) {
      const mb = (b: number) => (b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round(b / 1e6))} MB`)
      const secs = (t.download + t.upload) / Math.max(t.speed, 1)
      lines.push(
        `On the server: ${t.download ? `${mb(t.download)} downloaded, ` : ''}${mb(t.upload)} uploaded` +
          ` — about ${secs < 90 ? `${Math.max(1, Math.round(secs))} s` : `${Math.round(secs / 60)} min`}` +
          (t.measured ? '' : ' (estimated)'),
      )
    }
    return lines
  }, [p])

  const run = async () => {
    if (!opts || busy || count === 0) return
    setBusy(true)
    try {
      await onRun(opts)
    } catch (e) {
      onError((e as Error).message)
      setBusy(false)
    }
  }

  const measureUnit = opts?.measure === 'rms' ? 'dB' : 'LUFS'

  return createPortal(
    <div
      aria-modal="true"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-[3px] p-6"
      onClick={() => !busy && onClose()}
    >
      <div className="flex max-h-[80vh] w-full max-w-lg flex-col overflow-hidden glass-overlay" onClick={(e) => e.stopPropagation()}>
        <div className="border-b border-line px-5 py-4">
          <div className="text-[15px] font-semibold tracking-tight">Normalize</div>
          <div className="text-xs text-muted">
            {plural(ids.length, 'track')} selected · changes the audio files' level
          </div>
        </div>

        {opts && (
          <div className="space-y-2 border-b border-line px-5 py-3 text-sm">
            <label className="flex items-center justify-between gap-4">
              <span className="text-text">Level by</span>
              <select
                className="rounded-lg well px-2 py-1 text-sm text-text outline-none"
                value={opts.measure}
                onChange={(e) => setOpts({ ...opts, measure: e.target.value as 'lufs' | 'rms' })}
              >
                <option value="lufs">LUFS (integrated loudness)</option>
                <option value="rms">RMS (loudest sections)</option>
              </select>
            </label>
            <label className="flex items-center justify-between gap-4">
              <span className="text-text">Target</span>
              <LevelInput value={opts.target} unit={measureUnit} min={-30} max={0}
                onCommit={(v) => setOpts({ ...opts, target: v })} />
            </label>
            <label className="flex items-center justify-between gap-4">
              <span className="text-text" title="A raise stops here, so nothing clips">Peak ceiling</span>
              <LevelInput value={opts.ceiling} unit="dBTP" min={-12} max={0}
                onCommit={(v) => setOpts({ ...opts, ceiling: v })} />
            </label>
          </div>
        )}

        <div className={`min-h-0 flex-1 overflow-y-auto px-5 py-3 text-sm ${preview.isFetching ? 'opacity-70' : ''}`}>
          <div className="space-y-1 text-muted">
            {summary.map((line) => (
              <div key={line}>{line}</div>
            ))}
            {p && count === 0 && <div className="text-faint">Nothing to change at this target.</div>}
          </div>
          {p && p.rows.length > 1 && (
            <div className="mt-3 space-y-0.5">
              {p.rows.map((r) => (
                <div key={r.track_id} className="flex items-baseline justify-between gap-3 text-xs">
                  <span className="min-w-0 truncate text-text" title={r.title}>
                    {r.title}
                  </span>
                  <span className={`shrink-0 tabular-nums ${r.gain_db ? 'text-muted' : 'text-faint'}`}>
                    {r.level != null && `${r.level.toFixed(1)} ${measureUnit} · `}
                    {rowNote(r)}
                  </span>
                </div>
              ))}
            </div>
          )}
          {p && p.rows.length === 1 && <div className="mt-1 text-xs text-faint">{rowNote(p.rows[0])}</div>}
        </div>

        {p && p.lossless > 0 && (
          <div className="mx-5 mb-2 flex gap-1.5 rounded-xl bg-gold/10 px-2.5 py-1.5 text-[11px] leading-snug text-gold">
            <Icon name="warning" size={13} className="mt-px shrink-0" />
            <span>
              Saving makes the change to {plural(p.lossless, 'lossless file')} permanent: the original
              {p.lossless === 1 ? ' is' : 's are'} deleted. MP3s keep an undo tag. Until then, Discard puts
              everything back.
            </span>
          </div>
        )}

        <div className="flex items-center justify-end gap-2 border-t border-line px-5 py-3">
          <button
            onClick={onClose}
            disabled={busy}
            className="rounded-md px-3 py-1.5 text-sm text-muted hover:bg-ink-800 hover:text-text"
          >
            Cancel
          </button>
          <button
            onClick={run}
            disabled={busy || count === 0 || !p}
            className="rounded-full btn-primary px-4 py-1.5 text-sm font-semibold disabled:opacity-40"
          >
            {`Normalize${count ? ` ${count}` : ''}`}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
