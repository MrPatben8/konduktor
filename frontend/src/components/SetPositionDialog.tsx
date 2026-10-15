import { createPortal } from 'react-dom'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import type { SortingState } from '@tanstack/react-table'
import { maxPosition, moveToPosition } from '../lib/playlistOrder'
import { COLUMN_MENU } from '../lib/trackColumns'

interface Props {
  /** The playlist's full entry order — never the sorted or filtered view, so
   *  the move is safe whatever the table is showing. */
  order: string[]
  /** The tracks to move, in the order the table shows them. */
  ids: string[]
  /** The table's sort. While there is one, the block may keep that order
   *  instead of its playlist order — which writes the sort into the playlist. */
  sorting: SortingState
  onMove: (order: string[]) => void
  onClose: () => void
}

/** "Key" / "Key descending, then BPM" — the sort as the Order choice names it. */
function describeSort(sorting: SortingState): string {
  const label = (id: string) => COLUMN_MENU.find((c) => c.id === id)?.label ?? id
  return sorting.map((s) => `${label(s.id)}${s.desc ? ' descending' : ''}`).join(', then ')
}

/** "Set Position…": type where the first selected track should end up, and —
 *  while the table is sorted — whether the block keeps its playlist order or
 *  takes the sorted one. */
export function SetPositionDialog({ order, ids, sorting, onMove, onClose }: Props) {
  const moving = new Set(ids)
  const count = order.filter((id) => moving.has(id)).length
  const max = maxPosition(order, moving)
  const current = order.findIndex((id) => moving.has(id)) + 1
  const [value, setValue] = useState(String(current))
  // With one track there is no order to choose; unsorted, both are the same.
  const canSort = sorting.length > 0 && count > 1
  const [mode, setMode] = useState<'playlist' | 'sorted'>('sorted')
  const sorted = canSort && mode === 'sorted'
  const input = useRef<HTMLInputElement>(null)

  useEffect(() => input.current?.select(), [])

  const typed = Number.parseInt(value, 10)
  const position = Number.isFinite(typed) ? Math.min(Math.max(typed, 1), max) : null

  const submit = () => {
    if (position === null) return
    const next = moveToPosition(order, moving, position, sorted ? ids : undefined)
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

  const option = (value: 'playlist' | 'sorted', label: string, detail: ReactNode) => (
    <label
      className={`flex cursor-pointer gap-3 rounded-lg px-3 py-2 ${
        mode === value ? 'is-selected' : 'hover:bg-ink-850'
      }`}
    >
      <input
        type="radio"
        name="position-order"
        checked={mode === value}
        onChange={() => setMode(value)}
        className="mt-0.5 accent-accent"
      />
      <span className="min-w-0 flex-1">
        <span className="block text-text">{label}</span>
        <span className="block text-xs text-faint">{detail}</span>
      </span>
    </label>
  )

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
                ? `Will occupy ${span(position)}${canSort ? '' : ', in the order they have in the playlist.'}`
                : `Now ${span(current)} · will be ${span(position)}`}
          </div>
          {canSort && (
            <div role="radiogroup" aria-label="Order" className="space-y-1">
              <div className="text-xs uppercase tracking-wide text-faint">Order</div>
              {option(
                'sorted',
                `As sorted: ${describeSort(sorting)}`,
                'In the order shown here. The playlist keeps this order when you clear the sort.',
              )}
              {option(
                'playlist',
                'Playlist order',
                'In the order they have in the playlist now, whatever the sort shows.',
              )}
            </div>
          )}
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
