import type { CuePoint } from '../api'
import { contrastText, cueColor, cueGlyph } from '../lib/cues'

interface Props {
  cues: CuePoint[]
  /** Bank size, from capabilities — not every platform has eight. */
  slotCount: number
  /** How a slot is labelled ("1".."8" or "A".."H"). */
  slotLabel: (slot: number) => string
  selectedSlot: number | null
  onSlotPress: (slot: number) => void
  onSlotRelease: (slot: number) => void
}

const READONLY_LABELS: Record<string, string> = {
  beatgrid_companion: 'Beatgrid marker — managed by the grid controls',
  platform_managed: 'Managed by the DJ app — not editable here',
}

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
export function HotcueBar({
  cues,
  slotCount,
  slotLabel,
  selectedSlot,
  onSlotPress,
  onSlotRelease,
}: Props) {
  return (
    <div className="flex flex-1 items-stretch gap-px">
      {Array.from({ length: slotCount }, (_, i) => i).map((slot) => {
        const cue = cues.find((c) => c.role === 'hotcue' && c.slot === slot) ?? null
        // Gate on `editable`, never on why: a platform-owned cue is a read-only
        // case, not a Traktor-companion case.
        const locked = cue != null && !cue.editable
        const selected = selectedSlot === slot && !locked
        const color = cue ? cueColor(cue) : null
        const glyph = cue ? cueGlyph(cue) : ''
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
              locked
                ? (cue!.readonly_reason && READONLY_LABELS[cue!.readonly_reason]) ??
                  'Not editable here'
                : cue
                  ? cue.name && cue.name !== 'n.n.'
                    ? `${slotLabel(slot)}: ${cue.name}`
                    : `Hotcue ${slotLabel(slot)}`
                  : `Hotcue ${slotLabel(slot)} — click to set at playhead`
            }
            style={color ? { backgroundColor: color, color: contrastText(color) } : undefined}
            className={
              'relative flex flex-1 items-center justify-center text-sm font-semibold transition-colors ' +
              (color ? 'hover:brightness-110' : 'bg-ink-900 text-faint hover:bg-ink-800') +
              (selected ? ' ring-2 ring-inset ring-white/80' : '')
            }
          >
            {locked ? '🔒' : slotLabel(slot)}
            {glyph && (
              <span className="absolute right-0.5 top-0 text-[9px] leading-none opacity-80">
                {glyph}
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}
