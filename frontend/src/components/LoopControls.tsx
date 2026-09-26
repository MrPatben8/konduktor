import { Icon } from '../lib/icons'

/** One size, 1/32 → 32 beats, shared by beat loops AND beat jump: the number a
 *  DJ has in their head ("4 beats") should not differ between the two. */
export const LOOP_SIZES = [1 / 32, 1 / 16, 1 / 8, 1 / 4, 1 / 2, 1, 2, 4, 8, 16, 32]

export function sizeLabel(size: number): string {
  return size < 1 ? `1/${Math.round(1 / size)}` : String(size)
}

export type LoopMode = 'beat' | 'manual'

interface Props {
  mode: LoopMode
  onMode: (mode: LoopMode) => void
  hasGrid: boolean // beat loops and jumps are sized in beats; inert without a grid
  ready: boolean
  /** The shared loop/jump size, in beats. */
  size: number
  onSmaller: () => void
  onBigger: () => void
  /** A loop is engaged right now (either kind). */
  loopActive: boolean
  /** The engaged loop is a beat loop of exactly `size` — the size key is lit. */
  sizeLit: boolean
  onToggleSize: () => void
  // manual mode
  canToggle: boolean // a loop region exists to switch on/off
  /** IN was pressed and is waiting for OUT. */
  inArmed: boolean
  /** A manual (IN/OUT) loop exists, engaged or not. */
  manualSet: boolean
  onLoopIn: () => void
  onLoopOut: () => void
  onToggleLoop: () => void
  /** ‹ ›: move an engaged loop by its own length, else jump by `size`. */
  onMove: (dir: -1 | 1) => void
}

const LIT =
  'bg-gradient-to-b from-[#7bf0ad] to-mint text-ink-950 shadow-[inset_0_1px_0_rgb(255_255_255/0.6),0_0_14px_rgb(61_220_132/0.55)]'
// Every key presses in when clicked, so a press is felt even when what it did
// (a loop moved, an in-point set) is somewhere else on screen.
const KEY =
  'flex items-center justify-center rounded-[9px] transition-[color,background-color,box-shadow,transform,filter] duration-75 ' +
  'active:scale-[0.93] active:brightness-125 disabled:opacity-30 disabled:active:scale-100'
// One half of the ‹ › pair: its own hover and press, inside the shared surface.
const HALF =
  'flex h-10 w-[34px] items-center justify-center text-text transition-[background-color,transform] duration-75 ' +
  'hover:bg-white/[0.08] active:scale-[0.92] active:bg-white/[0.14] disabled:opacity-30 ' +
  'disabled:hover:bg-transparent disabled:active:scale-100'
// An armed IN: a green rim and glow — "waiting for OUT".
const ARMED =
  'bg-mint/15 text-mint shadow-[inset_0_0_0_1px_rgb(61_220_132/0.75),0_0_12px_-3px_rgb(61_220_132/0.7)]'
// A set point (the loop exists): green text on a faint green tint.
const SET = 'bg-mint/10 text-mint shadow-[inset_0_0_0_1px_rgb(61_220_132/0.35)]'
// BOTH modes' clusters are this wide, so switching BEAT ⇄ MAN never shifts
// the pads and tempo controls to their right.
const CLUSTER = 'well flex h-10 w-[8.5rem] shrink-0 items-center gap-0.5 rounded-xl p-[3px]'

/**
 * The deck's loop + jump cluster: a BEAT / MAN switch, then either the beat
 * size (− size +, the size itself toggling a loop of that length) or the manual
 * IN / OUT / toggle, then ‹ › which move an engaged loop or jump the playhead.
 *
 * Presentational: PrepStrip owns every piece of state and does the seeking.
 */
export function LoopControls({
  mode,
  onMode,
  hasGrid,
  ready,
  size,
  onSmaller,
  onBigger,
  loopActive,
  sizeLit,
  onToggleSize,
  canToggle,
  inArmed,
  manualSet,
  onLoopIn,
  onLoopOut,
  onToggleLoop,
  onMove,
}: Props) {
  const atMin = size <= LOOP_SIZES[0]
  const atMax = size >= LOOP_SIZES[LOOP_SIZES.length - 1]
  const what = loopActive ? 'Move the loop' : `Jump ${sizeLabel(size)} beat${size === 1 ? '' : 's'}`
  // A beat jump needs a grid; moving a manual loop does not.
  const canMove = ready && (loopActive || hasGrid)

  const tab = (m: LoopMode, text: string, title: string) => (
    <button
      onClick={() => onMode(m)}
      title={title}
      aria-pressed={mode === m}
      className={`h-[17px] rounded-[7px] px-[7px] text-[9px] font-bold tracking-[0.08em] transition-colors ${
        mode === m ? 'btn-primary' : 'text-faint hover:text-text'
      }`}
    >
      {text}
    </button>
  )

  return (
    <div className="flex shrink-0 items-center gap-2">
      <div role="group" aria-label="Loop mode" className="well flex h-10 flex-col justify-center gap-0.5 rounded-[10px] p-0.5">
        {tab('beat', 'BEAT', 'Beat loops, sized in beats')}
        {tab('manual', 'MAN', 'Manual loops: set IN and OUT')}
      </div>

      {mode === 'beat' ? (
        <div role="group" aria-label="Loop size" className={`${CLUSTER} font-mono`}>
          <button
            onClick={onSmaller}
            disabled={atMin}
            title="Halve the size (Cmd/Ctrl+↓)"
            className={`${KEY} h-[34px] w-7 text-[15px] text-muted hover:bg-ink-800 hover:text-text`}
          >
            −
          </button>
          <button
            onClick={onToggleSize}
            disabled={!hasGrid || !ready}
            title={hasGrid ? `${sizeLabel(size)}-beat loop — click to switch on or off` : 'No beatgrid'}
            className={`${KEY} h-[34px] min-w-0 flex-1 px-2 text-sm font-semibold ${
              sizeLit ? LIT : 'btn-glass text-text'
            }`}
          >
            {sizeLabel(size)}
          </button>
          <button
            onClick={onBigger}
            disabled={atMax}
            title="Double the size (Cmd/Ctrl+↑)"
            className={`${KEY} h-[34px] w-7 text-[15px] text-muted hover:bg-ink-800 hover:text-text`}
          >
            +
          </button>
        </div>
      ) : (
        <div role="group" aria-label="Manual loop" className={`${CLUSTER} text-xs font-semibold`}>
          <button
            onClick={onLoopIn}
            disabled={!ready}
            aria-pressed={inArmed || manualSet}
            title={inArmed ? 'Loop in is set — now press OUT (or IN again to move it)' : 'Loop in at the playhead'}
            className={`${KEY} h-[34px] min-w-0 flex-1 ${inArmed ? ARMED : manualSet ? SET : 'btn-glass text-text'}`}
          >
            IN
          </button>
          <button
            onClick={onLoopOut}
            disabled={!ready || (!inArmed && !manualSet)}
            aria-pressed={manualSet && !inArmed}
            title={
              inArmed
                ? 'Loop out at the playhead'
                : manualSet
                  ? "Move the loop's end to the playhead"
                  : 'Set IN first'
            }
            className={`${KEY} h-[34px] min-w-0 flex-1 ${manualSet && !inArmed ? SET : 'btn-glass text-text'}`}
          >
            OUT
          </button>
          <button
            onClick={onToggleLoop}
            disabled={!canToggle}
            title={loopActive ? 'Switch the loop off' : 'Switch the loop on'}
            aria-pressed={loopActive}
            className={`${KEY} h-[34px] min-w-0 flex-1 ${loopActive ? LIT : 'btn-glass text-text'}`}
          >
            <Icon name="loop" size={15} />
          </button>
        </div>
      )}

      <div role="group" aria-label="Jump or move loop" className="glass-group flex h-10 items-center rounded-xl">
        <button
          onClick={() => onMove(-1)}
          disabled={!canMove}
          title={`${what} back (←)`}
          className={`${HALF} rounded-l-xl`}
        >
          <Icon name="chevronLeft" size={15} strokeWidth={2.2} />
        </button>
        <button
          onClick={() => onMove(1)}
          disabled={!canMove}
          title={`${what} forward (→)`}
          className={`${HALF} rounded-r-xl`}
        >
          <Icon name="chevronRight" size={15} strokeWidth={2.2} />
        </button>
      </div>
    </div>
  )
}
