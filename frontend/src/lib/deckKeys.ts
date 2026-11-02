// The decks' keyboard shortcuts: ONE window listener for every deck, routing
// each key to the deck it is meant for. Keys act on the FOCUSED deck; in
// performance mode, holding Alt/Option sends them to the OTHER deck instead
// (decided 2026-11-02 — Shift and Cmd/Ctrl were already taken by the keys
// themselves). With one deck, Alt does nothing, as before.
//
//   Space            play / pause
//   C                CUE (held = preview)
//   1–9, 0           hotcue slot (held = preview); Shift+ deletes it
//   Q W E R          mute stem 1–4; Shift+ solos it
//   ← / →            beat jump, or move an engaged loop
//   Shift+← / →      step between grid markers
//   Cmd/Ctrl+↑ / ↓   loop / jump size (Shift+↑/↓ is the track table's)
//
// Keys are read from `e.code` (layout- and Shift-independent). A new or changed
// key needs `lib/shortcuts.ts` (the keyboard map) updating by hand.

import { useEffect, type RefObject } from 'react'

/** Keys that mute stems 1-4 (Shift = solo), by `KeyboardEvent.code`. */
export const STEM_KEYS = ['KeyQ', 'KeyW', 'KeyE', 'KeyR']

/** What a deck offers the keyboard (its latest handlers). */
export interface DeckShortcuts {
  slotCount: number
  toggle(): void
  onCuePress(): void
  onCueRelease(): void
  onSlotPress(slot: number): void | Promise<void>
  onSlotRelease(slot: number): void
  deleteHotcueSlot(slot: number): void | Promise<void>
  toggleStem(index: number, solo: boolean): void
  moveOrJump(dir: -1 | 1): void
  prevMarker(): void
  nextMarker(): void
  stepSize(dir: -1 | 1): void
}

export interface DeckKeyTargets {
  focused: DeckShortcuts | null
  /** The other deck, in performance mode (else null: Alt does nothing). */
  other: DeckShortcuts | null
}

export type DeckKeyAction =
  | { kind: 'stepSize'; dir: -1 | 1 }
  | { kind: 'toggle' }
  | { kind: 'cue' }
  | { kind: 'stem'; index: number; solo: boolean }
  | { kind: 'move'; dir: -1 | 1 }
  | { kind: 'marker'; dir: -1 | 1 }
  | { kind: 'slot'; slot: number; remove: boolean }

export interface KeyLike {
  code: string
  shiftKey: boolean
  altKey: boolean
  metaKey: boolean
  ctrlKey: boolean
  repeat: boolean
}

const DIGIT_RE = /^(?:Digit|Numpad)([0-9])$/

/**
 * Digit key -> bank slot: 1..9 map to slots 0..8 and 0 maps to slot 10.
 *
 * Ten digits is the physical ceiling, so a bank larger than that simply has no
 * shortcut for its tail. Returns null when the key is not a digit or the slot is
 * beyond this platform's bank — an 8-slot bank behaves exactly as before.
 */
export function slotForDigit(code: string, slotCount: number): number | null {
  const m = code.match(DIGIT_RE)
  if (!m) return null
  const slot = m[1] === '0' ? 9 : Number(m[1]) - 1
  return slot < slotCount ? slot : null
}

/**
 * What a key-down means, and whether it goes to the other deck. Null = not a
 * deck key (or Alt with no other deck). `slotCount` is the TARGET deck's bank.
 */
export function keyAction(
  e: KeyLike,
  slotCount: (toOther: boolean) => number,
  hasOther: boolean,
): { action: DeckKeyAction; toOther: boolean } | null {
  const toOther = e.altKey
  if (toOther && !hasOther) return null
  const meta = e.metaKey || e.ctrlKey
  // Cmd (macOS) / Ctrl (Windows, Linux), either accepted as Cmd/Ctrl+A is.
  // Repeats allowed: holding it sweeps through the sizes.
  if (meta && !e.shiftKey && (e.code === 'ArrowUp' || e.code === 'ArrowDown')) {
    return { action: { kind: 'stepSize', dir: e.code === 'ArrowUp' ? 1 : -1 }, toOther }
  }
  if (meta) return null
  if (e.repeat) return null // a held key auto-repeats — one press + hold
  const done = (action: DeckKeyAction) => ({ action, toOther })
  if (e.code === 'Space') return done({ kind: 'toggle' })
  if (e.code === 'KeyC') return done({ kind: 'cue' })
  const stem = STEM_KEYS.indexOf(e.code)
  if (stem >= 0) return done({ kind: 'stem', index: stem, solo: e.shiftKey })
  if (e.code === 'ArrowLeft' || e.code === 'ArrowRight') {
    const dir = e.code === 'ArrowLeft' ? -1 : 1
    return done(e.shiftKey ? { kind: 'marker', dir } : { kind: 'move', dir })
  }
  const slot = slotForDigit(e.code, slotCount(toOther))
  if (slot != null) return done({ kind: 'slot', slot, remove: e.shiftKey })
  return null
}

export function runKeyAction(a: DeckKeyAction, sc: DeckShortcuts): void {
  switch (a.kind) {
    case 'stepSize':
      return sc.stepSize(a.dir)
    case 'toggle':
      return sc.toggle()
    case 'cue':
      return sc.onCuePress()
    case 'stem':
      return sc.toggleStem(a.index, a.solo)
    case 'move':
      return sc.moveOrJump(a.dir)
    case 'marker':
      return a.dir < 0 ? sc.prevMarker() : sc.nextMarker()
    case 'slot':
      void (a.remove ? sc.deleteHotcueSlot(a.slot) : sc.onSlotPress(a.slot))
  }
}

/**
 * Attach the listener once; `targets` is read at each key event, so it always
 * routes to the decks as they are now. A key-up goes to the deck its key-down
 * went to, so letting go of Alt first cannot strand a held preview.
 */
export function useDeckKeys(targets: RefObject<DeckKeyTargets>): void {
  useEffect(() => {
    const held = new Map<string, DeckShortcuts>()
    const onKey = (e: KeyboardEvent) => {
      // Ignore while typing in a field.
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
      const { focused, other } = targets.current
      const r = keyAction(e, (toOther) => (toOther ? other : focused)?.slotCount ?? 0, other != null)
      const sc = r && (r.toOther ? other : focused)
      if (!r || !sc) return
      e.preventDefault()
      if (r.action.kind === 'cue' || (r.action.kind === 'slot' && !r.action.remove)) held.set(e.code, sc)
      runKeyAction(r.action, sc)
    }
    // keyup ends a held CUE / hotcue's momentary preview (release).
    const onKeyUp = (e: KeyboardEvent) => {
      const sc = held.get(e.code)
      if (!sc) return
      held.delete(e.code)
      if (e.code === 'KeyC') sc.onCueRelease()
      else {
        const slot = slotForDigit(e.code, sc.slotCount)
        if (slot != null) sc.onSlotRelease(slot)
      }
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('keyup', onKeyUp)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('keyup', onKeyUp)
    }
  }, [targets])
}
