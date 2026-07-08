import { useEffect } from 'react'

export interface ToastMsg {
  id: number
  kind: 'success' | 'error' | 'warning'
  text: string
}

const KIND_CLS: Record<ToastMsg['kind'], string> = {
  error: 'border-pink/40 bg-ink-800 text-pink',
  warning: 'border-gold/40 bg-ink-800 text-gold',
  success: 'border-mint/40 bg-ink-800 text-mint',
}

export function Toast({ toast, onClose }: { toast: ToastMsg | null; onClose: () => void }) {
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(onClose, toast.kind === 'success' ? 4000 : 6000)
    return () => clearTimeout(t)
  }, [toast, onClose])

  if (!toast) return null
  return (
    <div className="pointer-events-none fixed inset-x-0 top-4 z-50 flex justify-center">
      <div
        className={`pointer-events-auto max-w-lg rounded-lg border px-4 py-2.5 text-sm shadow-xl ${KIND_CLS[toast.kind]}`}
      >
        {toast.text}
      </div>
    </div>
  )
}
