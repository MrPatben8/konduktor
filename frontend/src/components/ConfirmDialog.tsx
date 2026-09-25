import { useEffect, useState, type ReactNode } from 'react'

export interface ConfirmRequest {
  title: string
  /** What will happen, in a sentence or two. */
  body: ReactNode
  /** The confirm button's label, e.g. "Remove 3 tracks". */
  confirmLabel: string
  onConfirm: () => Promise<void> | void
}

interface Props extends ConfirmRequest {
  onClose: () => void
}

/** A yes/no confirmation for destructive actions. Enter confirms and Esc
 *  cancels, so a confirmed Delete key stays a two-keystroke action. */
export function ConfirmDialog({ title, body, confirmLabel, onConfirm, onClose }: Props) {
  const [busy, setBusy] = useState(false)

  const confirm = async () => {
    if (busy) return
    setBusy(true)
    try {
      await onConfirm()
      onClose()
    } catch {
      setBusy(false) // the caller has already reported it; allow a retry
    }
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) onClose()
      // A focused button handles its own Enter — otherwise tabbing to Cancel
      // and pressing Enter would run the destructive action.
      if (e.key === 'Enter' && !(e.target instanceof HTMLButtonElement)) {
        e.preventDefault()
        void confirm()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  return (
    <div
      aria-modal="true"
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/40 backdrop-blur-[3px] p-6"
      onClick={() => !busy && onClose()}
    >
      <div
        className="w-full max-w-sm overflow-hidden glass-overlay"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="border-b border-line px-5 py-4 text-[15px] font-semibold tracking-tight">
          {title}
        </div>
        <div className="space-y-2 px-5 py-4 text-sm text-muted">{body}</div>
        <div className="flex items-center justify-end gap-2 border-t border-line px-5 py-3">
          <button
            onClick={onClose}
            disabled={busy}
            className="rounded-md px-3 py-1.5 text-sm text-muted hover:bg-ink-800 hover:text-text disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            onClick={() => void confirm()}
            disabled={busy}
            className="rounded-full bg-pink/15 shadow-[inset_0_1px_0_rgb(255_255_255/0.12),inset_0_0_0_1px_rgb(255_122_154/0.55)] hover:bg-pink/25 px-4 py-1.5 text-sm font-semibold text-[#ffd0da] disabled:opacity-50"
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
