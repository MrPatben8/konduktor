import { useCallback, useEffect, useRef, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'
import type { PlaylistNode, Track, TrackOrigin } from '../api'
import { Icon } from './icons'

/**
 * Drag and drop across the whole window: track rows onto sidebar playlists,
 * exports and the deck; playlists around the sidebar tree.
 *
 * ONE gesture, and the drop TARGET decides what it means. A source calls
 * `beginDrag` once the pointer has moved past its threshold; targets register
 * with `useDropTarget` and are found by hit-testing (`elementFromPoint`, then up
 * through the ancestors), so the virtualized table, the sidebar and the deck
 * need no shared React tree — and the innermost target that accepts wins, with
 * the search falling through to an enclosing one where it declines.
 *
 * Pointer events, NOT HTML5 drag and drop: Tauri's webview captures native drag
 * events for its own file drop on Windows, so HTML5 DnD silently dies in the
 * packaged app.
 *
 * The state lives in a module-level store read through useSyncExternalStore
 * with narrow selectors, so a pointer move re-renders only the target under it
 * (and the ghost) — never the table.
 */

export type DragPayload =
  | {
      kind: 'tracks'
      ids: string[]
      tracks: Track[]
      /** Which library the ids belong to — a device's or a folder's are not
       *  the collection's, so collection targets must not take them as such. */
      origin: TrackOrigin
      /** The editable playlist they were dragged out of, for Option = move. */
      fromPlaylist?: string
      /** Identity of the table that started the drag (its own reorder target). */
      source: object
    }
  | { kind: 'node'; node: PlaylistNode; parentId: string | null }

export interface DropPoint {
  x: number
  y: number
  /** Option/Alt held: tracks dragged out of a playlist MOVE instead of copy. */
  alt: boolean
}

export interface DropSpec<H> {
  /** The hint for a pointer at `at` over this target — anything the target
   *  needs to draw and to drop (an insertion index, a before/into/after zone) —
   *  or null where it does not accept this drop, so the search continues to an
   *  enclosing target. */
  hint: (p: DragPayload, at: DropPoint, el: HTMLElement) => H | null
  /** What a drop here would do, shown on the ghost ("Add to Warmup"). */
  label?: (p: DragPayload, hint: H, at: DropPoint) => string | null
  onDrop: (p: DragPayload, hint: H, at: DropPoint) => void
}

interface State {
  payload: DragPayload | null
  x: number
  y: number
  alt: boolean
  target: HTMLElement | null
  hint: unknown
  label: string | null
}

const IDLE: State = { payload: null, x: 0, y: 0, alt: false, target: null, hint: null, label: null }
let state: State = IDLE
const listeners = new Set<() => void>()
const targets = new Map<HTMLElement, { current: DropSpec<unknown> }>()

function set(next: Partial<State>) {
  state = { ...state, ...next }
  listeners.forEach((l) => l())
}
function subscribe(l: () => void) {
  listeners.add(l)
  return () => listeners.delete(l)
}

/** Keep the previous hint object when the new one has the same fields, so a
 *  target re-renders only when what it draws changes, not on every move. */
function sameHint(a: unknown, b: unknown): boolean {
  if (a === b) return true
  if (!a || !b || typeof a !== 'object' || typeof b !== 'object') return false
  const ka = Object.keys(a)
  return (
    ka.length === Object.keys(b).length &&
    ka.every((k) => (a as Record<string, unknown>)[k] === (b as Record<string, unknown>)[k])
  )
}

/** Find the innermost target under the pointer that accepts, and record its hint. */
function evaluate() {
  const { payload, x, y, alt } = state
  if (!payload) return
  const at = { x, y, alt }
  let el = document.elementFromPoint(x, y) as HTMLElement | null
  while (el) {
    const spec = targets.get(el)
    if (spec) {
      const hint = spec.current.hint(payload, at, el)
      if (hint != null) {
        const keep = state.target === el && sameHint(state.hint, hint) ? state.hint : hint
        set({ target: el, hint: keep, label: spec.current.label?.(payload, keep, at) ?? null })
        return
      }
    }
    el = el.parentElement
  }
  set({ target: null, hint: null, label: null })
}

let detach: (() => void) | null = null

/** Start a drag. Call it once the press has moved past the source's threshold. */
export function beginDrag(payload: DragPayload, at: { x: number; y: number; alt: boolean }) {
  detach?.()
  set({ ...IDLE, payload, ...at })
  document.documentElement.classList.add('is-dragging')
  window.getSelection()?.removeAllRanges()

  const onMove = (e: PointerEvent) => {
    // One notification per move: evaluate() publishes the position with the hint.
    state = { ...state, x: e.clientX, y: e.clientY, alt: e.altKey }
    evaluate()
  }
  const finish = (drop: boolean) => {
    const { payload: p, target, hint, x, y, alt } = state
    detach?.()
    if (!drop || !p || !target) return
    const spec = targets.get(target)
    try {
      spec?.current.onDrop(p, hint, { x, y, alt })
    } catch (err) {
      console.error('drop failed', err)
    }
  }
  const onUp = () => finish(true)
  const onCancel = () => finish(false)
  const onKey = (e: KeyboardEvent) => {
    if (e.key === 'Escape' && e.type === 'keydown') {
      // Before every other Esc handler: cancelling the drag is all Esc does.
      e.preventDefault()
      e.stopImmediatePropagation()
      finish(false)
      return
    }
    if (e.key === 'Alt' && state.alt !== (e.type === 'keydown')) {
      set({ alt: e.type === 'keydown' })
      evaluate()
    }
  }
  // Hold near the edge of a `data-drag-scroll` element to scroll it. Its
  // value is the height covered at the top (a sticky header).
  let raf = 0
  const autoScroll = () => {
    const { x, y } = state
    let scrolled = false
    document.querySelectorAll<HTMLElement>('[data-drag-scroll]').forEach((el) => {
      const r = el.getBoundingClientRect()
      if (x < r.left || x > r.right || y < r.top - 24 || y > r.bottom + 24) return
      const top = r.top + Number(el.dataset.dragScroll || 0)
      const edge = 40
      const dy = y < top + edge ? -Math.ceil((top + edge - y) / 4) : y > r.bottom - edge ? Math.ceil((y - r.bottom + edge) / 4) : 0
      if (!dy) return
      const before = el.scrollTop
      el.scrollTop += Math.max(-20, Math.min(20, dy))
      scrolled ||= el.scrollTop !== before
    })
    if (scrolled) evaluate()
    raf = requestAnimationFrame(autoScroll)
  }
  raf = requestAnimationFrame(autoScroll)

  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', onUp)
  window.addEventListener('pointercancel', onCancel)
  window.addEventListener('keydown', onKey, true)
  window.addEventListener('keyup', onKey, true)
  window.addEventListener('blur', onCancel)
  detach = () => {
    detach = null
    cancelAnimationFrame(raf)
    window.removeEventListener('pointermove', onMove)
    window.removeEventListener('pointerup', onUp)
    window.removeEventListener('pointercancel', onCancel)
    window.removeEventListener('keydown', onKey, true)
    window.removeEventListener('keyup', onKey, true)
    window.removeEventListener('blur', onCancel)
    document.documentElement.classList.remove('is-dragging')
    // The pointerup that ended the drag is followed by a click on whatever
    // is under it; that click is part of the drop, not a click of its own.
    const swallow = (e: MouseEvent) => {
      e.stopPropagation()
      e.preventDefault()
    }
    window.addEventListener('click', swallow, true)
    setTimeout(() => window.removeEventListener('click', swallow, true), 0)
    set(IDLE)
  }
  evaluate()
}

/** The drag in progress, or null. Changes only when a drag starts or ends. */
export function useDragPayload(): DragPayload | null {
  return useSyncExternalStore(subscribe, () => state.payload)
}

/** Register an element as a drop target. Returns the ref to attach and the
 *  current hint while the pointer is over it (null otherwise). */
export function useDropTarget<H>(spec: DropSpec<H>): [(el: HTMLElement | null) => void, H | null] {
  const specRef = useRef(spec as DropSpec<unknown>)
  specRef.current = spec as DropSpec<unknown>
  const elRef = useRef<HTMLElement | null>(null)
  const ref = useCallback((el: HTMLElement | null) => {
    if (elRef.current) targets.delete(elRef.current)
    elRef.current = el
    if (el) targets.set(el, specRef)
  }, [])
  useEffect(() => () => {
    if (elRef.current) targets.delete(elRef.current)
  }, [])
  const hint = useSyncExternalStore(subscribe, () =>
    state.target !== null && state.target === elRef.current ? state.hint : null,
  )
  return [ref, hint as H | null]
}

/** What the ghost says it is carrying. */
function describe(p: DragPayload): { icon: 'music' | 'playlist' | 'folder' | 'smart'; text: string } {
  if (p.kind === 'node')
    return { icon: p.node.kind === 'folder' ? 'folder' : p.node.kind === 'smart' ? 'smart' : 'playlist', text: p.node.name }
  if (p.ids.length === 1) {
    const t = p.tracks[0]
    return { icon: 'music', text: t ? [t.artist, t.title].filter(Boolean).join(' – ') || 'Untitled' : '1 track' }
  }
  return { icon: 'music', text: `${p.ids.length} tracks` }
}

/** The ghost under the cursor. Mounted once, in main.tsx. */
export function DragLayer() {
  const s = useSyncExternalStore(subscribe, () => state)
  if (!s.payload) return null
  const { icon, text } = describe(s.payload)
  return createPortal(
    <div
      className="glass-overlay pointer-events-none fixed left-0 top-0 z-[90] flex max-w-72 flex-col gap-0.5 overflow-hidden rounded-xl px-3 py-1.5 text-xs shadow-lg"
      style={{ transform: `translate(${s.x + 14}px, ${s.y + 12}px)` }}
    >
      <span className="flex min-w-0 items-center gap-1.5 text-text">
        <Icon name={icon} size={13} className="shrink-0 text-muted" />
        <span className="truncate">{text}</span>
      </span>
      {s.label && <span className="truncate text-[11px] text-accent">{s.label}</span>}
    </div>,
    document.body,
  )
}
