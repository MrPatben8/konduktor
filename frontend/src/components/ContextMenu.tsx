import { useEffect, useRef, useState } from 'react'

/** One row of a menu: an action, a submenu (`label ▸`), or a section heading. */
export type MenuItem =
  | { label: string; onClick: () => void; danger?: boolean; hint?: string; icon?: string }
  | { label: string; submenu: MenuItem[] }
  | { heading: string; empty?: string }

interface Props {
  x: number
  y: number
  items: MenuItem[]
  onClose: () => void
}

const SUBMENU_WIDTH = 240
const SUBMENU_MAX_HEIGHT = 288

// Lightweight fixed-position menu. Closes on outside click, scroll, or Escape.
export function ContextMenu({ x, y, items, onClose }: Props) {
  const rootRef = useRef<HTMLDivElement>(null)
  // Read through a ref so the listeners are attached ONCE. Callers pass an
  // inline `onClose`; if the effect depended on it, a click that re-renders the
  // parent (e.g. selecting a row) would detach the listeners before the
  // window's click handler ran, and the menu would never close.
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  useEffect(() => {
    const close = () => onCloseRef.current()
    // Scrolling a long submenu must not dismiss the menu it belongs to.
    const onScroll = (e: Event) => {
      if (e.target instanceof Node && rootRef.current?.contains(e.target)) return
      close()
    }
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onCloseRef.current()
    // Defer attaching outside-dismiss listeners by a tick so the very
    // right-click/click that opened the menu can't immediately close it.
    const id = setTimeout(() => {
      window.addEventListener('click', close)
      window.addEventListener('contextmenu', close)
      window.addEventListener('scroll', onScroll, true)
      window.addEventListener('keydown', onKey)
    }, 0)
    return () => {
      clearTimeout(id)
      window.removeEventListener('click', close)
      window.removeEventListener('contextmenu', close)
      window.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('keydown', onKey)
    }
  }, [])

  return (
    <div ref={rootRef} role="menu" onClick={(e) => e.stopPropagation()}>
      <MenuPanel
        items={items}
        onClose={onClose}
        className="min-w-[160px]"
        style={{ top: Math.min(y, window.innerHeight - 100), left: Math.min(x, window.innerWidth - 180) }}
      />
    </div>
  )
}

function MenuPanel({
  items,
  onClose,
  className = '',
  style,
}: {
  items: MenuItem[]
  onClose: () => void
  className?: string
  style: React.CSSProperties
}) {
  // The open submenu: which row, and where to draw it. Fixed-positioned from
  // the row's rect, flipping left / up when it would run off the window.
  const [open, setOpen] = useState<{ index: number; style: React.CSSProperties } | null>(null)

  const openSubmenu = (index: number, row: HTMLElement) => {
    const r = row.getBoundingClientRect()
    const left =
      r.right + SUBMENU_WIDTH > window.innerWidth ? Math.max(0, r.left - SUBMENU_WIDTH) : r.right
    const top = Math.max(8, Math.min(r.top - 4, window.innerHeight - SUBMENU_MAX_HEIGHT - 8))
    setOpen({ index, style: { left, top, width: SUBMENU_WIDTH, maxHeight: SUBMENU_MAX_HEIGHT } })
  }

  const openItem = open ? items[open.index] : null

  return (
    <>
      <div
        className={`fixed z-50 overflow-y-auto rounded-lg border border-line bg-ink-850 py-1 shadow-2xl ${className}`}
        style={style}
      >
        {items.map((item, i) => {
          if ('heading' in item) {
            return (
              <div key={i}>
                <div className="px-3 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-wider text-faint">
                  {item.heading}
                </div>
                {item.empty && <div className="px-3 py-1.5 text-sm text-faint">{item.empty}</div>}
              </div>
            )
          }
          if ('submenu' in item) {
            return (
              <button
                key={i}
                onMouseEnter={(e) => openSubmenu(i, e.currentTarget)}
                onClick={(e) => openSubmenu(i, e.currentTarget)}
                className={`flex w-full items-center justify-between gap-4 px-3 py-1.5 text-left text-sm hover:bg-ink-800 hover:text-text ${
                  open?.index === i ? 'bg-ink-800 text-text' : 'text-muted'
                }`}
              >
                <span>{item.label}</span>
                <span className="text-[10px] text-faint">▸</span>
              </button>
            )
          }
          return (
            <button
              key={i}
              onMouseEnter={() => setOpen(null)}
              onClick={() => {
                item.onClick()
                onClose()
              }}
              className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm ${
                item.danger
                  ? 'text-pink hover:bg-ink-800'
                  : 'text-muted hover:bg-ink-800 hover:text-text'
              }`}
            >
              {item.icon && <span className="shrink-0 text-[11px] text-gold">{item.icon}</span>}
              <span className="min-w-0 flex-1 truncate">{item.label}</span>
              {item.hint && (
                <span className="shrink-0 text-[10px] tabular-nums text-faint">{item.hint}</span>
              )}
            </button>
          )
        })}
      </div>
      {open && openItem && 'submenu' in openItem && (
        <MenuPanel items={openItem.submenu} onClose={onClose} style={open.style} />
      )}
    </>
  )
}
