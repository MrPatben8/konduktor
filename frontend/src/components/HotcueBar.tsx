import type { CuePoint } from '../api'
import { contrastText, cueColor } from '../lib/cues'

interface Props {
  cues: CuePoint[]
  selectedSlot: number | null
  onSlotClick: (slot: number) => void
}

const SLOTS = [0, 1, 2, 3, 4, 5, 6, 7] // Traktor hotcue slots → labelled 1–8

/**
 * The 8 hotcue slots, colour-coded by cue type when assigned (gray when empty).
 * Rendered as a flex-1 group inside the hotcue row; the parent supplies the row
 * chrome and the trailing edit controls. Clicking a slot delegates to the parent
 * (create when empty, jump/trigger when assigned).
 */
export function HotcueBar({ cues, selectedSlot, onSlotClick }: Props) {
  return (
    <div className="flex flex-1 items-stretch gap-px">
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
              'relative flex flex-1 items-center justify-center text-sm font-semibold transition-colors ' +
              (color ? 'hover:brightness-110' : 'bg-ink-900 text-faint hover:bg-ink-800') +
              (selected ? ' ring-2 ring-inset ring-white/80' : '')
            }
          >
            {slot + 1}
          </button>
        )
      })}
    </div>
  )
}
