import { createPortal } from 'react-dom'
import { useEffect, useRef, useState, type AriaRole, type ReactNode } from 'react'

// Open modals by opening order (taken at first render, so a dialog opened
// from another always ranks above it). Esc is answered by the top one only.
const open = new Set<number>()
let opened = 0

const menuOpen = () => document.querySelector('[role="menu"]') != null

interface Props {
  /** What Esc and a click on the backdrop do: the dialog's Cancel / Close.
   *  `null` while that is not available (a run in progress), so neither
   *  can close the dialog when its own Cancel could not. */
  onDismiss: (() => void) | null
  /** `false`: Esc only. For a dialog whose dismissal decides something a
   *  stray click should not (RelocateDialog). */
  backdropDismiss?: boolean
  /** `nested` for a dialog opened from a dialog or above every other one. */
  layer?: 'dialog' | 'nested'
  /** The backdrop's padding (default `p-6`). */
  padding?: string
  role?: AriaRole
  'aria-label'?: string
  'aria-labelledby'?: string
  children: ReactNode
}

/** The ONE modal container: portal, dimmed backdrop, Esc and outside click.
 *
 *  An outside click closes only when the press AND the release are both on
 *  the backdrop. A `click` alone fires on the nearest common ancestor, so a
 *  text selection started inside the dialog and released outside it used to
 *  land on the backdrop and close the dialog. */
export function Modal({
  onDismiss,
  backdropDismiss = true,
  layer = 'dialog',
  padding = 'p-6',
  role,
  children,
  ...aria
}: Props) {
  const [id] = useState(() => ++opened)
  const dismissRef = useRef(onDismiss)
  dismissRef.current = onDismiss

  useEffect(() => {
    open.add(id)
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return
      if (id !== Math.max(...open)) return
      // A menu over the dialog closes itself on Esc; the dialog stays.
      if (menuOpen()) return
      const dismiss = dismissRef.current
      if (!dismiss) return
      e.preventDefault()
      dismiss()
    }
    window.addEventListener('keydown', onKey)
    return () => {
      open.delete(id)
      window.removeEventListener('keydown', onKey)
    }
  }, [id])

  const pressed = useRef(false)
  const released = useRef(false)

  return createPortal(
    <div
      aria-modal="true"
      role={role}
      {...aria}
      className={`fixed inset-0 ${layer === 'nested' ? 'z-[60]' : 'z-50'} flex items-center justify-center bg-black/40 backdrop-blur-[3px] ${padding}`}
      // React bubbles events through portals, so a nested dialog's or menu's
      // pointer events reach here too; `target === currentTarget` keeps only
      // the ones on THIS backdrop.
      onPointerDown={(e) => {
        // A press that closes an open menu closes only the menu.
        pressed.current = e.target === e.currentTarget && e.button === 0 && !menuOpen()
        released.current = false
      }}
      onPointerUp={(e) => {
        released.current = e.target === e.currentTarget
      }}
      onClick={(e) => {
        const outside = pressed.current && released.current && e.target === e.currentTarget
        pressed.current = released.current = false
        if (outside && backdropDismiss) dismissRef.current?.()
      }}
    >
      {children}
    </div>,
    document.body,
  )
}
