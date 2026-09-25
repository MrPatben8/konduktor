// Konduktor is one page with unsaved edits held in memory, so browser
// back/forward can only ever mean "leave the app and lose them". Every way in
// is closed here, once, at startup:
//
//   - trackpad swipe      → `overscroll-behavior: none` on html/body (index.css)
//   - mouse back/forward  → buttons 3 and 4, cancelled before the browser acts
//   - keyboard            → Alt+←/→ (Windows/Linux), Cmd+[ / Cmd+] and Cmd+←/→
//                           (macOS) — except in a text field, where Cmd+←/→
//                           moves the caret
//   - anything else (the toolbar button, a gesture a browser still honours)
//                         → a history trap: one extra entry, re-pushed on every
//                           popstate, so "back" lands on this same page.
//
// The trap's entry is pushed on the first user gesture: Chrome skips history
// entries added without one when the user presses back.

const isEditable = (t: EventTarget | null) =>
  t instanceof HTMLElement &&
  (t.isContentEditable || t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT')

const isNavKey = (e: KeyboardEvent) => {
  const arrow = e.key === 'ArrowLeft' || e.key === 'ArrowRight'
  if (e.altKey && !e.metaKey && !e.ctrlKey && arrow) return true
  if (e.metaKey && !e.altKey && !e.ctrlKey && (e.key === '[' || e.key === ']' || arrow)) return true
  return false
}

let installed = false

export function preventHistoryNavigation() {
  if (installed) return
  installed = true

  const blockMouse = (e: MouseEvent) => {
    if (e.button === 3 || e.button === 4) e.preventDefault()
  }
  for (const type of ['mousedown', 'mouseup', 'auxclick'] as const) {
    window.addEventListener(type, blockMouse, true)
  }

  // Capture phase and preventDefault only: the app's own handlers still see
  // the key (←/→ jump the deck), the browser just does not navigate.
  window.addEventListener(
    'keydown',
    (e) => {
      if (isNavKey(e) && !isEditable(e.target)) e.preventDefault()
    },
    true,
  )

  const arm = () => {
    history.pushState({ konduktorTrap: true }, '', location.href)
    window.removeEventListener('pointerdown', arm, true)
    window.removeEventListener('keydown', arm, true)
  }
  window.addEventListener('pointerdown', arm, true)
  window.addEventListener('keydown', arm, true)
  window.addEventListener('popstate', () => {
    history.pushState({ konduktorTrap: true }, '', location.href)
  })
}
