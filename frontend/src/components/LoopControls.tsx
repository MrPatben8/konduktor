import { useState } from 'react'

interface Props {
  bpm: number | null // beat-loops need a grid; buttons disable without one
  active: boolean // a loop is currently engaged
  activeBeats: number | null // size of the active loop (null for a manual loop)
  canToggle: boolean // a loop region exists to enable/disable
  snap: boolean
  onToggleSnap: () => void
  onSetLoop: (beats: number) => void
  onLoopIn: () => void
  onLoopOut: () => void
  onToggleActive: () => void
}

// Fixed beat-loop sizes, 1/32 → 32 beats.
const SIZES = [1 / 32, 1 / 16, 1 / 8, 1 / 4, 1 / 2, 1, 2, 4, 8, 16, 32]
const WINDOW = 6 // sizes shown at once (Traktor-style), shifted with the arrows
const DEFAULT_START = 3 // start on 1/4 … 8

function label(size: number): string {
  return size < 1 ? `1/${Math.round(1 / size)}` : String(size)
}

export function LoopControls({
  bpm,
  active,
  activeBeats,
  canToggle,
  snap,
  onToggleSnap,
  onSetLoop,
  onLoopIn,
  onLoopOut,
  onToggleActive,
}: Props) {
  const [start, setStart] = useState(DEFAULT_START)
  const maxStart = SIZES.length - WINDOW
  const visible = SIZES.slice(start, start + WINDOW)
  const hasGrid = !!bpm && bpm > 0

  const btn =
    'flex h-6 min-w-8 items-center justify-center rounded px-1.5 text-xs font-semibold ' +
    'border border-line text-muted hover:text-text disabled:opacity-30'

  return (
    <div className="flex h-9 shrink-0 items-center gap-1.5 border-t border-line bg-ink-900 px-3">
      <button
        onClick={onToggleSnap}
        title="Snap to the nearest beat"
        className={
          'mr-1 rounded px-2 py-0.5 text-xs font-semibold uppercase tracking-wider ' +
          (snap ? 'bg-accent text-ink-950' : 'border border-line text-muted hover:text-text')
        }
      >
        Snap
      </button>

      <button
        onClick={() => setStart((s) => Math.max(0, s - 1))}
        disabled={start === 0}
        className={btn}
        title="Smaller sizes"
      >
        ‹
      </button>
      {visible.map((size) => {
        const isActive = active && activeBeats === size
        return (
          <button
            key={size}
            onClick={() => onSetLoop(size)}
            disabled={!hasGrid}
            title={hasGrid ? `${label(size)}-beat loop` : 'No beatgrid'}
            className={
              'flex h-6 min-w-9 items-center justify-center rounded px-1.5 text-xs font-semibold ' +
              (isActive
                ? 'bg-mint text-ink-950'
                : 'border border-line text-text hover:border-accent disabled:opacity-30')
            }
          >
            {label(size)}
          </button>
        )
      })}
      <button
        onClick={() => setStart((s) => Math.min(maxStart, s + 1))}
        disabled={start === maxStart}
        className={btn}
        title="Larger sizes"
      >
        ›
      </button>

      <div className="mx-1 h-5 w-px bg-line" />

      <button onClick={onLoopIn} className={btn} title="Set loop in point at playhead">
        IN
      </button>
      <button onClick={onLoopOut} className={btn} title="Set loop out point at playhead">
        OUT
      </button>
      <button
        onClick={onToggleActive}
        disabled={!canToggle}
        title={active ? 'Disable loop' : 'Enable loop'}
        className={
          'flex h-6 min-w-9 items-center justify-center rounded px-2 text-sm ' +
          (active
            ? 'bg-mint text-ink-950'
            : 'border border-line text-muted hover:text-text disabled:opacity-30')
        }
      >
        ⟳
      </button>
    </div>
  )
}
