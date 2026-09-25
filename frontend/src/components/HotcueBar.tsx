import { useRef, useState, type CSSProperties } from 'react'
import type { CuePoint } from '../api'
import { CUE_TYPE_LABELS, cueColor, cueGlyph, withAlpha } from '../lib/cues'
import { Icon } from '../lib/icons'

interface Props {
  cues: CuePoint[]
  /** Bank size, from capabilities — not every platform has eight. */
  slotCount: number
  /** How a slot is labelled ("1".."8" or "A".."H"). */
  slotLabel: (slot: number) => string
  selectedSlot: number | null
  onSlotPress: (slot: number) => void
  onSlotRelease: (slot: number) => void
  /** Right-click on a pad: the deck opens its Type / Rename / Delete menu. */
  onSlotMenu: (slot: number, x: number, y: number) => void
  /** The pad whose name is being edited in place, if any. */
  renamingSlot: number | null
  onRenameCommit: (slot: number, name: string) => void
  onRenameCancel: () => void
}

const READONLY_LABELS: Record<string, string> = {
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
 * A cue the adapter will not edit (`editable: false`) shows a lock and only
 * seeks; the backend would refuse the edit, and showing that up front is better
 * than surfacing the refusal as an error.
 */
export function HotcueBar({
  cues,
  slotCount,
  slotLabel,
  selectedSlot,
  onSlotPress,
  onSlotRelease,
  onSlotMenu,
  renamingSlot,
  onRenameCommit,
  onRenameCancel,
}: Props) {
  return (
    // The group takes the row's spare width; each pad is capped, so on a wide
    // window the pads keep a pad's proportions and the space is left empty
    // rather than stretching them into bars.
    <div className="flex min-w-0 flex-1 items-center gap-1.5">
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
            onContextMenu={(e) => {
              e.preventDefault()
              onSlotMenu(slot, e.clientX, e.clientY)
            }}
            onPointerDown={(e) => {
              // Right button opens the menu; it must not also trigger the pad.
              if (e.button !== 0) return
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
            style={color ? padStyle(color, selected) : undefined}
            className={
              'relative flex h-11 min-w-0 max-w-[7.5rem] flex-1 flex-col items-start justify-center gap-1 rounded-xl px-2.5 text-left transition-[filter,background-color] ' +
              (color
                ? 'hover:brightness-115'
                : // An empty pad gets the same dark ground, so the row reads as
                  // one bank of pads rather than lit ones and holes.
                  'bg-[rgb(8_10_18/0.4)] text-faint shadow-[inset_0_0_0_1px_rgb(255_255_255/0.09)] hover:bg-[rgb(8_10_18/0.25)]') +
              (selected && !color ? ' ring-2 ring-inset ring-white/70' : '')
            }
          >
            <span
              className="font-mono text-[11px] font-bold leading-none"
              style={color ? { color: lighten(color) } : undefined}
            >
              {locked ? <Icon name="lock" size={11} strokeWidth={2.2} /> : slotLabel(slot)}
            </span>
            {renamingSlot === slot && cue ? (
              <RenameField
                initial={cue.name && cue.name !== 'n.n.' ? cue.name : ''}
                onCommit={(name) => onRenameCommit(slot, name)}
                onCancel={onRenameCancel}
              />
            ) : (
              // A cue without a name shows its type, dimmed, rather than a
              // blank line — the pad still says what it holds.
              <span
                className={`w-full truncate text-[11px] leading-none ${
                  cue && cue.name && cue.name !== 'n.n.' ? 'text-text' : 'text-text/60'
                }`}
              >
                {cue ? (cue.name && cue.name !== 'n.n.' ? cue.name : CUE_TYPE_LABELS[cue.type]) : 'Empty'}
              </span>
            )}
            {glyph && (
              <span
                className="absolute right-1.5 top-1 text-[9px] leading-none opacity-90"
                style={{ color: color ?? undefined }}
              >
                {glyph}
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}

/**
 * A lit pad: tinted glass in the cue's colour with a soft glow of it, the way a
 * backlit controller pad reads. Selection brightens the tint and rings it in
 * the colour itself, so it stays legible whichever colour the cue is.
 */
function padStyle(color: string, selected: boolean): CSSProperties {
  return {
    // The cue colour sits on a DARK GROUND (the last layer), not on bare glass:
    // over a bright or warm ambient wash a thin tint alone came out the same hue
    // as the backdrop and the pad dissolved into it. The ground keeps every pad
    // the same deep base whatever track is loaded.
    background: [
      `linear-gradient(180deg, ${withAlpha(color, selected ? 0.52 : 0.34)}, ${withAlpha(
        color,
        selected ? 0.26 : 0.12,
      )})`,
      'rgba(8,10,18,0.55)',
    ].join(', '),
    boxShadow: [
      'inset 0 1px 0 rgba(255,255,255,0.22)',
      `inset 0 0 0 ${selected ? 2 : 1}px ${withAlpha(color, selected ? 0.95 : 0.7)}`,
      `0 0 ${selected ? 24 : 18}px -4px ${withAlpha(color, selected ? 0.8 : 0.55)}`,
    ].join(', '),
  }
}

/** A cue colour lifted toward white, for the pad's number: the raw colour is
 *  too dark to read as text on its own tint (blue especially). */
function lighten(hex: string, amount = 0.35): string {
  const v = parseInt(hex.slice(1, 7), 16)
  const mix = (c: number) => Math.round(c + (255 - c) * amount)
  return `rgb(${mix(v >> 16)},${mix((v >> 8) & 255)},${mix(v & 255)})`
}

/** The pad's name, edited in place. Enter or clicking away commits; Esc cancels. */
function RenameField({
  initial,
  onCommit,
  onCancel,
}: {
  initial: string
  onCommit: (name: string) => void
  onCancel: () => void
}) {
  const [val, setVal] = useState(initial)
  // Enter commits and unmounts the field, which can fire a trailing blur.
  const done = useRef(false)
  const finish = (commit: boolean) => {
    if (done.current) return
    done.current = true
    if (commit && val.trim() !== initial) onCommit(val.trim())
    else onCancel()
  }
  return (
    <input
      autoFocus
      value={val}
      onChange={(e) => setVal(e.target.value)}
      onFocus={(e) => e.currentTarget.select()}
      onBlur={() => finish(true)}
      // The pad is a button: keep its press/preview handlers out of the field.
      onPointerDown={(e) => e.stopPropagation()}
      onPointerUp={(e) => e.stopPropagation()}
      onKeyDown={(e) => {
        e.stopPropagation() // digits and Space must type, not trigger the deck
        if (e.key === 'Enter') finish(true)
        else if (e.key === 'Escape') finish(false)
      }}
      className="w-full min-w-0 rounded bg-well px-1 text-[11px] leading-4 text-text outline-none ring-1 ring-accent"
    />
  )
}
