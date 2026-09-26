import { useEffect, useRef, useState } from 'react'
import { askConfirm } from '../lib/confirm'
import { Icon } from '../lib/icons'
import { MIN_TAPS, RESET_MS, TapTempo } from '../lib/tapTempo'

// The deck's beatgrid controls, in three pieces that live in three places:
//
//   BpmReadout      — the header's BPM: shows the governing marker's tempo, and
//                     is where an exact BPM is typed (click it).
//   TempoControls   — the everyday tempo tools, always on the control row:
//                     nudge ±0.01 / ±0.25, ÷2 ×2, tap tempo, lock.
//   GridEditStrip   — only in Grid mode, in place of the hotcue pads: step
//                     between markers, nudge the marker ±1 / ±10 ms, add or
//                     delete a marker, delete the grid, reset it.
//
// A beatgrid is a list of tempo markers, and the one being edited is derived
// from the playhead rather than selected separately — so every control acts on
// the marker governing what you are hearing. Presentational: PrepStrip owns the
// state and sends the commands.

/** m:ss.mmm — marker positions need millisecond precision to be useful. */
function markerTime(t: number): string {
  const m = Math.floor(t / 60)
  const s = t - m * 60
  return `${m}:${s < 10 ? '0' : ''}${s.toFixed(3)}`
}

/** The header readout's BPM cell. Click to type an exact tempo — with no grid,
 *  typing one creates it (the caller decides where its first marker goes). */
export function BpmReadout({
  bpm,
  editable,
  onSetBpm,
  onClose,
  openRequest = 0,
}: {
  /** Tempo of the marker governing the playhead; null without a grid. */
  bpm: number | null
  editable: boolean
  onSetBpm: (bpm: number) => void
  /** The editor closed, committed or not. */
  onClose?: () => void
  /** Bump to open the editor from elsewhere (Set grid on a track with no BPM). */
  openRequest?: number
}) {
  const [editing, setEditing] = useState(false)
  const editingRef = useRef(false)
  const [val, setVal] = useState('')
  const open = () => {
    setVal(bpm != null ? bpm.toFixed(3) : '')
    editingRef.current = true
    setEditing(true)
  }
  // Only a CHANGE opens it, so remounting with a non-zero count does not.
  const seenRequestRef = useRef(openRequest)
  useEffect(() => {
    if (openRequest === seenRequestRef.current) return
    seenRequestRef.current = openRequest
    open()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openRequest])
  const close = (commit: boolean) => {
    if (!editingRef.current) return // Enter commits; a blur as it unmounts must not again
    editingRef.current = false
    const n = parseFloat(val)
    if (commit && isFinite(n) && n > 0) onSetBpm(Math.round(n * 1000) / 1000)
    setEditing(false)
    onClose?.()
  }
  const label = (
    <span className="text-[10px] tracking-[0.08em] text-faint">
      BPM{editable ? ' ✎' : ''}
    </span>
  )
  if (editing) {
    return (
      <div className="flex flex-col justify-center gap-px px-4">
        {label}
        <input
          autoFocus
          value={val}
          placeholder={bpm == null ? 'BPM?' : undefined}
          onChange={(e) => setVal(e.target.value)}
          onBlur={() => close(true)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') close(true)
            if (e.key === 'Escape') close(false)
            e.stopPropagation() // Space/arrows must not drive the deck while typing
          }}
          className="w-[5.5rem] rounded-md bg-ink-800 px-1 font-mono text-[17px] font-semibold text-accent outline-none ring-1 ring-accent placeholder:text-faint"
        />
      </div>
    )
  }
  return (
    <button
      onClick={() => editable && open()}
      title={
        !editable
          ? undefined
          : bpm != null
            ? 'Click to type an exact BPM'
            : 'Click to type a BPM — starts the beatgrid at the playhead'
      }
      className="flex flex-col justify-center gap-px rounded-r-xl px-4 text-left hover:bg-ink-850"
    >
      {label}
      <span className="font-mono text-[17px] font-semibold text-accent">
        {bpm != null ? bpm.toFixed(2) : '––'}
      </span>
    </button>
  )
}

const T_BTN =
  'flex h-8 items-center justify-center rounded-lg font-mono text-xs text-text transition-[background-color,transform] duration-75 ' +
  'hover:bg-ink-800 active:scale-[0.92] active:bg-white/[0.14] disabled:opacity-30 disabled:hover:bg-transparent disabled:active:scale-100'

/** Nudge / octave / tap / lock — always visible on the control row. */
export function TempoControls({
  bpm,
  locked,
  lockable,
  flexible,
  markerIndex,
  onTapStart,
  onTapBpm,
  onNudgeBpm,
  onHalve,
  onDouble,
  onToggleLock,
}: {
  bpm: number | null
  locked: boolean
  lockable: boolean
  flexible: boolean
  markerIndex: number
  /** A tap run began — the caller notes where, to anchor a grid there. */
  onTapStart: () => void
  /** A tapped tempo to commit; creates the grid when there is none. */
  onTapBpm: (bpm: number) => void
  onNudgeBpm: (delta: number) => void
  onHalve: () => void
  onDouble: () => void
  onToggleLock: () => void
}) {
  // One run of taps; its reading is shown on the button, so a press that is
  // not yet enough to commit still visibly counts.
  const tapperRef = useRef(new TapTempo())
  const committedRef = useRef<number | null>(null)
  const idleRef = useRef<ReturnType<typeof setTimeout>>(undefined)
  const [tapCount, setTapCount] = useState(0)
  const tap = () => {
    const { count, bpm: tapped } = tapperRef.current.tap(performance.now())
    if (count === 1) {
      committedRef.current = null
      onTapStart()
    }
    setTapCount(count)
    clearTimeout(idleRef.current)
    idleRef.current = setTimeout(() => setTapCount(0), RESET_MS)
    // Commit only when the snapped value moves: every set is an edit.
    if (tapped != null && tapped !== committedRef.current) {
      committedRef.current = tapped
      onTapBpm(tapped)
    }
  }
  useEffect(() => () => clearTimeout(idleRef.current), [])
  const off = bpm == null
  const of = flexible ? ` of marker ${markerIndex + 1}` : ''
  return (
    <div role="group" aria-label="Tempo" className="glass-group flex h-10 shrink-0 items-center gap-0.5 rounded-xl px-1">
      <button className={`${T_BTN} w-7`} onClick={() => onNudgeBpm(-0.25)} disabled={off} title={`BPM −0.25${of}`}>−−</button>
      <button className={`${T_BTN} w-[22px]`} onClick={() => onNudgeBpm(-0.01)} disabled={off} title={`BPM −0.01${of}`}>−</button>
      <button className={`${T_BTN} w-[22px]`} onClick={() => onNudgeBpm(0.01)} disabled={off} title={`BPM +0.01${of}`}>+</button>
      <button className={`${T_BTN} w-7`} onClick={() => onNudgeBpm(0.25)} disabled={off} title={`BPM +0.25${of}`}>++</button>
      <span aria-hidden className="mx-0.5 h-[18px] w-px bg-line" />
      <button className={`${T_BTN} w-7`} onClick={onHalve} disabled={off} title={`Halve the BPM${of}`}>÷2</button>
      <button className={`${T_BTN} w-7`} onClick={onDouble} disabled={off} title={`Double the BPM${of}`}>×2</button>
      <button
        className={`${T_BTN} h-7 min-w-[3.25rem] px-2 font-semibold tracking-[0.06em] ${
          tapCount ? 'bg-accent/20 text-accent' : 'bg-ink-800'
        }`}
        onClick={tap}
        title={`Tap tempo — tap along for ${MIN_TAPS}+ beats; snaps to whole BPM`}
      >
        {tapCount === 0 ? 'TAP' : tapCount < MIN_TAPS ? `${tapCount}/${MIN_TAPS}` : `TAP ${tapCount}`}
      </button>
      {lockable && (
        <button
          onClick={onToggleLock}
          aria-pressed={locked}
          title={locked ? 'Unlock the grid' : 'Lock the grid'}
          className={`${T_BTN} w-7 ${locked ? 'bg-gold/15 text-gold shadow-[inset_0_0_0_1px_rgb(255_200_97/0.45)]' : ''}`}
        >
          <Icon name={locked ? 'lock' : 'unlock'} size={14} />
        </button>
      )}
    </div>
  )
}

/** Grid mode: everything that fixes a grid, in place of the pads. */
export function GridEditStrip({
  markerIndex,
  markerCount,
  markerStart,
  atMarker,
  beforeFirst,
  canReset,
  onPrevMarker,
  onNextMarker,
  onNudgeMarker,
  onNudgeMarkerBeats,
  onAddMarker,
  onDeleteMarker,
  onDeleteGrid,
  onReset,
}: {
  markerIndex: number
  markerCount: number
  markerStart: number | null
  atMarker: boolean
  beforeFirst: boolean
  canReset: boolean
  onPrevMarker: () => void
  onNextMarker: () => void
  onNudgeMarker: (deltaMs: number) => void
  onNudgeMarkerBeats: (beats: number) => void
  onAddMarker: () => void
  onDeleteMarker: () => void
  onDeleteGrid: () => void
  onReset: () => void
}) {
  const hasGrid = markerCount > 0
  const flexible = markerCount > 1
  const nudge =
    'flex h-10 items-center px-2 font-mono text-[11px] text-text transition-[background-color,transform] duration-75 ' +
    'hover:bg-ink-800 active:scale-[0.92] active:bg-white/[0.14] disabled:opacity-30'
  const pill = 'flex h-[34px] shrink-0 items-center gap-1.5 rounded-full px-3 text-xs disabled:opacity-35'
  return (
    // A container, so the three actions on the right can drop their labels
    // rather than overflow when the window is narrow.
    <div role="group" aria-label="Grid editing" className="@container flex min-w-0 flex-1">
      <div className="flex min-w-0 flex-1 items-center gap-2">
        {/* The spare width goes on the LEFT, so the tools sit together at the
            right, next to the tempo controls, instead of splitting across the
            row with a gap in the middle. */}
        <div className="flex-1" />
        <div
          className="flex h-10 min-w-0 shrink items-center rounded-xl bg-gold/10 px-1 text-gold shadow-[inset_0_0_0_1px_rgb(255_200_97/0.35)]"
          title={
            !hasGrid
              ? 'No beatgrid yet — Set grid places one at the playhead'
              : beforeFirst
                ? "Playhead is before the first marker — marker 1's tempo extrapolates backwards"
                : `Editing marker ${markerIndex + 1} of ${markerCount} (Shift+←/→ to step)`
          }
        >
          <button onClick={onPrevMarker} disabled={!flexible} aria-label="Previous marker" className="flex h-10 w-6 items-center justify-center disabled:opacity-30">
            <Icon name="chevronLeft" size={13} strokeWidth={2.4} />
          </button>
          <span className="truncate px-1 font-mono text-xs font-semibold">
            {hasGrid
              ? `${beforeFirst ? '↤ ' : ''}Marker ${markerIndex + 1}/${markerCount}${
                  markerStart != null ? ` · ${markerTime(markerStart)}` : ''
                }`
              : 'No grid'}
          </span>
          <button onClick={onNextMarker} disabled={!flexible} aria-label="Next marker" className="flex h-10 w-6 items-center justify-center disabled:opacity-30">
            <Icon name="chevronRight" size={13} strokeWidth={2.4} />
          </button>
        </div>

        <div className="glass-group flex h-10 shrink-0 items-center overflow-hidden rounded-xl">
          {/* Phase nudges move the GOVERNING marker — not gated on atMarker,
              since the drift is heard at the playhead, not at the marker. */}
          <button className={nudge} onClick={() => onNudgeMarkerBeats(-1)} disabled={!hasGrid} title="Move the marker −1 beat">-1b</button>
          <span aria-hidden className="h-[18px] w-px bg-line" />
          <button className={nudge} onClick={() => onNudgeMarker(-10)} disabled={!hasGrid} title="Move the marker −10 ms">−10</button>
          <button className={nudge} onClick={() => onNudgeMarker(-1)} disabled={!hasGrid} title="Move the marker −1 ms">−1</button>
          <button
            onClick={onAddMarker}
            title={hasGrid ? 'Add a grid marker at the playhead' : 'Start the beatgrid at the playhead'}
            className="mx-1 flex h-[30px] items-center gap-1 rounded-lg bg-ink-800 px-2.5 text-xs font-semibold text-text hover:bg-ink-700"
          >
            <Icon name="plus" size={12} strokeWidth={2.4} />
            {hasGrid ? 'Marker' : 'Set grid'}
          </button>
          <button className={nudge} onClick={() => onNudgeMarker(1)} disabled={!hasGrid} title="Move the marker +1 ms">+1</button>
          <button className={nudge} onClick={() => onNudgeMarker(10)} disabled={!hasGrid} title="Move the marker +10 ms">+10</button>
          <span aria-hidden className="h-[18px] w-px bg-line" />
          <button className={nudge} onClick={() => onNudgeMarkerBeats(1)} disabled={!hasGrid} title="Move the marker +1 beat">+1b</button>
        </div>

        {/* Only a marker you are standing on can be deleted, so one scrolled
            off-screen can never go by accident. Deleting the last marker is
            Delete grid, not a second button doing the same thing. */}
        <button
          onClick={onDeleteMarker}
          disabled={!atMarker || !flexible}
          title={
            !flexible
              ? 'A single marker is the whole grid — use Delete grid'
              : atMarker
                ? `Delete marker ${markerIndex + 1}`
                : 'Park the playhead on a marker to delete it'
          }
          className={`${pill} btn-glass text-text`}
        >
          <Icon name="trash" size={13} />
          <span className="hidden @[34rem]:inline">Marker</span>
        </button>
        <button
          onClick={async () => {
            if (
              await askConfirm({
                title: "Delete this track's beatgrid?",
                body: flexible
                  ? `All ${markerCount} grid markers will be removed.`
                  : 'The grid marker will be removed.',
                confirmLabel: 'Delete grid',
              })
            ) {
              onDeleteGrid()
            }
          }}
          disabled={!hasGrid}
          title="Delete the beatgrid"
          className={`${pill} bg-pink/12 text-[#ffc2cf] shadow-[inset_0_0_0_1px_rgb(255_122_154/0.4)] hover:bg-pink/20`}
        >
          <Icon name="trash" size={13} />
          <span className="hidden @[34rem]:inline">Grid</span>
        </button>
        <button onClick={onReset} disabled={!canReset} title="Restore the grid as it was loaded" className={`${pill} btn-glass text-text`}>
          <Icon name="history" size={13} />
          <span className="hidden @[34rem]:inline">Reset</span>
        </button>
      </div>
    </div>
  )
}
