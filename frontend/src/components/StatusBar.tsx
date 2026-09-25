/** A background job's progress, shown at the right of the bar. */
export interface StatusJob {
  label: string
  done: number
  /** 0 until the job knows its size → indeterminate bar. */
  total: number
  /** What it is working on right now, e.g. the current track's title. */
  detail?: string
  cancelling?: boolean
  onCancel: () => void
}

interface Props {
  showing: number
  total: number
  sourceName: string
  /** Rows currently selected; shown only when non-zero. */
  selected?: number
  job?: StatusJob | null
  loading?: boolean
  collectionName?: string | null
  onChangeCollection?: () => void
}

export function StatusBar({
  showing,
  total,
  sourceName,
  selected = 0,
  job,
  loading,
  collectionName,
  onChangeCollection,
}: Props) {
  return (
    <div className="flex items-center gap-3 border-t border-line bg-ink-900 px-4 py-2 text-xs text-muted">
      <span className="font-medium text-text">{sourceName}</span>
      <span className="text-faint">·</span>
      {loading ? (
        <span>Loading…</span>
      ) : (
        <span className="tabular-nums">
          {showing === total ? (
            <>{total.toLocaleString()} tracks</>
          ) : (
            <>
              {showing.toLocaleString()} of {total.toLocaleString()} tracks
            </>
          )}
        </span>
      )}
      {selected > 0 && (
        <>
          <span className="text-faint">·</span>
          <span className="tabular-nums text-accent">{selected.toLocaleString()} selected</span>
        </>
      )}
      {job && (
        <div className="ml-auto flex min-w-0 items-center gap-2">
          <span className="shrink-0 tabular-nums text-text">
            {job.cancelling ? 'Cancelling…' : job.label}
            {job.total > 0 && ` ${Math.min(job.done + 1, job.total)}/${job.total}`}
          </span>
          <span className="h-1.5 w-32 shrink-0 overflow-hidden rounded-full bg-ink-800">
            <span
              className={`block h-full rounded-full bg-accent transition-[width] duration-300 ${
                job.total > 0 ? '' : 'w-1/3 animate-pulse'
              }`}
              style={job.total > 0 ? { width: `${(job.done / job.total) * 100}%` } : undefined}
            />
          </span>
          {job.detail && <span className="max-w-[240px] truncate text-faint">{job.detail}</span>}
          <button
            onClick={job.onCancel}
            disabled={job.cancelling}
            className="shrink-0 rounded px-1.5 text-faint hover:bg-ink-800 hover:text-pink disabled:opacity-40"
            title="Cancel"
          >
            ×
          </button>
        </div>
      )}
      {collectionName && (
        <button
          onClick={onChangeCollection}
          className={`${job ? '' : 'ml-auto '}flex items-center gap-1.5 rounded px-2 py-0.5 text-faint hover:bg-ink-800 hover:text-text`}
          title="Change collection"
        >
          <span className="text-[11px]">⎘</span>
          <span className="max-w-[220px] truncate font-mono text-[11px]">{collectionName}</span>
        </button>
      )}
    </div>
  )
}
