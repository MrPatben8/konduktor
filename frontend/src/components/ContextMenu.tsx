import { createPortal } from 'react-dom'
import type { ReactNode } from 'react'
import { useEffect, useRef, useState } from 'react'
import { Icon } from '../lib/icons'

/** One row of a menu: an action, a submenu (`label ▸`), or a section heading. */
export type MenuItem =
  | { label: string; onClick: () => void; danger?: boolean; hint?: string; icon?: ReactNode; disabled?: boolean }
  | { label: string; submenu: MenuItem[] }
  // Toggles in place and leaves the menu open, so several can be flipped in one go.
  | { label: string; checked: boolean; onToggle: () => void }
  | { heading: string; empty?: string }
  | { separator: true }

interface Props {
  x: number
  y: number
  items: MenuItem[]
  onClose: () => void
}

const SUBMENU_WIDTH = 240
const SUBMENU_MAX_HEIGHT = 288
// A long menu (the header's column chooser lists every column) scrolls rather
// than running the height of the window.
const MENU_MAX_HEIGHT = 400

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

  return createPortal(
    // `contents`: the panels are fixed-positioned, so this wrapper must take no
    // part in layout. As an ordinary empty block it became one more item in
    // the app's gapped flex column and pushed every panel down by a gap.
    <div ref={rootRef} role="menu" className="contents" onClick={(e) => e.stopPropagation()}>
      <MenuPanel
        items={items}
        onClose={onClose}
        className="min-w-[160px]"
        style={(() => {
          const top = Math.min(y, window.innerHeight - 100)
          return {
            top,
            left: Math.min(x, window.innerWidth - 180),
            maxHeight: Math.min(MENU_MAX_HEIGHT, window.innerHeight - top - 8),
          }
        })()}
      />
    </div>,
    document.body,
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
      {/* The glass (tint, blur, rim) is painted by pseudo-elements of THIS box,
          so it must not be the one that scrolls: its ::before/::after would
          scroll away with the first screenful of items and leave the rest on
          bare background. The frame stays put; the inner list scrolls. */}
      <div
        className={`glass-overlay fixed z-[70] flex flex-col overflow-hidden rounded-2xl ${className}`}
        style={style}
      >
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-1.5">
        {items.map((item, i) => {
          if ('separator' in item) {
            return <div key={i} className="mx-2 my-1 border-t border-line" />
          }
          if ('checked' in item) {
            return (
              <button
                key={i}
                role="menuitemcheckbox"
                aria-checked={item.checked}
                onMouseEnter={() => setOpen(null)}
                onClick={item.onToggle}
                className="flex w-full items-center gap-2 rounded-[10px] px-3 py-1.5 text-left text-sm text-muted hover:bg-ink-800 hover:text-text"
              >
                <span
                  className={`flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded border leading-none ${
                    item.checked ? 'border-accent btn-primary' : 'border-ink-600'
                  }`}
                >
                  {item.checked && <Icon name="check" size={10} strokeWidth={3.2} />}
                </span>
                <span className="min-w-0 flex-1 truncate">{item.label}</span>
              </button>
            )
          }
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
                className={`flex w-full items-center justify-between gap-4 rounded-[10px] px-3 py-1.5 text-left text-sm hover:bg-ink-800 hover:text-text ${
                  open?.index === i ? 'bg-ink-800 text-text' : 'text-muted'
                }`}
              >
                <span>{item.label}</span>
                <Icon name="chevronRight" size={12} strokeWidth={2.4} className="text-faint" />
              </button>
            )
          }
          return (
            <button
              key={i}
              disabled={item.disabled}
              onMouseEnter={() => setOpen(null)}
              onClick={() => {
                item.onClick()
                onClose()
              }}
              className={`flex w-full items-center gap-2 rounded-[10px] px-3 py-1.5 text-left text-sm ${
                item.disabled
                  ? 'cursor-default text-faint'
                  : item.danger
                    ? 'text-pink hover:bg-ink-800'
                    : 'text-muted hover:bg-ink-800 hover:text-text'
              }`}
            >
              {item.icon && <span className="flex shrink-0 text-gold">{item.icon}</span>}
              <span className="min-w-0 flex-1 truncate">{item.label}</span>
              {item.hint && (
                <span className="shrink-0 font-mono text-[10px] text-faint">{item.hint}</span>
              )}
            </button>
          )
        })}
        </div>
      </div>
      {open && openItem && 'submenu' in openItem && (
        <MenuPanel items={openItem.submenu} onClose={onClose} style={open.style} />
      )}
    </>
  )
}
