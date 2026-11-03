import { Icon } from '../../lib/icons'

/** Performance mode on / off — at the right of the track list's toolbar
 *  (decided 2026-11-03), lit while on. P does the same. */
export function PerformanceButton({ on, onToggle }: { on: boolean; onToggle: () => void }) {
  return (
    <button
      onClick={onToggle}
      aria-pressed={on}
      aria-label="Performance mode"
      title={on ? 'Leave performance mode (P)' : 'Performance mode — a second deck and a mixer, to try transitions (P)'}
      className={`flex shrink-0 items-center gap-2 rounded-full px-3.5 py-1.5 text-sm font-medium ${
        on ? 'is-selected text-text' : 'btn-glass text-text'
      }`}
    >
      <Icon name="decks" size={16} />
      Performance
    </button>
  )
}
