import { useRef, useState } from 'react'

/** A pad's or memory cue's name, edited in place. Enter or clicking away
 *  commits; Esc cancels. Keys stay in the field, so digits and Space type
 *  rather than trigger the deck. */
export function RenameField({
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
