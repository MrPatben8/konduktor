import { useEffect, useRef } from 'react'
import { nativeDragEnd, nativeDragMove, nativeDragStart } from './drag'

const IS_WINDOWS = /Windows/.test(navigator.userAgent)

/**
 * Files dragged in from Finder / Explorer, fed into the window's ONE drag
 * store (`lib/drag.tsx`) as a `files` payload, so the targets that take track
 * rows — playlists, exports, the deck — take files too, and anywhere else is
 * "add to the collection" (App's own target).
 *
 * Tauri only: the shell's webview hands the OS drag to Tauri, which reports it
 * with real paths — what adding a file IN PLACE needs. A plain browser gives a
 * page `File` objects without paths, so in a browser a file drop is only
 * CAUGHT — left alone, the browser navigates to the dropped file and the app
 * is gone — and `onBrowserDrop` says why nothing happened.
 *
 * `enabled` false (no library open yet) and an open dialog both refuse the
 * drag at its start, before anything is highlighted.
 */
export function useNativeFileDrop(enabled: boolean, onBrowserDrop: () => void) {
  const enabledRef = useRef(enabled)
  enabledRef.current = enabled
  const onBrowserDropRef = useRef(onBrowserDrop)
  onBrowserDropRef.current = onBrowserDrop

  useEffect(() => {
    if (!('__TAURI_INTERNALS__' in window)) {
      // Only drags carrying files: the app's own drags are pointer events,
      // and a text drag into a field must keep working.
      const hasFiles = (e: DragEvent) => !!e.dataTransfer?.types.includes('Files')
      const over = (e: DragEvent) => {
        if (hasFiles(e)) e.preventDefault()
      }
      const drop = (e: DragEvent) => {
        if (!hasFiles(e)) return
        e.preventDefault()
        onBrowserDropRef.current()
      }
      window.addEventListener('dragover', over)
      window.addEventListener('drop', drop)
      return () => {
        window.removeEventListener('dragover', over)
        window.removeEventListener('drop', drop)
      }
    }
    let unlisten: (() => void) | undefined
    let cancelled = false
    let active = false
    void (async () => {
      const { getCurrentWebview } = await import('@tauri-apps/api/webview')
      const off = await getCurrentWebview().onDragDropEvent(({ payload }) => {
        // Tauri calls the position PHYSICAL, but only Windows (WebView2:
        // ScreenToClient) really reports device pixels. macOS (WKWebView) and
        // Linux (WebKitGTK) report the view's own points — CSS pixels already —
        // so dividing there put the ghost at half the cursor's position on a
        // Retina screen (wry 0.55).
        const scale = IS_WINDOWS ? window.devicePixelRatio || 1 : 1
        const at = 'position' in payload ? { x: payload.position.x / scale, y: payload.position.y / scale } : null
        switch (payload.type) {
          case 'enter':
            active =
              enabledRef.current &&
              payload.paths.length > 0 &&
              !document.querySelector('[aria-modal="true"]')
            if (active && at) nativeDragStart({ kind: 'files', paths: payload.paths }, at)
            break
          case 'over':
            if (active && at) nativeDragMove(at)
            break
          case 'drop':
            if (active) nativeDragEnd(true, at ?? undefined)
            active = false
            break
          case 'leave':
            if (active) nativeDragEnd(false)
            active = false
            break
        }
      })
      if (cancelled) off()
      else unlisten = off
    })()
    return () => {
      cancelled = true
      unlisten?.()
      if (active) nativeDragEnd(false)
    }
  }, [])
}
