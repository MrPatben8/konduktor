/** Jump-size cycle, in beats. Fractional sizes allow fine micro-nudging. */
export const BEAT_JUMP_SIZES = [0.25, 0.5, 1, 2, 4, 8, 16, 32]

interface Props {
  /** Current jump size (one of BEAT_JUMP_SIZES). */
  beats: number
  /** Step the jump size down (-1) / up (+1) through BEAT_JUMP_SIZES. */
  onStep: (dir: -1 | 1) => void
  /** Jump the playhead back (-1) / forward (+1) by `beats`. */
  onJump: (dir: -1 | 1) => void
  /** Disabled when the loaded track has no beatgrid. */
  disabled: boolean
}

const BTN =
  'flex h-7 w-7 shrink-0 items-center justify-center rounded border border-line bg-ink-850 ' +
  'text-xs font-semibold text-text transition-colors hover:border-accent disabled:opacity-30 ' +
  'disabled:hover:border-line'

/** Beats-only readout: 0.25 → "1/4", 0.5 → "1/2", else the integer. */
function sizeLabel(beats: number): string {
  if (beats === 0.25) return '1/4'
  if (beats === 0.5) return '1/2'
  return String(beats)
}

/**
 * Beat-jump control cluster (Traktor/Serato equivalent): step the jump size,
 * then jump the playhead forward/back by that many beats — phase-preserving,
 * playback-only. Presentational + controlled; PrepStrip owns the size state and
 * performs the actual seek. Disabled wholesale when the track is ungridded.
 */
export function BeatJumpControls({ beats, onStep, onJump, disabled }: Props) {
  const atMin = beats <= BEAT_JUMP_SIZES[0]
  const atMax = beats >= BEAT_JUMP_SIZES[BEAT_JUMP_SIZES.length - 1]
  return (
    <div className="flex items-center gap-1">
      <span className="mr-0.5 text-[10px] font-semibold uppercase tracking-wider text-faint">
        Jump
      </span>
      <button
        className={BTN}
        onClick={() => onStep(-1)}
        disabled={disabled || atMin}
        title="Smaller jump size"
      >
        −
      </button>
      <span className="w-9 text-center text-xs font-semibold tabular-nums text-text">
        {sizeLabel(beats)}
      </span>
      <button
        className={BTN}
        onClick={() => onStep(1)}
        disabled={disabled || atMax}
        title="Larger jump size"
      >
        +
      </button>
      <span className="mx-0.5 w-px self-stretch bg-line" aria-hidden />
      <button
        className={BTN}
        onClick={() => onJump(-1)}
        disabled={disabled}
        title="Jump back (←)"
      >
        ◀
      </button>
      <button
        className={BTN}
        onClick={() => onJump(1)}
        disabled={disabled}
        title="Jump forward (→)"
      >
        ▶
      </button>
    </div>
  )
}
