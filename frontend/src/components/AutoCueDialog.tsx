import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  api,
  type AutoCueEvent,
  type AutoHotcuesResult,
  type CuePoint,
  type Track,
} from '../api'
import { formatDuration } from '../lib/format'

/**
 * Auto Hotcues: bind each hotcue slot to a structural event plus an offset in
 * beats, then analyse the track and place them all in one go.
 *
 * The template (event + offset per slot) is remembered in userprefs — a DJ cues
 * every track the same way — but the per-slot "replace" ticks are NOT: whether
 * to overwrite a hand-placed cue is a decision about THIS track, and a
 * remembered tick would silently clobber the next track's cues.
 */

export const AUTO_CUE_EVENTS: { id: AutoCueEvent; label: string }[] = [
  { id: 'first_beat', label: 'First beat' },
  { id: 'intro_end', label: 'Intro end' },
  { id: 'build_1', label: 'Build 1' },
  { id: 'drop_1', label: 'Drop 1' },
  { id: 'breakdown_1', label: 'Breakdown 1' },
  { id: 'build_2', label: 'Build 2' },
  { id: 'drop_2', label: 'Drop 2' },
  { id: 'breakdown_2', label: 'Breakdown 2' },
  { id: 'build_3', label: 'Build 3' },
  { id: 'drop_3', label: 'Drop 3' },
  { id: 'breakdown_3', label: 'Breakdown 3' },
  { id: 'outro', label: 'Outro start' },
  { id: 'last_beat', label: 'Last beat' },
]
const EVENT_IDS = new Set<string>(AUTO_CUE_EVENTS.map((e) => e.id))
export const eventLabel = (id: AutoCueEvent) =>
  AUTO_CUE_EVENTS.find((e) => e.id === id)?.label ?? id

interface Row {
  event: AutoCueEvent | null
  offset: number // beats
}

const DEFAULT_TEMPLATE: AutoCueEvent[] = [
  'first_beat', 'build_1', 'drop_1', 'breakdown_1', 'build_2', 'drop_2', 'outro', 'last_beat',
]
const PREF_KEY = 'autoCueTemplate'
const MAX_OFFSET = 256

/** The remembered template, padded/truncated to this library's bank size.
 *  Anything malformed in prefs falls back to the default for that slot. */
function loadTemplate(prefs: Record<string, unknown> | undefined, slots: number): Row[] {
  const saved = Array.isArray(prefs?.[PREF_KEY]) ? (prefs![PREF_KEY] as unknown[]) : null
  return Array.from({ length: slots }, (_, i) => {
    const r = saved?.[i] as { event?: unknown; offset_beats?: unknown } | undefined
    if (saved && r && typeof r === 'object') {
      const event = r.event === null ? null : EVENT_IDS.has(String(r.event)) ? (r.event as AutoCueEvent) : null
      const off = typeof r.offset_beats === 'number' && isFinite(r.offset_beats) ? Math.round(r.offset_beats) : 0
      return { event, offset: Math.max(-MAX_OFFSET, Math.min(MAX_OFFSET, off)) }
    }
    return { event: DEFAULT_TEMPLATE[i] ?? null, offset: 0 }
  })
}

interface Props {
  track: Track
  slotCount: number
  slotLabel: (slot: number) => string
  /** The track's current hotcues by slot, to mark occupied rows. */
  existing: Map<number, CuePoint>
  onClose: () => void
  onDone: (result: AutoHotcuesResult) => void
  onError: (msg: string) => void
}

export function AutoCueDialog({ track, slotCount, slotLabel, existing, onClose, onDone, onError }: Props) {
  const qc = useQueryClient()
  const prefs = useQuery({ queryKey: ['prefs'], queryFn: api.getPrefs })
  const [rows, setRows] = useState<Row[] | null>(null)
  const [replace, setReplace] = useState<Set<number>>(new Set())
  const [busy, setBusy] = useState(false)

  // Hydrate once from prefs (the query is normally already cached by PrepStrip).
  useEffect(() => {
    if (rows === null && (prefs.data || prefs.isError)) setRows(loadTemplate(prefs.data, slotCount))
  }, [prefs.data, prefs.isError, rows, slotCount])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [busy, onClose])

  const setRow = (i: number, patch: Partial<Row>) =>
    setRows((rs) => rs && rs.map((r, j) => (j === i ? { ...r, ...patch } : r)))
  const nudge = (i: number, by: number) =>
    setRows((rs) =>
      rs && rs.map((r, j) => (j === i ? { ...r, offset: Math.max(-MAX_OFFSET, Math.min(MAX_OFFSET, r.offset + by)) } : r)),
    )

  const active = (rows ?? []).filter((r) => r.event !== null).length

  const confirm = async () => {
    if (!rows || busy || active === 0) return
    setBusy(true)
    const template = rows.map((r) => ({ event: r.event, offset_beats: r.offset }))
    // Remember the template whatever the analysis finds: it describes how the
    // user cues, not this track.
    api
      .patchPrefs({ [PREF_KEY]: template })
      .then((p) => qc.setQueryData(['prefs'], p))
      .catch(() => {})
    try {
      const result = await api.autoCues(
        track.id,
        rows.flatMap((r, slot) =>
          r.event ? [{ slot, event: r.event, offset_beats: r.offset, overwrite: replace.has(slot) }] : [],
        ),
      )
      onDone(result)
      onClose()
    } catch (e) {
      onError((e as Error).message)
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 p-6" onClick={() => !busy && onClose()}>
      <div
        className="flex max-h-[85vh] w-full max-w-xl flex-col overflow-hidden rounded-xl border border-line bg-ink-900 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="border-b border-line px-5 py-4">
          <div className="text-[15px] font-semibold tracking-tight">Auto Hotcues</div>
          <div className="truncate text-xs text-muted">
            {track.artist} — {track.title}
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-3">
          <div className="mb-2 grid grid-cols-[2rem_1fr_9rem_7.5rem] gap-2 text-[10px] font-semibold uppercase tracking-wider text-faint">
            <span>Slot</span>
            <span>Event</span>
            <span className="text-center">Offset (beats)</span>
            <span>Existing cue</span>
          </div>
          {rows === null ? (
            <div className="py-6 text-center text-sm text-muted">Loading…</div>
          ) : (
            rows.map((r, slot) => {
              const cue = existing.get(slot)
              return (
                <div key={slot} className="grid grid-cols-[2rem_1fr_9rem_7.5rem] items-center gap-2 py-1">
                  <span className="flex h-7 w-7 items-center justify-center rounded bg-ink-800 text-xs font-bold text-text">
                    {slotLabel(slot)}
                  </span>
                  <select
                    value={r.event ?? ''}
                    onChange={(e) => setRow(slot, { event: (e.target.value || null) as AutoCueEvent | null })}
                    className="h-7 rounded-md border border-line bg-ink-850 px-2 text-sm text-text"
                  >
                    <option value="">— Leave empty —</option>
                    {AUTO_CUE_EVENTS.map((ev) => (
                      <option key={ev.id} value={ev.id}>
                        {ev.label}
                      </option>
                    ))}
                  </select>
                  <div className={`flex items-center gap-1 ${r.event ? '' : 'pointer-events-none opacity-30'}`}>
                    <button
                      onClick={() => nudge(slot, -4)}
                      title="4 beats earlier"
                      className="h-7 w-7 rounded-md border border-line bg-ink-850 text-sm text-muted hover:text-text"
                    >
                      −
                    </button>
                    <input
                      type="number"
                      value={r.offset}
                      step={1}
                      onChange={(e) => {
                        const v = Math.round(Number(e.target.value))
                        if (isFinite(v)) setRow(slot, { offset: Math.max(-MAX_OFFSET, Math.min(MAX_OFFSET, v)) })
                      }}
                      className="h-7 w-12 rounded-md border border-line bg-ink-850 px-1 text-center text-sm tabular-nums text-text"
                    />
                    <button
                      onClick={() => nudge(slot, 4)}
                      title="4 beats later"
                      className="h-7 w-7 rounded-md border border-line bg-ink-850 text-sm text-muted hover:text-text"
                    >
                      +
                    </button>
                  </div>
                  {cue ? (
                    cue.editable ? (
                      <label className="flex min-w-0 items-center gap-1.5 text-xs text-muted" title={cue.name ?? ''}>
                        <input
                          type="checkbox"
                          checked={replace.has(slot)}
                          disabled={!r.event}
                          onChange={(e) =>
                            setReplace((s) => {
                              const n = new Set(s)
                              if (e.target.checked) n.add(slot)
                              else n.delete(slot)
                              return n
                            })
                          }
                        />
                        <span className="truncate">
                          Replace {cue.name || formatDuration(cue.start)}
                        </span>
                      </label>
                    ) : (
                      <span className="truncate text-xs text-faint" title="This cue belongs to the beatgrid and is never replaced">
                        Grid cue (kept)
                      </span>
                    )
                  ) : (
                    <span className="text-xs text-faint">Empty</span>
                  )}
                </div>
              )
            })
          )}
          <p className="mt-3 text-[11px] leading-relaxed text-faint">
            Events are found on the beatgrid, counting bar 1 from its first marker — if drops land a
            bar or two off, move the grid marker onto the first downbeat. Occupied slots are kept
            unless you tick Replace. If two slots land on the same beat, only the lower one gets
            the cue. Your slot setup is remembered for the next track.
          </p>
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-line px-5 py-3">
          <button
            onClick={onClose}
            disabled={busy}
            className="rounded-md px-3 py-1.5 text-sm text-muted hover:text-text disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            onClick={confirm}
            disabled={busy || rows === null || active === 0}
            className="rounded-md bg-accent px-4 py-1.5 text-sm font-semibold text-ink-950 hover:brightness-110 disabled:opacity-40"
          >
            {busy ? 'Analyzing…' : `Analyze & place ${active} cue${active === 1 ? '' : 's'}`}
          </button>
        </div>
      </div>
    </div>
  )
}
