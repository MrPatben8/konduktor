import type { StemBatchResult } from '../api'
import { Modal } from './Modal'

/**
 * The Details behind a finished conversion's toast: every track that was
 * skipped or failed, with its reason (decided: a long skip list must not vanish
 * into a toast).
 */
export function StemReportDialog({ result, onClose }: { result: StemBatchResult; onClose: () => void }) {
  const rows = [
    ...result.failed.map((s) => ({ ...s, failed: true })),
    ...result.skipped.map((s) => ({ ...s, failed: false })),
  ]
  const n = result.converted.length
  return (
    <Modal onDismiss={onClose} padding="p-4">
      <div className="flex max-h-[80vh] w-full max-w-lg flex-col overflow-hidden glass-overlay">
        <div className="border-b border-line px-5 py-3 text-sm font-semibold text-text">
          {n} converted to stems · {result.skipped.length} skipped · {result.failed.length} failed
        </div>
        <ul className="min-h-0 flex-1 space-y-1 overflow-y-auto px-5 py-3 text-xs">
          {rows.map((r) => (
            <li key={`${r.failed}-${r.track_id}`} className="flex gap-3">
              <span className="min-w-0 flex-1 truncate text-text" title={r.title}>
                {r.title}
              </span>
              <span className={`shrink-0 ${r.failed ? 'text-pink' : 'text-faint'}`}>
                {r.failed ? 'failed: ' : ''}
                {r.reason}
              </span>
            </li>
          ))}
        </ul>
        <div className="flex items-center justify-between gap-2 border-t border-line px-5 py-3">
          <span className="text-xs text-faint">{n ? 'Save to keep the converted tracks.' : ''}</span>
          <button onClick={onClose} className="btn-glass rounded-full px-3 py-1.5 text-sm text-muted hover:text-text">
            Close
          </button>
        </div>
      </div>
    </Modal>
  )
}
