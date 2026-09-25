import { memo, useRef, useState } from 'react'
import { Icon } from '../lib/icons'

interface Props {
  /** Tempo of the marker governing the playhead — what the tempo controls act
   *  on. Null when the track has no grid. */
  bpm: number | null
  /** Index of that marker; -1 when there is no grid. */
  markerIndex: number
  /** 0 = no grid, 1 = constant tempo, >1 = a flexible (multi-tempo) grid. */
  markerCount: number
  /** Position (s) of the governing marker; null when there is no grid. */
  markerStart: number | null
  /** The playhead is sitting on the governing marker. */
  atMarker: boolean
  /** The playhead precedes the first marker (whose tempo extrapolates back). */
  beforeFirst: boolean
  locked: boolean
  /** A snapshot of the grid as loaded exists to restore. */
  canReset: boolean
  onSetBpm: (bpm: number) => void
  onNudgeBpm: (delta: number) => void
  /** Octave fix for the governing marker (halve / double its BPM). */
  onHalve: () => void
  onDouble: () => void
  /** Move the governing marker by ±ms. */
  onNudgeMarker: (deltaMs: number) => void
  /** Add a marker at the playhead (creates the grid when there is none). */
  onAddMarker: () => void
  /** Delete the governing marker (offered only while standing on it). */
  onDeleteMarker: () => void
  /** Seek to the adjacent marker — under playhead-derived selection this is
   *  how the user changes which marker they are editing. */
  onPrevMarker: () => void
  onNextMarker: () => void
  onReset: () => void
  onToggleLock: () => void
  onDeleteGrid: () => void
  /** Detect BPM + first beat and rebuild the grid as a single marker. */
  onAnalyze: () => void
  analyzing: boolean
}

/** m:ss.mmm — marker positions need millisecond precision to be useful. */
function markerTime(t: number): string {
  const m = Math.floor(t / 60)
  const s = t - m * 60
  return `${m}:${s < 10 ? '0' : ''}${s.toFixed(3)}`
}

const BTN =
  'btn-glass flex flex-1 items-center justify-center gap-1 rounded-lg py-1 font-mono text-[11px] ' +
  'font-semibold text-text disabled:opacity-30'

/**
 * Beatgrid / tempo panel (Traktor grid-control equivalent): editable BPM, BPM
 * nudge (fine ±0.01 / coarse ±0.25), half/double, tap tempo, grid-phase nudge
 * (±1/±10 ms), add/delete markers, reset to the loaded grid, lock, delete grid.
 *
 * A beatgrid is a list of tempo markers, and the one being edited is derived
 * from the playhead rather than selected separately — so the tempo controls
 * always act on the marker governing what you are hearing. Memoised because the
 * playhead updates every frame and most of those frames change nothing here.
 */
export const GridControls = memo(function GridControls({
  bpm,
  markerIndex,
  markerCount,
  markerStart,
  atMarker,
  beforeFirst,
  locked,
  canReset,
  onSetBpm,
  onNudgeBpm,
  onHalve,
  onDouble,
  onNudgeMarker,
  onAddMarker,
  onDeleteMarker,
  onPrevMarker,
  onNextMarker,
  onReset,
  onToggleLock,
  onDeleteGrid,
  onAnalyze,
  analyzing,
}: Props) {
  const hasGrid = markerCount > 0
  const flexible = markerCount > 1
  const [editing, setEditing] = useState(false)
  const [val, setVal] = useState('')
  const tapsRef = useRef<number[]>([])

  const commit = () => {
    const n = parseFloat(val)
    if (isFinite(n) && n > 0) onSetBpm(Math.round(n * 1000) / 1000)
    setEditing(false)
  }

  const tap = () => {
    const now = performance.now()
    const taps = tapsRef.current
    if (taps.length && now - taps[taps.length - 1] > 2000) taps.length = 0 // reset after a pause
    taps.push(now)
    if (taps.length > 8) taps.shift()
    if (taps.length >= 2) {
      const first = taps[0]
      const avg = (now - first) / (taps.length - 1)
      onSetBpm(Math.round((60000 / avg) * 1000) / 1000)
    }
  }

  return (
    <div className="flex flex-col gap-2">
      {/* BPM readout / editor */}
      <div className="well rounded-2xl px-3 py-2 text-center">
        {editing ? (
          <input
            autoFocus
            value={val}
            onChange={(e) => setVal(e.target.value)}
            onBlur={commit}
            onKeyDown={(e) => {
              if (e.key === 'Enter') commit()
              if (e.key === 'Escape') setEditing(false)
            }}
            className="w-32 rounded-lg bg-ink-800 py-0.5 text-center font-mono text-[26px] font-semibold text-accent outline-none"
          />
        ) : (
          <button
            onClick={() => {
              setVal(bpm != null ? bpm.toFixed(3) : '')
              setEditing(true)
            }}
            className="font-mono text-[26px] font-semibold leading-tight text-accent [text-shadow:0_0_18px_color-mix(in_oklab,var(--color-accent)_55%,transparent)] hover:brightness-110"
            title="Click to type an exact BPM"
          >
            {bpm != null ? bpm.toFixed(2) : '––'}
          </button>
        )}
        {!hasGrid ? (
          <div className="text-[10px] font-semibold uppercase tracking-wider text-faint">
            No grid
          </div>
        ) : !flexible ? (
          <div className="text-[10px] font-semibold uppercase tracking-wider text-faint">BPM</div>
        ) : (
          // Flexible grid: show which marker is in charge and let the user step
          // between them (which seeks — there is no separate selection).
          <div
            className={
              'flex items-center justify-center gap-1 text-[10px] font-semibold tabular-nums ' +
              (beforeFirst ? 'text-faint' : 'text-gold')
            }
            title={
              beforeFirst
                ? "Playhead is before the first marker — marker 1's tempo extrapolates backwards"
                : `Editing marker ${markerIndex + 1} of ${markerCount}`
            }
          >
            <button onClick={onPrevMarker} className="px-1 hover:text-accent" title="Previous marker">
              ‹
            </button>
            <span>
              {beforeFirst ? '↤ ' : '⊞ '}
              {markerIndex + 1}/{markerCount}
              {markerStart != null ? ` · ${markerTime(markerStart)}` : ''}
            </span>
            <button onClick={onNextMarker} className="px-1 hover:text-accent" title="Next marker">
              ›
            </button>
          </div>
        )}
      </div>

      {/* BPM nudge — coarse ±0.25 (outer) / fine ±0.01 (inner) */}
      <div className="flex items-center gap-1">
        <span className="mr-0.5 text-[10px] font-semibold uppercase tracking-wider text-faint">
          BPM
        </span>
        <button
          className={BTN}
          onClick={() => onNudgeBpm(-0.25)}
          disabled={bpm == null}
          title="Nudge BPM −0.25 (coarse)"
        >
          −.25
        </button>
        <button
          className={BTN}
          onClick={() => onNudgeBpm(-0.01)}
          disabled={bpm == null}
          title="Nudge BPM −0.01 (fine)"
        >
          −.01
        </button>
        <button
          className={BTN}
          onClick={() => onNudgeBpm(0.01)}
          disabled={bpm == null}
          title="Nudge BPM +0.01 (fine)"
        >
          +.01
        </button>
        <button
          className={BTN}
          onClick={() => onNudgeBpm(0.25)}
          disabled={bpm == null}
          title="Nudge BPM +0.25 (coarse)"
        >
          +.25
        </button>
      </div>

      {/* halve / double / tap / reset */}
      <div className="flex gap-1">
        <button
          className={BTN}
          onClick={onHalve}
          disabled={bpm == null}
          title={flexible ? `Halve BPM of marker ${markerIndex + 1}` : 'Halve BPM'}
        >
          /2
        </button>
        <button
          className={BTN}
          onClick={onDouble}
          disabled={bpm == null}
          title={flexible ? `Double BPM of marker ${markerIndex + 1}` : 'Double BPM'}
        >
          ×2
        </button>
        <button className={BTN} onClick={tap} title="Tap tempo">
          TAP
        </button>
        <button
          className={BTN}
          onClick={onReset}
          disabled={!canReset}
          title="Restore the beatgrid as loaded"
        >
          Reset
        </button>
      </div>

      {/* grid-phase nudge + set beat 1 */}
      <div className="flex items-center gap-1">
        <span className="mr-0.5 text-[10px] font-semibold uppercase tracking-wider text-faint">
          Grid
        </span>
        {/* Phase nudges move the GOVERNING marker — deliberately not gated on
            atMarker, since the drift is heard at the playhead, not at the marker. */}
        <button
          className={BTN}
          onClick={() => onNudgeMarker(-10)}
          disabled={!hasGrid}
          title="Move this marker −10 ms"
        >
          ◀◀
        </button>
        <button
          className={BTN}
          onClick={() => onNudgeMarker(-1)}
          disabled={!hasGrid}
          title="Move this marker −1 ms"
        >
          ◀
        </button>
        <button
          className={BTN}
          onClick={onAddMarker}
          title={
            hasGrid
              ? 'Add a grid marker at the playhead'
              : 'Set the beatgrid at the playhead'
          }
        >
          {hasGrid ? '+ Mrk' : 'SET'}
        </button>
        <button
          className={BTN}
          onClick={() => onNudgeMarker(1)}
          disabled={!hasGrid}
          title="Move this marker +1 ms"
        >
          ▶
        </button>
        <button
          className={BTN}
          onClick={() => onNudgeMarker(10)}
          disabled={!hasGrid}
          title="Move this marker +10 ms"
        >
          ▶▶
        </button>
      </div>

      {/* lock / delete grid */}
      <div className="flex gap-1">
        <button
          onClick={onToggleLock}
          title={locked ? 'Unlock track' : 'Lock track'}
          className={
            'flex flex-1 items-center justify-center gap-1.5 rounded-lg py-1 text-xs font-semibold transition-colors ' +
            (locked
              ? 'bg-gold/15 text-gold shadow-[inset_0_0_0_1px_rgb(255_200_97/0.45),0_0_16px_-4px_rgb(255_200_97/0.6)]'
              : 'btn-glass text-text')
          }
        >
          <Icon name={locked ? 'lock' : 'unlock'} size={13} />
          {locked ? 'Locked' : 'Lock'}
        </button>
        {/* Only a marker you are standing on can be deleted, so one that is
            scrolled off-screen can never go by accident. Deleting the last
            marker is "delete grid", not a second button doing the same thing. */}
        <button
          className={BTN}
          onClick={onDeleteMarker}
          disabled={!atMarker || !flexible}
          title={
            !flexible
              ? 'Use Delete grid to remove the last marker'
              : atMarker
                ? `Delete grid marker ${markerIndex + 1}`
                : 'Park the playhead on a marker to delete it (‹ › to step)'
          }
        >
          <Icon name="trash" size={12} /> Mrk
        </button>
        <button
          className={BTN}
          onClick={() => {
            if (
              confirm(
                flexible
                  ? `Delete this track's beatgrid? All ${markerCount} grid markers will be removed.`
                  : "Delete this track's beatgrid?",
              )
            ) {
              onDeleteGrid()
            }
          }}
          disabled={!hasGrid}
          title="Delete beatgrid"
        >
          <Icon name="trash" size={12} /> Grid
        </button>
      </div>

      {/* analyze: detect BPM + first beat, set hotcue 1 + grid anchor */}
      <div className="flex gap-1">
        <button
          className={BTN}
          onClick={onAnalyze}
          disabled={analyzing}
          title="Detect BPM and first beat, then set the grid + hotcue 1 (fix octave with ÷2 / ×2)"
        >
          <Icon name="wave" size={13} />
          {analyzing ? 'Analyzing…' : 'Analyze'}
        </button>
      </div>
    </div>
  )
})
