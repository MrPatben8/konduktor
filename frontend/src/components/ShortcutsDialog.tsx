import { useEffect } from 'react'
import { useCaps } from '../lib/capabilities'

interface Props {
  onClose: () => void
}

// Cmd on macOS, Ctrl elsewhere — the handlers accept either, but a list that
// says "Cmd/Ctrl" everywhere is harder to scan than one that names your key.
const IS_MAC = /Mac|iPhone|iPad/.test(navigator.userAgent)
const MOD = IS_MAC ? '⌘' : 'Ctrl'

type Row = { keys: string[][]; action: string }
type Section = { title: string; rows: Row[] }

/** Hotcue keys are digits in pad order, 0 being the tenth, so the list follows
 *  the loaded library's bank size rather than hard-coding eight. */
function hotcueKeys(slots: number): string {
  const n = Math.min(slots, 10)
  if (n <= 0) return '1'
  if (n <= 9) return `1–${n}`
  return '1–9, 0'
}

function sections(slots: number): Section[] {
  const pads = hotcueKeys(slots)
  return [
    {
      title: 'Deck',
      rows: [
        { keys: [['Space']], action: 'Play / pause' },
        { keys: [['C']], action: 'CUE — jump back, set, or hold to preview' },
        { keys: [['←'], ['→']], action: 'Beat jump back / forward' },
        { keys: [[MOD, '↓'], [MOD, '↑']], action: 'Smaller / larger beat jump' },
        { keys: [['Shift', '←'], ['Shift', '→']], action: 'Previous / next grid marker' },
        { keys: [[pads]], action: 'Hotcue — set if empty, else jump (hold to preview)' },
        { keys: [['Shift', pads]], action: 'Delete hotcue' },
        { keys: [['Drag waveform']], action: 'Scratch' },
      ],
    },
    {
      title: 'Track list',
      rows: [
        { keys: [['Enter']], action: 'Load and play the highlighted track' },
        { keys: [['↑'], ['↓']], action: 'Move the selection' },
        { keys: [['Shift', '↑'], ['Shift', '↓']], action: 'Extend the selection' },
        { keys: [[MOD, 'A']], action: 'Select all' },
        { keys: [['Esc']], action: 'Clear the selection' },
        { keys: [['Delete']], action: 'Remove from this playlist' },
        { keys: [['Shift', 'Click']], action: 'Select a range' },
        { keys: [[MOD, 'Click']], action: 'Add to / remove from the selection' },
        { keys: [['Double-click']], action: 'Edit a cell' },
        { keys: [['Right-click']], action: 'Track menu (header: choose columns)' },
      ],
    },
    {
      title: 'Sidebar',
      rows: [{ keys: [['Right-click']], action: 'New playlist or folder, rename, delete' }],
    },
    {
      title: 'Fields & dialogs',
      rows: [
        { keys: [['Enter']], action: 'Save the edit / confirm' },
        { keys: [['Esc']], action: 'Cancel / close' },
      ],
    },
  ]
}

function Keys({ combo }: { combo: string[] }) {
  return (
    <span className="inline-flex items-center gap-0.5">
      {combo.map((k, i) => (
        <kbd
          key={i}
          className="min-w-[1.5rem] rounded border border-line bg-ink-800 px-1.5 py-0.5 text-center font-sans text-[11px] text-text"
        >
          {k}
        </kbd>
      ))}
    </span>
  )
}

/** Every keyboard shortcut (and the mouse gestures that go with them), from the
 *  settings menu. Keep in step with the handlers in `PrepStrip` and `TrackTable`
 *  — this list is written out by hand, not derived from them. */
export function ShortcutsDialog({ onClose }: Props) {
  const slots = useCaps().cues.hotcue_slots

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      aria-modal="true"
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 p-6"
      onClick={onClose}
    >
      <div
        className="flex max-h-full w-full max-w-lg flex-col overflow-hidden rounded-xl border border-line bg-ink-900 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-line px-5 py-4">
          <div className="text-[15px] font-semibold tracking-tight">Keyboard Shortcuts</div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="rounded px-1.5 text-muted hover:bg-ink-800 hover:text-text"
          >
            ×
          </button>
        </div>
        <div className="space-y-5 overflow-y-auto px-5 py-4">
          {sections(slots).map((s) => (
            <section key={s.title}>
              <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-faint">
                {s.title}
              </div>
              <div className="divide-y divide-line/60">
                {s.rows.map((r, i) => (
                  <div key={i} className="flex items-center justify-between gap-4 py-1.5">
                    <span className="text-sm text-muted">{r.action}</span>
                    <span className="flex shrink-0 items-center gap-1.5">
                      {r.keys.map((combo, j) => (
                        <Keys key={j} combo={combo} />
                      ))}
                    </span>
                  </div>
                ))}
              </div>
            </section>
          ))}
        </div>
      </div>
    </div>
  )
}
