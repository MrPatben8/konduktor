import { useEffect, useRef, useState } from 'react'
import type { VisibilityState } from '@tanstack/react-table'
import { COLUMN_MENU } from '../lib/trackColumns'

interface Props {
  visibility: VisibilityState
  onChange: (v: VisibilityState) => void
  onReset: () => void
}

/** Dropdown of checkboxes to show/hide library columns, plus a layout reset. */
export function ColumnsMenu({ visibility, onChange, onReset }: Props) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  const isVisible = (id: string) => visibility[id] !== false // default-visible unless explicitly false

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1.5 rounded-md border border-line bg-ink-850 px-2.5 py-1.5 text-sm text-text transition-colors hover:border-ink-600"
        title="Choose columns"
      >
        <span className="text-faint">▦</span> Columns
      </button>
      {open && (
        <div className="absolute right-0 z-30 mt-1 w-52 rounded-md border border-line bg-ink-850 p-1 shadow-2xl">
          <div className="max-h-80 overflow-y-auto p-1">
            {COLUMN_MENU.map((c) => (
              <label
                key={c.id}
                className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm text-text hover:bg-ink-800"
              >
                <input
                  type="checkbox"
                  checked={isVisible(c.id)}
                  onChange={(e) => onChange({ ...visibility, [c.id]: e.target.checked })}
                  className="accent-accent"
                />
                {c.label}
              </label>
            ))}
          </div>
          <button
            onClick={() => {
              onReset()
              setOpen(false)
            }}
            className="mt-1 w-full rounded px-2 py-1.5 text-left text-xs text-muted hover:bg-ink-800 hover:text-text"
          >
            Reset to defaults
          </button>
        </div>
      )}
    </div>
  )
}
