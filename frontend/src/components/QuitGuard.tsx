import { createPortal } from 'react-dom'
import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api'
import { flushPrefs } from '../lib/prefs'

/**
 * Ask before quitting with unsaved changes: Save · Discard · Cancel.
 *
 * The desktop shell (src-tauri/src/lib.rs) no longer quits on its own. Closing
 * the window or Cmd/Ctrl+Q is PREVENTED there and becomes a
 * `konduktor://quit-requested` event; this answers it. It acknowledges at once
 * (`quit_ack`) — the shell quits anyway after ~2 s without one, so a webview
 * that has hung can never trap the user — then either quits (`quit_now`) or
 * asks. Discard goes through the backend rather than just letting the process
 * die, because unsaved state may include files to put back (a stem
 * conversion's parked originals).
 *
 * In a plain browser (dev) there is no shell to prevent anything, so the most
 * that can be done is the browser's own `beforeunload` warning.
 *
 * Mounted once in main.tsx, beside App, so it works on the picker screen too.
 */
export function QuitGuard() {
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState<null | 'save' | 'discard'>(null)
  const [error, setError] = useState<string | null>(null)
  const askingRef = useRef(false)
  const state = useQuery({ queryKey: ['state'], queryFn: api.state, retry: false })
  const dirty = state.data?.dirty ?? false

  // Tauri: answer the shell's quit request.
  useEffect(() => {
    if (!('__TAURI_INTERNALS__' in window)) return
    let unlisten: (() => void) | undefined
    let cancelled = false
    void (async () => {
      const [{ listen }, { invoke }] = await Promise.all([
        import('@tauri-apps/api/event'),
        import('@tauri-apps/api/core'),
      ])
      const off = await listen('konduktor://quit-requested', async () => {
        await invoke('quit_ack')
        if (askingRef.current) return // already asking: a second Cmd+Q changes nothing
        const unsaved = await api
          .state()
          .then((s) => s.dirty)
          .catch(() => false) // no library loaded: nothing to lose
        if (!unsaved) {
          await flushPrefs(1000)
          await invoke('quit_now')
          return
        }
        askingRef.current = true
        setError(null)
        setAsking(true)
      })
      if (cancelled) off()
      else unlisten = off
    })()
    return () => {
      cancelled = true
      unlisten?.()
    }
  }, [])

  // Browser dev: the browser's own warning is all there is.
  useEffect(() => {
    if ('__TAURI_INTERNALS__' in window || !dirty) return
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault()
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])

  const quit = async () => {
    await flushPrefs(1000) // a layout change made just before quitting still lands
    const { invoke } = await import('@tauri-apps/api/core')
    await invoke('quit_now')
  }
  const cancel = async () => {
    askingRef.current = false
    setAsking(false)
    const { invoke } = await import('@tauri-apps/api/core')
    await invoke('quit_cancel')
  }
  const act = async (kind: 'save' | 'discard') => {
    setBusy(kind)
    setError(null)
    try {
      if (kind === 'save') await api.save()
      else await api.discard()
      await quit()
    } catch (e) {
      // Stay open: a failed save must never turn into a silent loss.
      setError(e instanceof Error ? e.message : String(e))
      setBusy(null)
    }
  }

  useEffect(() => {
    if (!asking) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) void cancel()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  if (!asking) return null
  return createPortal(
    <div
      aria-modal="true"
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-[3px] p-6"
    >
      <div className="w-full max-w-sm overflow-hidden glass-overlay">
        <div className="border-b border-line px-5 py-4 text-[15px] font-semibold tracking-tight">
          Save changes before quitting?
        </div>
        <div className="space-y-2 px-5 py-4 text-sm text-muted">
          <p>You have unsaved changes. If you quit without saving, they are lost.</p>
          {error && <p className="text-pink">{error}</p>}
        </div>
        <div className="flex items-center justify-end gap-2 border-t border-line px-5 py-3">
          <button
            onClick={() => void cancel()}
            disabled={!!busy}
            className="btn-glass rounded-full px-4 py-1.5 text-sm text-muted hover:text-text disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            onClick={() => void act('discard')}
            disabled={!!busy}
            className="rounded-full bg-pink/15 px-4 py-1.5 text-sm font-semibold text-[#ffd0da] shadow-[inset_0_1px_0_rgb(255_255_255/0.12),inset_0_0_0_1px_rgb(255_122_154/0.55)] hover:bg-pink/25 disabled:opacity-50"
          >
            {busy === 'discard' ? 'Discarding…' : 'Discard'}
          </button>
          <button
            autoFocus
            onClick={() => void act('save')}
            disabled={!!busy}
            className="btn-primary rounded-full px-4 py-1.5 text-sm font-semibold disabled:opacity-50"
          >
            {busy === 'save' ? 'Saving…' : 'Save and quit'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
