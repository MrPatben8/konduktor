import { useEffect } from 'react'

export interface MenuItem {
  label: string
  onClick: () => void
  danger?: boolean
}

interface Props {
  x: number
  y: number
  items: MenuItem[]
  onClose: () => void
}

// Lightweight fixed-position menu. Closes on outside click, scroll, or Escape.
export function ContextMenu({ x, y, items, onClose }: Props) {
  useEffect(() => {
    const close = () => onClose()
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    // Defer attaching outside-dismiss listeners by a tick so the very
    // right-click/click that opened the menu can't immediately close it.
    const id = setTimeout(() => {
      window.addEventListener('click', close)
      window.addEventListener('contextmenu', close)
      window.addEventListener('scroll', close, true)
      window.addEventListener('keydown', onKey)
    }, 0)
    return () => {
      clearTimeout(id)
      window.removeEventListener('click', close)
      window.removeEventListener('contextmenu', close)
      window.removeEventListener('scroll', close, true)
      window.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  return (
    <div
      className="fixed z-50 min-w-[160px] overflow-hidden rounded-lg border border-line bg-ink-850 py-1 shadow-2xl"
      style={{ top: Math.min(y, window.innerHeight - 100), left: Math.min(x, window.innerWidth - 180) }}
      onClick={(e) => e.stopPropagation()}
    >
      {items.map((item, i) => (
        <button
          key={i}
          onClick={() => {
            item.onClick()
            onClose()
          }}
          className={`block w-full px-3 py-1.5 text-left text-sm ${
            item.danger
              ? 'text-pink hover:bg-ink-800'
              : 'text-muted hover:bg-ink-800 hover:text-text'
          }`}
        >
          {item.label}
        </button>
      ))}
    </div>
  )
}
