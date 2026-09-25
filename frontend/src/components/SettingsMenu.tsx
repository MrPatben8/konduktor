import { useEffect, useRef, useState } from 'react'
import { ShortcutsDialog } from './ShortcutsDialog'

interface Props {
  onOpenPathMapping: () => void
  /** Open the menu above the button — for a button at the bottom of the window. */
  up?: boolean
}

/** Overflow menu for low-frequency, collection-level settings. Currently holds
 * path remapping and the shortcut list; the natural home for future advanced
 * settings. The shortcuts dialog is owned here, since nothing else opens it. */
export function SettingsMenu({ onOpenPathMapping, up = false }: Props) {
  const [open, setOpen] = useState(false)
  const [showShortcuts, setShowShortcuts] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  const item =
    'flex w-full flex-col items-start rounded px-2 py-1.5 text-left hover:bg-ink-800'

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex h-full items-center justify-center rounded-md border border-line bg-ink-850 px-2 py-1.5 text-muted transition-colors hover:border-ink-600 hover:text-text"
        title="Settings"
        aria-label="Settings"
      >
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          className="h-5 w-5"
        >
          <circle cx="12" cy="12" r="3" />
          <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
        </svg>
      </button>
      {open && (
        <div
          className={`absolute right-0 z-30 w-56 rounded-md border border-line bg-ink-850 p-1 shadow-2xl ${
            up ? 'bottom-full mb-1' : 'mt-1'
          }`}
        >
          <button
            onClick={() => {
              onOpenPathMapping()
              setOpen(false)
            }}
            className={item}
          >
            <span className="text-sm text-text">Path remapping…</span>
            <span className="text-[11px] text-faint">
              Point the collection at moved or relocated files
            </span>
          </button>
          <button
            onClick={() => {
              setShowShortcuts(true)
              setOpen(false)
            }}
            className={item}
          >
            <span className="text-sm text-text">View Shortcuts</span>
            <span className="text-[11px] text-faint">Every keyboard shortcut in one list</span>
          </button>
        </div>
      )}
      {showShortcuts && <ShortcutsDialog onClose={() => setShowShortcuts(false)} />}
    </div>
  )
}
