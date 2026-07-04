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

// All fixed beat-loop sizes, 1/32 → 32 beats, shown at once.
const SIZES = [1 / 32, 1 / 16, 1 / 8, 1 / 4, 1 / 2, 1, 2, 4, 8, 16, 32]

function label(size: number): string {
  return size < 1 ? `1/${Math.round(1 / size)}` : String(size)
}

const CELL = 'flex items-center justify-center text-sm font-semibold transition-colors select-none'
const IDLE = 'bg-ink-900 text-text hover:bg-ink-800'
const OFF = 'disabled:opacity-30 disabled:hover:bg-ink-900'

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
  const hasGrid = !!bpm && bpm > 0

  return (
    <div className="flex h-10 shrink-0 items-stretch gap-px border-t border-line bg-ink-950">
      <span className="flex w-16 items-center justify-center bg-ink-900 text-[10px] font-semibold uppercase tracking-wider text-faint">
        Loop
      </span>

      {SIZES.map((size) => {
        const isActive = active && activeBeats === size
        return (
          <button
            key={size}
            onClick={() => onSetLoop(size)}
            disabled={!hasGrid}
            title={hasGrid ? `${label(size)}-beat loop` : 'No beatgrid'}
            className={`${CELL} ${OFF} flex-1 ${isActive ? 'bg-mint text-ink-950' : IDLE}`}
          >
            {label(size)}
          </button>
        )
      })}

      <button onClick={onLoopIn} className={`${CELL} ${IDLE} w-12`} title="Loop in at playhead">
        IN
      </button>
      <button onClick={onLoopOut} className={`${CELL} ${IDLE} w-12`} title="Loop out at playhead">
        OUT
      </button>
      <button
        onClick={onToggleActive}
        disabled={!canToggle}
        title={active ? 'Disable loop' : 'Enable loop'}
        className={`${CELL} ${OFF} w-12 text-base ${active ? 'bg-mint text-ink-950' : IDLE}`}
      >
        ⟳
      </button>
      <button
        onClick={onToggleSnap}
        title="Snap to the nearest beat"
        className={`${CELL} w-16 text-[11px] uppercase tracking-wider ${
          snap ? 'bg-accent text-ink-950' : `${IDLE} text-muted`
        }`}
      >
        Snap
      </button>
    </div>
  )
}
