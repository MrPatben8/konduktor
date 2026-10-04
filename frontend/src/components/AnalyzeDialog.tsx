import { createPortal } from 'react-dom'
import { useEffect, useState, type ReactNode } from 'react'
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type AnalyzeOptions, type AnalyzePreview } from '../api'

interface Props {
  /** The selected tracks. */
  ids: string[]
  /** The library can write beatgrids (BPM and Grid). */
  gridEditable: boolean
  /** The library can store a key. */
  keyWritable: boolean
  onRun: (opts: AnalyzeOptions) => Promise<void>
  onClose: () => void
  onError: (msg: string) => void
}

type What = Pick<AnalyzeOptions, 'bpm' | 'grid' | 'key'>
const PREF_KEY = 'analyzeWhat'
const DEFAULT_WHAT: What = { bpm: true, grid: true, key: true }

/** The remembered ticks; anything malformed falls back to the default. */
function loadWhat(prefs: Record<string, unknown> | undefined): What {
  const saved = prefs?.[PREF_KEY] as Partial<What> | undefined
  if (!saved || typeof saved !== 'object') return DEFAULT_WHAT
  const pick = (k: keyof What) => (typeof saved[k] === 'boolean' ? (saved[k] as boolean) : DEFAULT_WHAT[k])
  return { bpm: pick('bpm'), grid: pick('grid'), key: pick('key') }
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`

/**
 * The context menu's "Analyze…" (decided 2026-10-04): which of BPM / Grid /
 * Key to analyse, with the "already has one — replace?" questions folded in so
 * there is never a second popup. BPM and Grid are independent halves of a
 * beatgrid — BPM alone keeps the downbeat, Grid alone keeps the tempo — and
 * the grid Replace tick only guards a FULL re-analysis (both ticked). The
 * ticks are remembered; the Replace ticks never are — overwriting is a
 * decision about THESE tracks.
 */
export function AnalyzeDialog({ ids, gridEditable, keyWritable, onRun, onClose, onError }: Props) {
  const qc = useQueryClient()
  const prefs = useQuery({ queryKey: ['prefs'], queryFn: api.getPrefs })
  const [what, setWhat] = useState<What | null>(null)
  const [replaceGrid, setReplaceGrid] = useState(false)
  const [replaceKey, setReplaceKey] = useState(false)
  const [busy, setBusy] = useState(false)

  // Hydrate once from prefs, masked by what this library can store.
  useEffect(() => {
    if (what || prefs.isPending) return
    const w = loadWhat(prefs.data as Record<string, unknown> | undefined)
    setWhat({ bpm: w.bpm && gridEditable, grid: w.grid && gridEditable, key: w.key && keyWritable })
  }, [what, prefs.isPending, prefs.data, gridEditable, keyWritable])

  const opts: AnalyzeOptions | null = what && {
    ...what,
    replace_grid: what.bpm && what.grid && replaceGrid,
    replace_key: what.key && replaceKey,
  }
  const preview = useQuery({
    queryKey: ['analyze-preview', ids, opts],
    queryFn: () => api.analyzePreview(ids, opts!),
    enabled: !!opts && (opts.bpm || opts.grid || opts.key),
    placeholderData: keepPreviousData,
  })
  const p: AnalyzePreview | undefined = preview.data
  const nothingTicked = !!what && !(what.bpm || what.grid || what.key)
  const count = nothingTicked ? 0 : (p?.tracks_to_analyze ?? 0)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && !busy && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [busy, onClose])

  const run = async () => {
    if (!opts || busy || count === 0) return
    setBusy(true)
    api
      .patchPrefs({ [PREF_KEY]: { bpm: opts.bpm, grid: opts.grid, key: opts.key } })
      .then((x) => qc.setQueryData(['prefs'], x))
      .catch(() => {})
    try {
      await onRun(opts)
    } catch (e) {
      onError((e as Error).message)
      setBusy(false)
    }
  }

  const toggle = (k: keyof What, label: string, help: string, enabled: boolean, why: string) => (
    <label
      className={`flex items-start gap-3 rounded-md px-2 py-1.5 ${enabled ? 'cursor-pointer hover:bg-ink-800' : 'opacity-40'}`}
      title={enabled ? undefined : why}
    >
      <input
        type="checkbox"
        className="mt-0.5"
        checked={!!what?.[k]}
        disabled={!enabled || !what}
        onChange={(e) => setWhat((w) => w && { ...w, [k]: e.target.checked })}
      />
      <span className="min-w-0">
        <span className="block text-sm text-text">{label}</span>
        <span className="block text-xs text-faint">{help}</span>
      </span>
    </label>
  )

  // What the grid ticks will do, in the dialog's own words.
  const gridLines: ReactNode[] = []
  if (p && what && (what.bpm || what.grid)) {
    if (what.bpm && what.grid) {
      if (p.with_grid > 0)
        gridLines.push(
          <div key="rg" className="flex items-center justify-between gap-3">
            <span>
              <span className="tabular-nums text-text">{p.with_grid}</span>{' '}
              {p.with_grid === 1 ? 'already has a grid' : 'already have a grid'}
            </span>
            <label className="flex shrink-0 items-center gap-1.5 text-xs" title="Replacing discards any hand edits to those grids">
              <input type="checkbox" checked={replaceGrid} onChange={(e) => setReplaceGrid(e.target.checked)} />
              Replace {p.with_grid === 1 ? 'it' : 'them'}
            </label>
          </div>,
        )
    } else if (what.bpm) {
      if (p.grid_bpm) gridLines.push(<div key="b">{plural(p.grid_bpm, 'grid')}: tempo re-detected, downbeat kept</div>)
      if (p.grid_full) gridLines.push(<div key="bf">{plural(p.grid_full, 'track')} without a grid: analyzed in full</div>)
    } else {
      if (p.grid_phase) gridLines.push(<div key="g">{plural(p.grid_phase, 'track')}: downbeats found at the current BPM</div>)
      if (p.grid_full) gridLines.push(<div key="gf">{plural(p.grid_full, 'track')} without a BPM: analyzed in full</div>)
    }
    if (p.grid_flexible)
      gridLines.push(
        <div key="fx" className="text-faint" title="One BPM or downbeat for the whole track would flatten its tempo changes">
          {plural(p.grid_flexible, 'multi-tempo grid')} — skipped
        </div>,
      )
    if (p.grid_locked) gridLines.push(<div key="lk" className="text-faint">{plural(p.grid_locked, 'locked grid')} — skipped</div>)
  }

  return createPortal(
    <div
      aria-modal="true"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-[3px] p-6"
      onClick={() => !busy && onClose()}
    >
      <div className="w-full max-w-sm overflow-hidden glass-overlay" onClick={(e) => e.stopPropagation()}>
        <div className="border-b border-line px-5 py-4">
          <div className="text-[15px] font-semibold tracking-tight">Analyze</div>
          <div className="text-xs text-muted">{plural(ids.length, 'track')} selected</div>
        </div>

        <div className="space-y-0.5 px-3 pt-3">
          {toggle('bpm', 'BPM', 'Detect the tempo', gridEditable, "This library's beatgrids cannot be edited")}
          {toggle('grid', 'Grid', 'Find where the downbeats fall', gridEditable, "This library's beatgrids cannot be edited")}
          {toggle('key', 'Key', 'Detect the musical key', keyWritable, "This library cannot store a key")}
        </div>

        <div className={`space-y-1.5 px-5 py-3 text-sm text-muted ${preview.isFetching ? 'opacity-70' : ''}`}>
          {gridLines}
          {p && what?.key && p.with_key > 0 && (
            <div className="flex items-center justify-between gap-3">
              <span>
                <span className="tabular-nums text-text">{p.with_key}</span>{' '}
                {p.with_key === 1 ? 'already has a key' : 'already have a key'}
              </span>
              <label className="flex shrink-0 items-center gap-1.5 text-xs">
                <input type="checkbox" checked={replaceKey} onChange={(e) => setReplaceKey(e.target.checked)} />
                Replace {p.with_key === 1 ? 'it' : 'them'}
              </label>
            </div>
          )}
          {nothingTicked && <div className="text-faint">Tick what to analyze.</div>}
        </div>

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
            {count === 0 && p && !nothingTicked ? 'Nothing to analyze' : `Analyze${count ? ` ${count}` : ''}`}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
