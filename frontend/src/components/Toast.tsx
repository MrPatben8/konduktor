import { createPortal } from 'react-dom'
import { useEffect } from 'react'

export interface ToastMsg {
  id: number
  kind: 'success' | 'error' | 'warning'
  text: string
  /** A button in the toast, e.g. "Details". Keeps the toast up longer. */
  action?: { label: string; onClick: () => void }
}

const KIND_CLS: Record<ToastMsg['kind'], string> = {
  error: 'bg-pink shadow-[0_0_14px_rgb(255_122_154/0.6)]',
  warning: 'bg-gold shadow-[0_0_14px_rgb(255_200_97/0.6)]',
  success: 'bg-mint shadow-[0_0_14px_rgb(61_220_132/0.6)]',
}

export function Toast({ toast, onClose }: { toast: ToastMsg | null; onClose: () => void }) {
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(onClose, toast.action ? 10000 : toast.kind === 'success' ? 4000 : 6000)
    return () => clearTimeout(t)
  }, [toast, onClose])

  if (!toast) return null
  return createPortal(
    <div className="pointer-events-none fixed inset-x-0 top-4 z-[80] flex justify-center">
      <div
        className="glass-overlay pointer-events-auto flex max-w-lg items-center gap-3 !rounded-full px-5 py-2.5 text-sm text-text"
      >
        {/* The glass carries no colour; a lit dot says which kind this is. */}
        <span className={`h-2 w-2 shrink-0 rounded-full ${KIND_CLS[toast.kind]}`} />
        {toast.text}
        {toast.action && (
          <button
            onClick={() => {
              toast.action!.onClick()
              onClose()
            }}
            className="shrink-0 rounded-full px-2 py-0.5 text-xs font-semibold text-accent hover:bg-ink-800"
          >
            {toast.action.label}
          </button>
        )}
      </div>
    </div>,
    document.body,
  )
}
