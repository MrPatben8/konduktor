// `askConfirm()` — the app's own replacement for `window.confirm()`.
//
// It is awaited exactly like the native one, so a call site stays a single
// line, but shows the themed `ConfirmDialog` instead of the browser's grey box.
// `ConfirmHost` (mounted once, in main.tsx) renders whichever request is open;
// a module-level handle is what lets plain event handlers — and components
// that have no dialog state of their own — ask without threading state around.
//
// Never call `window.confirm` / `alert` / `prompt` in this app: they cannot be
// themed, they block the event loop (a playing deck stutters), and under Tauri
// they read as a web page rather than the app.
import { useEffect, useState } from 'react'
import { ConfirmDialog, type ConfirmRequest } from '../components/ConfirmDialog'

type Ask = Omit<ConfirmRequest, 'onConfirm' | 'onAlt' | 'altLabel'>
type Choice = 'confirm' | 'alt' | null
type Pending = { req: Ask & { altLabel?: string }; resolve: (choice: Choice) => void }

let open: ((p: Pending) => void) | null = null

/** Show a confirmation and resolve true (confirmed) or false (cancelled). */
export function askConfirm(req: Ask): Promise<boolean> {
  return askChoice(req).then((c) => c === 'confirm')
}

/** A confirmation with a SECOND way to go ahead (`altLabel`): resolves
 *  'confirm', 'alt', or null when cancelled. */
export function askChoice(req: Ask & { altLabel?: string }): Promise<Choice> {
  return new Promise((resolve) => {
    if (open) open({ req, resolve })
    // No host mounted (should not happen outside tests): refuse rather than
    // fall back to a native dialog, since a destructive action must not go
    // ahead unconfirmed.
    else resolve(null)
  })
}

/** Renders the open confirmation, if any. Mount exactly once. */
export function ConfirmHost() {
  const [pending, setPending] = useState<Pending | null>(null)
  useEffect(() => {
    open = (p) =>
      setPending((prev) => {
        prev?.resolve(null) // a second ask supersedes an unanswered one
        return p
      })
    return () => {
      open = null
    }
  }, [])
  if (!pending) return null
  return (
    <ConfirmDialog
      {...pending.req}
      // A promise settles once, so confirm-then-close resolves 'confirm'.
      onConfirm={() => pending.resolve('confirm')}
      onAlt={() => pending.resolve('alt')}
      onClose={() => {
        pending.resolve(null)
        setPending(null)
      }}
    />
  )
}
