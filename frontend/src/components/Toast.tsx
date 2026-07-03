import { useEffect } from 'react'

export interface ToastMsg {
  id: number
  kind: 'success' | 'error'
  text: string
}

export function Toast({ toast, onClose }: { toast: ToastMsg | null; onClose: () => void }) {
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(onClose, toast.kind === 'error' ? 6000 : 4000)
    return () => clearTimeout(t)
  }, [toast, onClose])

  if (!toast) return null
  return (
    <div className="pointer-events-none fixed inset-x-0 top-4 z-50 flex justify-center">
      <div
        className={`pointer-events-auto max-w-lg rounded-lg border px-4 py-2.5 text-sm shadow-xl ${
          toast.kind === 'error'
            ? 'border-pink/40 bg-ink-800 text-pink'
            : 'border-mint/40 bg-ink-800 text-mint'
        }`}
      >
        {toast.text}
      </div>
    </div>
  )
}
