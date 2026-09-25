import { Icon } from '../lib/icons'

interface Props {
  hasGrid: boolean // beat-loops need a beatgrid; buttons disable without one
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

const CELL =
  'flex items-center justify-center rounded-lg font-mono text-xs font-semibold transition-colors select-none'
const IDLE = 'btn-glass text-text'
const OFF = 'disabled:opacity-30'
// A lit green key: an engaged loop should read as ON from across the room.
const LIT =
  'bg-gradient-to-b from-[#7bf0ad] to-mint text-ink-950 shadow-[inset_0_1px_0_rgb(255_255_255/0.6),0_0_14px_rgb(61_220_132/0.55)]'

export function LoopControls({
  hasGrid,
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

  return (
    <div className="flex h-10 shrink-0 items-center gap-1.5 pt-2">
      <span className="flex w-12 shrink-0 items-center text-[10px] font-semibold uppercase tracking-wider text-faint">
        Loop
      </span>

      <div className="well flex h-full min-w-0 flex-1 items-stretch gap-0.5 rounded-xl p-[3px]">
      {SIZES.map((size) => {
        const isActive = active && activeBeats === size
        return (
          <button
            key={size}
            onClick={() => onSetLoop(size)}
            disabled={!hasGrid}
            title={hasGrid ? `${label(size)}-beat loop` : 'No beatgrid'}
            className={`${CELL} ${OFF} flex-1 ${
              isActive ? LIT : 'text-muted hover:bg-ink-800 hover:text-text'
            }`}
          >
            {label(size)}
          </button>
        )
      })}
      </div>

      <button onClick={onLoopIn} className={`${CELL} ${IDLE} h-full w-11`} title="Loop in at playhead">
        IN
      </button>
      <button onClick={onLoopOut} className={`${CELL} ${IDLE} h-full w-11`} title="Loop out at playhead">
        OUT
      </button>
      <button
        onClick={onToggleActive}
        disabled={!canToggle}
        title={active ? 'Disable loop' : 'Enable loop'}
        className={`${CELL} ${OFF} h-full w-11 ${active ? LIT : IDLE}`}
      >
        <Icon name="loop" size={15} />
      </button>
      <button
        onClick={onToggleSnap}
        title="Snap to the nearest beat"
        className={`${CELL} h-full w-14 font-sans text-[11px] uppercase tracking-wider ${
          snap ? 'is-selected text-text' : `${IDLE} text-muted`
        }`}
      >
        Snap
      </button>
    </div>
  )
}
