import { createPortal } from 'react-dom'
import { useEffect } from 'react'

interface Props {
  /** Tracks the user selected. */
  total: number
  /** Unlocked tracks that already have a beatgrid. */
  existing: number
  /** Tracks whose grid is locked — skipped whatever is chosen here. */
  locked: number
  onChoose: (replaceExisting: boolean) => void
  onClose: () => void
}

/** Asked only when a batch analysis would meet existing grids: re-analysing a
 *  grid someone may have hand-fixed is a decision, not a default. */
export function AnalyzeGridDialog({ total, existing, locked, onChoose, onClose }: Props) {
  // "Skip them" would leave nothing to do when every unlocked track has a grid.
  const canSkip = existing < total - locked

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return createPortal(
    <div
      aria-modal="true"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-[3px] p-6"
      onClick={onClose}
    >
      <div
        className="w-full max-w-sm overflow-hidden glass-overlay"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="border-b border-line px-5 py-4">
          <div className="text-[15px] font-semibold tracking-tight">Analyze Grid &amp; BPM</div>
          <div className="text-xs text-muted">
            {total} track{total === 1 ? '' : 's'} selected
          </div>
        </div>
        <div className="space-y-1 px-5 py-4 text-sm text-muted">
          <p>
            <span className="tabular-nums text-text">{existing}</span>{' '}
            {existing === 1 ? 'already has a beatgrid' : 'already have a beatgrid'}. Replacing
            discards any hand edits to {existing === 1 ? 'it' : 'them'}.
          </p>
          {locked > 0 && (
            <p className="text-faint">
              {locked} {locked === 1 ? 'is' : 'are'} locked and will be skipped either way.
            </p>
          )}
        </div>
        <div className="flex items-center justify-end gap-2 border-t border-line px-5 py-3">
          <button
            onClick={onClose}
            className="rounded-md px-3 py-1.5 text-sm text-muted hover:bg-ink-800 hover:text-text"
          >
            Cancel
          </button>
          <button
            onClick={() => onChoose(true)}
            className={
              canSkip
                ? 'rounded-md px-3 py-1.5 text-sm text-muted hover:bg-ink-800 hover:text-text'
                : 'rounded-md bg-accent px-4 py-1.5 text-sm font-medium text-ink-950 hover:brightness-110'
            }
          >
            Replace {existing === 1 ? 'it' : 'them'}
          </button>
          {canSkip && (
            <button
              onClick={() => onChoose(false)}
              className="rounded-full btn-primary px-4 py-1.5 text-sm font-semibold"
            >
              Skip {existing === 1 ? 'it' : 'them'}
            </button>
          )}
        </div>
      </div>
    </div>,
    document.body,
  )
}
