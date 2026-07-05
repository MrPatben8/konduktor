import { useRef, useState } from 'react'

interface Props {
  bpm: number | null
  locked: boolean
  hasGrid: boolean
  onSetBpm: (bpm: number) => void
  onNudgeBpm: (delta: number) => void
  onHalve: () => void
  onDouble: () => void
  onNudge: (deltaMs: number) => void
  onSetHere: () => void
  onReset: () => void
  onToggleLock: () => void
  onDeleteGrid: () => void
}

const BTN =
  'flex flex-1 items-center justify-center rounded border border-line bg-ink-850 py-1 text-xs ' +
  'font-semibold text-text transition-colors hover:border-accent disabled:opacity-30 ' +
  'disabled:hover:border-line'

/**
 * Beatgrid / tempo panel (Traktor grid-control equivalent): editable BPM, BPM
 * nudge (fine ±0.01 / coarse ±0.25), half/double, tap tempo, grid-phase nudge
 * (±1/±10 ms), set beat 1 at the playhead, reset to the loaded values, lock,
 * and delete grid.
 */
export function GridControls({
  bpm,
  locked,
  hasGrid,
  onSetBpm,
  onNudgeBpm,
  onHalve,
  onDouble,
  onNudge,
  onSetHere,
  onReset,
  onToggleLock,
  onDeleteGrid,
}: Props) {
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
      <div className="text-center">
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
            className="w-28 rounded bg-ink-850 py-0.5 text-center text-2xl font-semibold tabular-nums text-text outline-none"
          />
        ) : (
          <button
            onClick={() => {
              setVal(bpm != null ? bpm.toFixed(3) : '')
              setEditing(true)
            }}
            className="text-2xl font-semibold tabular-nums text-text hover:text-accent"
            title="Click to type an exact BPM"
          >
            {bpm != null ? bpm.toFixed(2) : '––'}
          </button>
        )}
        <div className="text-[10px] font-semibold uppercase tracking-wider text-faint">BPM</div>
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
        <button className={BTN} onClick={onHalve} disabled={bpm == null} title="Halve BPM">
          /2
        </button>
        <button className={BTN} onClick={onDouble} disabled={bpm == null} title="Double BPM">
          ×2
        </button>
        <button className={BTN} onClick={tap} title="Tap tempo">
          TAP
        </button>
        <button className={BTN} onClick={onReset} title="Reset to loaded values">
          Reset
        </button>
      </div>

      {/* grid-phase nudge + set beat 1 */}
      <div className="flex items-center gap-1">
        <span className="mr-0.5 text-[10px] font-semibold uppercase tracking-wider text-faint">
          Grid
        </span>
        <button className={BTN} onClick={() => onNudge(-10)} disabled={!hasGrid} title="−10 ms">
          ◀◀
        </button>
        <button className={BTN} onClick={() => onNudge(-1)} disabled={!hasGrid} title="−1 ms">
          ◀
        </button>
        <button className={BTN} onClick={onSetHere} title="Set beat 1 at the playhead">
          SET
        </button>
        <button className={BTN} onClick={() => onNudge(1)} disabled={!hasGrid} title="+1 ms">
          ▶
        </button>
        <button className={BTN} onClick={() => onNudge(10)} disabled={!hasGrid} title="+10 ms">
          ▶▶
        </button>
      </div>

      {/* lock / delete grid */}
      <div className="flex gap-1">
        <button
          onClick={onToggleLock}
          title={locked ? 'Unlock track' : 'Lock track'}
          className={
            'flex flex-1 items-center justify-center gap-1 rounded border py-1 text-xs font-semibold transition-colors ' +
            (locked
              ? 'border-gold bg-gold/15 text-gold'
              : 'border-line bg-ink-850 text-text hover:border-accent')
          }
        >
          {locked ? '🔒 Locked' : '🔓 Lock'}
        </button>
        <button
          className={BTN}
          onClick={onDeleteGrid}
          disabled={!hasGrid}
          title="Delete beatgrid"
        >
          🗑 Grid
        </button>
      </div>
    </div>
  )
}
