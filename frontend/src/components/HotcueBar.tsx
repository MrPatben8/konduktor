import type { CuePoint } from '../api'
import { contrastText, cueColor, GRID_MARKER_COLOR } from '../lib/cues'

interface Props {
  cues: CuePoint[]
  selectedSlot: number | null
  onSlotPress: (slot: number) => void
  onSlotRelease: (slot: number) => void
}

const SLOTS = [0, 1, 2, 3, 4, 5, 6, 7] // Traktor hotcue slots → labelled 1–8

/**
 * The 8 hotcue slots, colour-coded by cue type when assigned (gray when empty).
 * Rendered as a flex-1 group inside the hotcue row; the parent supplies the row
 * chrome and the trailing edit controls. Slots respond to press AND release
 * (pointer capture, so a release outside the button still fires) so the parent
 * can implement momentary "cue preview" (play while held). Press creates when
 * empty and jumps/triggers when assigned; release ends any preview.
 *
 * A slot may be held by a grid marker's companion cue (Traktor writes one beside
 * most of its own markers). Those belong to the beatgrid, so they are shown with
 * the marker colour and a ⊞ instead of a number, and pressing one only seeks —
 * the backend refuses hotcue edits on them, and showing that up front is better
 * than surfacing the refusal as an error.
 */
export function HotcueBar({ cues, selectedSlot, onSlotPress, onSlotRelease }: Props) {
  return (
    <div className="flex flex-1 items-stretch gap-px">
      {SLOTS.map((slot) => {
        const cue = cues.find((c) => c.hotcue === slot) ?? null
        const isMarker = cue?.grid_marker != null
        const selected = selectedSlot === slot && !isMarker
        const color = cue ? (isMarker ? GRID_MARKER_COLOR : cueColor(cue)) : null
        return (
          <button
            key={slot}
            onPointerDown={(e) => {
              e.preventDefault()
              // Capture so the matching release fires even if the pointer drifts
              // off the button before it's let go.
              e.currentTarget.setPointerCapture(e.pointerId)
              onSlotPress(slot)
            }}
            onPointerUp={() => onSlotRelease(slot)}
            onPointerCancel={() => onSlotRelease(slot)}
            title={
              isMarker
                ? `Beatgrid marker ${(cue!.grid_marker ?? 0) + 1} — managed by the grid controls`
                : cue
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
            {isMarker ? '⊞' : slot + 1}
          </button>
        )
      })}
    </div>
  )
}
