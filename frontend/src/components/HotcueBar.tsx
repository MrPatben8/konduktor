import type { CuePoint } from '../api'
import { contrastText, cueColor } from '../lib/cues'

interface Props {
  cues: CuePoint[]
  selectedSlot: number | null
  onSlotClick: (slot: number) => void
}

const SLOTS = [0, 1, 2, 3, 4, 5, 6, 7] // Traktor hotcue slots → labelled 1–8

/**
 * Traktor-style hotcue bar: all 8 hotcue slots, colour-coded by cue type when
 * assigned (gray when empty). Clicking a slot selects it — the parent decides
 * whether that creates a cue (empty slot) or jumps to it (assigned slot).
 */
export function HotcueBar({ cues, selectedSlot, onSlotClick }: Props) {
  return (
    <div className="flex h-8 shrink-0 items-stretch gap-px border-t border-line bg-ink-950">
      {SLOTS.map((slot) => {
        const cue = cues.find((c) => c.hotcue === slot) ?? null
        const selected = selectedSlot === slot
        const color = cue ? cueColor(cue) : null
        return (
          <button
            key={slot}
            onClick={() => onSlotClick(slot)}
            title={
              cue
                ? cue.name && cue.name !== 'n.n.'
                  ? `${slot + 1}: ${cue.name}`
                  : `Hotcue ${slot + 1}`
                : `Hotcue ${slot + 1} — click to set at playhead`
            }
            style={color ? { backgroundColor: color, color: contrastText(color) } : undefined}
            className={
              'flex flex-1 items-center justify-center text-xs font-semibold transition-[filter] hover:brightness-110 ' +
              (color ? '' : 'text-faint hover:bg-ink-900 ') +
              (selected ? 'ring-2 ring-inset ring-white/80' : '')
            }
          >
            {slot + 1}
          </button>
        )
      })}
    </div>
  )
}
