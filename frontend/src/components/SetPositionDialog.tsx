import { createPortal } from 'react-dom'
import { useEffect, useRef, useState } from 'react'
import { maxPosition, moveToPosition } from '../lib/playlistOrder'

interface Props {
  /** The playlist's full entry order — never the sorted or filtered view, so
   *  the move is safe whatever the table is showing. */
  order: string[]
  /** The tracks to move; they keep their playlist order as one block. */
  ids: string[]
  onMove: (order: string[]) => void
  onClose: () => void
}

/** "Set Position…": type where the first selected track should end up. */
export function SetPositionDialog({ order, ids, onMove, onClose }: Props) {
  const moving = new Set(ids)
  const count = order.filter((id) => moving.has(id)).length
  const max = maxPosition(order, moving)
  const current = order.findIndex((id) => moving.has(id)) + 1
  const [value, setValue] = useState(String(current))
  const input = useRef<HTMLInputElement>(null)

  useEffect(() => input.current?.select(), [])

  const typed = Number.parseInt(value, 10)
  const position = Number.isFinite(typed) ? Math.min(Math.max(typed, 1), max) : null

  const submit = () => {
    if (position === null) return
    const next = moveToPosition(order, moving, position)
    if (next.some((id, i) => id !== order[i])) onMove(next)
    onClose()
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const span = (p: number) => (count > 1 ? `#${p}–${p + count - 1}` : `#${p}`)

  return createPortal(
    <div
      aria-modal="true"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-[3px] p-6"
      onClick={onClose}
    >
      <form
        className="w-full max-w-sm overflow-hidden glass-overlay"
        onClick={(e) => e.stopPropagation()}
        onSubmit={(e) => {
          e.preventDefault()
          submit()
        }}
      >
        <div className="border-b border-line px-5 py-4 text-[15px] font-semibold tracking-tight">
          Set position
        </div>
        <div className="space-y-3 px-5 py-4 text-sm text-muted">
          <label className="flex items-center gap-2">
            <span>{count > 1 ? `Move ${count} tracks to position` : 'Move to position'}</span>
            <input
              ref={input}
              type="number"
              inputMode="numeric"
              min={1}
              max={max}
              value={value}
              onChange={(e) => setValue(e.target.value)}
              className="w-20 rounded-lg well px-2.5 py-1.5 text-sm tabular-nums text-text outline-none focus:ring-1 focus:ring-accent"
            />
            <span>of {max}</span>
          </label>
          <div className="tabular-nums">
            {position === null
              ? 'Type a position.'
              : count > 1
                ? `Will occupy ${span(position)}`
                : `Now ${span(current)} · will be ${span(position)}`}
          </div>
        </div>
        <div className="flex items-center justify-end gap-2 border-t border-line px-5 py-3">
          <button
            type="button"
            onClick={onClose}
            className="btn-glass rounded-full px-4 py-1.5 text-sm text-muted hover:text-text"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={position === null}
            className="btn-primary rounded-full px-4 py-1.5 text-sm font-semibold disabled:opacity-50"
          >
            Move
          </button>
        </div>
      </form>
    </div>,
    document.body,
  )
}
