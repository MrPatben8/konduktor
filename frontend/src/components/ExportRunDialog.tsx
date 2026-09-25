import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type ExportPreview, type ExportSet, type JobStatus } from '../api'

/**
 * Run an export: confirm what is about to happen, then watch it happen.
 *
 * The preview is fetched fresh every time and never cached. An export set holds
 * LIVE references, so what it would ship genuinely changes between two looks —
 * and a stale preview is worse than none when the next click copies gigabytes.
 *
 * `blocked` arrives as a fact, not a sentence, and is worded here. The one that
 * matters is `destination_not_empty`: an export clears its destination, so a
 * folder Konduktor did not write is REFUSED rather than confirmed. There is no
 * "do it anyway" — the whole point is that Konduktor never deletes what it did
 * not put there.
 */

const POLL_MS = 300

function gb(bytes: number): string {
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(0)} MB`
  return `${(bytes / 1e3).toFixed(0)} kB`
}

interface Props {
  set: ExportSet
  onClose: () => void
  onDone: (msg: string) => void
  onError: (msg: string) => void
}

export function ExportRunDialog({ set, onClose, onDone, onError }: Props) {
  const qc = useQueryClient()
  const [job, setJob] = useState<JobStatus | null>(null)
  const [starting, setStarting] = useState(false)
  const pollRef = useRef<number | null>(null)

  const preview = useQuery<ExportPreview>({
    queryKey: ['export-preview', set.id],
    queryFn: () => api.exportPreview(set.id),
    enabled: !job,
    staleTime: 0,
    gcTime: 0,
  })

  useEffect(() => {
    if (!job || job.state !== 'running') return
    pollRef.current = window.setInterval(async () => {
      try {
        const next = await api.job(job.id)
        setJob(next)
        if (next.state !== 'running') {
          if (pollRef.current) window.clearInterval(pollRef.current)
          if (next.state === 'done') {
            const r = next.result as { tracks?: number; playlists?: number } | null
            qc.invalidateQueries({ queryKey: ['export-contents', set.id] })
            onDone(
              `Exported ${r?.tracks ?? 0} track${r?.tracks === 1 ? '' : 's'}` +
                (r?.playlists ? ` and ${r.playlists} playlist${r.playlists === 1 ? '' : 's'}` : ''),
            )
          } else if (next.state === 'failed') {
            onError(next.error || 'Export failed')
          }
        }
      } catch (e) {
        onError((e as Error).message)
        if (pollRef.current) window.clearInterval(pollRef.current)
      }
    }, POLL_MS)
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current)
    }
  }, [job, qc, set.id, onDone, onError])

  const start = async () => {
    setStarting(true)
    try {
      setJob(await api.runExport(set.id))
    } catch (e) {
      onError((e as Error).message)
    } finally {
      setStarting(false)
    }
  }

  const cancel = async () => {
    if (!job) return
    try {
      await api.cancelJob(job.id)
    } catch (e) {
      onError((e as Error).message)
    }
  }

  const p = preview.data
  const running = job?.state === 'running'
  const finished = job != null && job.state !== 'running'
  const pct = job && job.total > 0 ? Math.min(100, (job.done / job.total) * 100) : null
  const blocked = p?.blocked ?? null

  return (
    <div aria-modal="true" className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="w-full max-w-lg rounded-lg border border-line bg-ink-900 shadow-xl">
        <div className="border-b border-line px-5 py-3">
          <h2 className="text-sm font-semibold text-text">Export “{set.name}”</h2>
        </div>

        <div className="space-y-4 px-5 py-4 text-sm">
          {!job && (
            <>
              <div>
                <span className="mb-1 block text-xs text-muted">To</span>
                <div
                  dir="rtl"
                  title={set.destination}
                  className="truncate rounded-md border border-line bg-ink-950 px-2 py-1.5 font-mono text-xs text-text"
                >
                  {set.destination}
                </div>
              </div>

              {preview.isLoading && <div className="text-faint">Checking…</div>}
              {preview.isError && (
                <div className="text-pink">{(preview.error as Error).message}</div>
              )}

              {p && !blocked && (
                <div className="space-y-2">
                  <div className="text-text">
                    {p.exportable} track{p.exportable === 1 ? '' : 's'}
                    {p.playlists.length > 0 &&
                      ` · ${p.playlists.length} playlist${p.playlists.length === 1 ? '' : 's'}`}
                    <span className="text-muted"> · {gb(p.total_bytes)} to copy</span>
                  </div>
                  {p.free_bytes != null && (
                    <div className={p.enough_space === false ? 'text-pink' : 'text-faint'}>
                      {gb(p.free_bytes)} free
                      {p.enough_space === false && ' — not enough space'}
                    </div>
                  )}
                  {/* Reported and skipped, not a failure halfway through a long
                      copy. A gig stick short four tracks must never be silent. */}
                  {p.missing.length > 0 && (
                    <div className="rounded border border-line bg-ink-950 px-3 py-2 text-xs text-pink">
                      {p.missing.length} track{p.missing.length === 1 ? '' : 's'} will be skipped —
                      the audio file is not on this disk:
                      <div className="mt-1 text-faint">{p.missing.slice(0, 4).join(', ')}</div>
                    </div>
                  )}
                  {p.replacing && (
                    <div className="rounded border border-line bg-ink-950 px-3 py-2 text-xs text-gold">
                      This folder already holds an export. Its files will be replaced.
                    </div>
                  )}
                  <div className="text-xs text-faint">
                    Writes a <span className="font-mono">collection.nml</span> plus a copy of every
                    track, in your own folder structure.
                  </div>
                </div>
              )}

              {blocked === 'destination_not_empty' && (
                <div className="rounded border border-pink/40 bg-ink-950 px-3 py-2 text-xs text-pink">
                  That folder already has files in it that Konduktor didn’t put there. An export
                  replaces what it wrote last time, so it will only write into an empty folder or
                  one of its own. Edit the export and choose somewhere else.
                </div>
              )}
              {blocked === 'nothing_to_export' && (
                <div className="rounded border border-line bg-ink-950 px-3 py-2 text-xs text-muted">
                  There’s nothing to export yet — add some tracks or a playlist.
                </div>
              )}
              {blocked === 'unsupported_target' && (
                <div className="rounded border border-pink/40 bg-ink-950 px-3 py-2 text-xs text-pink">
                  Konduktor can’t write that kind of library yet.
                </div>
              )}
            </>
          )}

          {job && (
            <div className="space-y-3">
              <div className="text-text">{job.message || 'Working…'}</div>
              <div className="h-2 overflow-hidden rounded-full bg-ink-950">
                <div
                  className={`h-full transition-[width] duration-200 ${
                    job.state === 'failed' ? 'bg-pink' : 'bg-accent'
                  } ${pct == null ? 'w-1/3 animate-pulse' : ''}`}
                  style={pct == null ? undefined : { width: `${pct}%` }}
                />
              </div>
              {pct != null && running && (
                <div className="text-xs text-faint">
                  {gb(job.done)} of {gb(job.total)}
                </div>
              )}
              {job.state === 'cancelled' && (
                <div className="text-xs text-muted">
                  Cancelled. Everything this export copied was removed, and no library file was
                  written.
                </div>
              )}
              {job.state === 'failed' && <div className="text-xs text-pink">{job.error}</div>}
              {job.state === 'done' && (
                <div className="space-y-1 text-xs text-mint">
                  <div>Done — the folder is ready.</div>
                  {/* Ben's workflow: the exported collection.nml is swapped into
                      a Traktor install, which REPLACES that machine's own. */}
                  <div className="text-faint">
                    To use it: quit Traktor, back up its existing{' '}
                    <span className="font-mono">collection.nml</span>, then put this one in its
                    place.
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2 border-t border-line px-5 py-3">
          {running ? (
            <>
              <button
                onClick={cancel}
                className="rounded-md border border-line px-3 py-1.5 text-sm text-muted hover:text-text"
              >
                Cancel export
              </button>
              <button
                onClick={onClose}
                className="rounded-md px-3 py-1.5 text-sm text-faint hover:text-text"
                title="The export keeps running"
              >
                Hide
              </button>
            </>
          ) : (
            <>
              <button
                onClick={onClose}
                className="rounded-md border border-line px-3 py-1.5 text-sm text-muted hover:text-text"
              >
                {finished ? 'Close' : 'Cancel'}
              </button>
              {!finished && (
                <button
                  disabled={
                    starting || !p || !!blocked || p.exportable === 0 || p.enough_space === false
                  }
                  onClick={start}
                  className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-ink-950 hover:brightness-110 disabled:opacity-40"
                >
                  {starting ? 'Starting…' : `Export ${p && !blocked ? p.exportable : ''}`.trim()}
                </button>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
