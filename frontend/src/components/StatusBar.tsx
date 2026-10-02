import { Icon } from '../lib/icons'
import type { RemoteConnection } from '../api'
/** A background job's progress, shown at the right of the bar. */
export interface StatusJob {
  label: string
  done: number
  /** 0 until the job knows its size → indeterminate bar. */
  total: number
  /** What it is working on right now, e.g. the current track's title. This is
   *  the one part that shortens when space runs out. */
  detail?: string
  /** How far the current item has got, 0..1: the TOP half of a split bar (the
   *  bottom half is the whole job, which moves within an item too). */
  fraction?: number
  /** What the current item is doing ("separating 41 %") — never truncated. */
  status?: string
  cancelling?: boolean
  onCancel: () => void
  /** `bytes`: done/total are byte counts (a download), shown in MB. */
  unit?: 'bytes'
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
  /** A remote library's connection; null for a library on this computer. */
  connection?: RemoteConnection | null
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
  connection,
}: Props) {
  return (
    <div className="glass flex h-7 shrink-0 items-center gap-3 !rounded-full px-4 text-[11px] text-muted">
      {/* The view's name gives way on a narrow window — the job section on the
          right is a fixed width and does not. */}
      <span className="min-w-0 truncate font-medium text-text" title={sourceName}>
        {sourceName}
      </span>
      <span className="text-faint">·</span>
      {loading ? (
        <span>Loading…</span>
      ) : (
        <span className="shrink-0 tabular-nums">
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
          <span className="shrink-0 tabular-nums text-accent">{selected.toLocaleString()} selected</span>
        </>
      )}
      {job && (
        // FIXED positions: the section is a fixed width, and everything before
        // the name has a fixed width too (the count sized for its largest value,
        // the status a fixed slot), so neither a long track name nor
        // "decoding" → "separating" can move the bar. The name fills what is
        // left and is the only thing that shortens.
        <div className="ml-auto flex w-[560px] shrink-0 items-center gap-2">
          <span className="shrink-0 text-text">{job.label}</span>
          {job.total > 1 && job.unit !== 'bytes' && (
            <span
              className="-ml-1 shrink-0 text-left tabular-nums text-text"
              style={{ minWidth: `${2 * String(job.total).length + 1}ch` }}
            >
              {Math.min(job.done + 1, job.total)}/{job.total}
            </span>
          )}
          {/* One bar, split horizontally when the job reports per-item progress
              (a stem conversion): TOP = the current item, 0-100 % across the
              full width; BOTTOM = the whole job, (done + fraction) / total, so
              it moves within a long item too. Other jobs: one full-height bar. */}
          {(() => {
            const fill =
              'absolute inset-y-0 left-0 rounded-full bg-gradient-to-r from-[var(--amb-1)] to-accent shadow-[0_0_6px_var(--color-accent)] transition-[width] duration-300'
            const pct = (v: number) => `${Math.max(0, Math.min(1, v)) * 100}%`
            if (job.total <= 0) {
              return (
                <span className="well h-2 w-32 shrink-0 overflow-hidden rounded-full">
                  <span className="block h-full w-1/3 animate-pulse rounded-full bg-accent" />
                </span>
              )
            }
            const overall = (job.done + (job.unit === 'bytes' ? 0 : job.fraction ?? 0)) / job.total
            if (job.unit === 'bytes' || !job.status) {
              return (
                <span className="well relative h-2 w-32 shrink-0 overflow-hidden rounded-full">
                  <span className={fill} style={{ width: pct(overall) }} />
                </span>
              )
            }
            return (
              <span className="flex h-3.5 w-32 shrink-0 flex-col gap-[2px]" title="Top: this track · bottom: overall">
                <span className="well relative flex-1 overflow-hidden rounded-full">
                  <span className={fill} style={{ width: pct(job.fraction ?? 0) }} />
                </span>
                <span className="well relative flex-1 overflow-hidden rounded-full">
                  <span className={fill} style={{ width: pct(overall) }} />
                </span>
              </span>
            )
          })()}
          {/* Status at its natural width: it sits AFTER the bar, so it cannot
              move it — and a fixed slot left a gap before the name. */}
          {(job.cancelling || job.status || (job.unit === 'bytes' && job.total > 0)) && (
            <>
              <span className="shrink-0 tabular-nums text-muted">
                {job.cancelling
                  ? 'cancelling…'
                  : job.unit === 'bytes'
                    ? `${Math.round(job.done / 1e6)} / ${Math.round(job.total / 1e6)} MB`
                    : job.status}
              </span>
              {job.detail && <span className="shrink-0 text-faint">·</span>}
            </>
          )}
          <span className="min-w-0 flex-1 truncate text-faint" title={job.detail}>
            {job.detail}
          </span>
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
      {connection && <ConnectionDot connection={connection} pushRight={!job} />}
      {collectionName && (
        <button
          onClick={onChangeCollection}
          className={`${job || connection ? '' : 'ml-auto '}flex items-center gap-1.5 rounded px-2 py-0.5 text-faint hover:bg-ink-800 hover:text-text`}
          title="Change collection"
        >
          <Icon name="link" size={12} />
          <span className="max-w-[220px] truncate font-mono text-[11px]">{collectionName}</span>
        </button>
      )}
    </div>
  )
}

/** How this computer stands with a remote library's server, in a word. */
function ConnectionDot({ connection, pushRight }: { connection: RemoteConnection; pushRight: boolean }) {
  const { state, via, machine } = connection
  const look = {
    connected: { dot: 'bg-mint shadow-[0_0_6px_var(--color-mint)]', text: via === 'fallback' ? 'Connected · fallback' : 'Connected' },
    reconnecting: { dot: 'bg-gold animate-pulse', text: 'Reconnecting…' },
    offline: { dot: 'bg-pink', text: 'Offline' },
    taken_over: { dot: 'bg-pink', text: `Taken over${machine ? ` by ${machine}` : ''}` },
  }[state]
  const title =
    state === 'connected'
      ? `Connected to the server through its ${via === 'fallback' ? 'fallback' : 'main'} address`
      : state === 'taken_over'
        ? 'Another computer holds this library; it is read-only here'
        : state === 'offline'
          ? 'The server cannot be reached; the library is read-only until it is back'
          : 'Trying to reach the server again'
  return (
    <span className={`${pushRight ? 'ml-auto ' : ''}flex shrink-0 items-center gap-1.5 text-faint`} title={title}>
      <span className={`h-1.5 w-1.5 rounded-full ${look.dot}`} />
      <span>{look.text}</span>
    </span>
  )
}
