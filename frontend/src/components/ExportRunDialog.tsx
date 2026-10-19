import { createPortal } from 'react-dom'
import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type ExportPreview, type ExportSet, type JobStatus } from '../api'
import { FolderPicker } from './FolderPicker'

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
 *
 * The destination can be changed HERE, without going back to Edit: a wrong
 * folder is usually noticed at this last look. The change is SAVED on the set,
 * not used for this run only — a set is bound to its destination, and a run
 * somewhere the set does not know would leave its next export (and the
 * incremental re-export's manifest) pointing at the old folder.
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
  // Remembered on the set (sent with the click), so a stick's next export
  // starts from the same answer. Older sets without it read as on.
  const [lockGrids, setLockGrids] = useState(set.lock_grids ?? true)
  const pollRef = useRef<number | null>(null)
  // The set as last saved from here: `set` is a snapshot, and a destination
  // changed in this dialog must reach the preview and the run.
  const [current, setCurrent] = useState(set)
  const [browsing, setBrowsing] = useState(false)
  const [moving, setMoving] = useState(false)

  // Platform names for wording the facts the preview and result carry as ids.
  const platforms = useQuery({ queryKey: ['export-targets'], queryFn: api.exportTargets })
  const nameOf = (id: string) => platforms.data?.find((p) => p.platform === id)?.name ?? id
  // Offered only where a target can store a lock — a stick format cannot.
  const lockable = set.targets.some(
    (id) => platforms.data?.find((p) => p.platform === id)?.locks_grids,
  )

  const preview = useQuery<ExportPreview>({
    queryKey: ['export-preview', set.id, current.destination],
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
            const r = next.result as { tracks?: number; playlists?: number; unchanged?: number } | null
            qc.invalidateQueries({ queryKey: ['export-contents', set.id] })
            onDone(
              `Exported ${r?.tracks ?? 0} track${r?.tracks === 1 ? '' : 's'}` +
                (r?.playlists ? ` and ${r.playlists} playlist${r.playlists === 1 ? '' : 's'}` : '') +
                (r?.unchanged ? ` (${r.unchanged} unchanged)` : ''),
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

  // Another set writing here would have its output replaced — the same warning
  // the Edit dialog gives, since a folder can now be chosen here too.
  const conflict = useQuery({
    queryKey: ['export-conflict', set.id, current.destination],
    queryFn: () => api.exportContents(set.id),
    enabled: !job,
  }).data?.destination_conflict

  const changeDestination = async (destination: string) => {
    if (destination === current.destination) return
    setMoving(true)
    try {
      setCurrent(await api.updateExport(set.id, { destination }))
      qc.invalidateQueries({ queryKey: ['exports'] })
      qc.invalidateQueries({ queryKey: ['export-contents', set.id] })
    } catch (e) {
      onError((e as Error).message)
    } finally {
      setMoving(false)
    }
  }

  const start = async () => {
    setStarting(true)
    try {
      setJob(await api.runExport(set.id, lockable ? { lock_grids: lockGrids } : {}))
      qc.invalidateQueries({ queryKey: ['exports'] })
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
  // A finished export is a full bar, whatever the last phase reported — a
  // re-export that copied nothing never had a byte count to fill.
  const pct =
    job?.state === 'done'
      ? 100
      : job && job.total > 0
        ? Math.min(100, (job.done / job.total) * 100)
        : null
  const blocked = p?.blocked ?? null

  return createPortal(
    <>
      {browsing && (
        <FolderPicker
          value={current.destination}
          onChange={changeDestination}
          onClose={() => setBrowsing(false)}
        />
      )}
      <div aria-modal="true" className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-[3px] p-4">
        <div className="w-full max-w-lg glass-overlay">
          <div className="border-b border-line px-5 py-3">
            <h2 className="text-sm font-semibold text-text">Export “{set.name}”</h2>
          </div>

          <div className="space-y-4 px-5 py-4 text-sm">
            {!job && (
              <>
                <div>
                  <span className="mb-1 block text-xs text-muted">To</span>
                  <div className="flex items-center gap-2">
                    <div
                      dir="rtl"
                      title={current.destination}
                      className="min-w-0 flex-1 truncate rounded-md well px-2 py-1.5 font-mono text-xs text-text"
                    >
                      {current.destination}
                    </div>
                    <button
                      onClick={() => setBrowsing(true)}
                      disabled={moving || starting}
                      className="shrink-0 btn-glass rounded-full px-3 py-1 text-xs text-muted hover:text-text disabled:opacity-40"
                    >
                      {moving ? 'Saving…' : 'Change…'}
                    </button>
                  </div>
                  {conflict && (
                    <div className="mt-2 rounded well px-3 py-2 text-xs text-gold">
                      “{conflict}” already exports here. Exporting replaces whatever the other one
                      wrote.
                    </div>
                  )}
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
                      <span className="text-muted">
                        {p.unchanged > 0 && ` · ${p.unchanged} unchanged`} · {gb(p.total_bytes)} to copy
                      </span>
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
                      <div className="rounded well px-3 py-2 text-xs text-pink">
                        {p.missing.length} track{p.missing.length === 1 ? '' : 's'} will be skipped —
                        the audio file is not on this disk:
                        <div className="mt-1 text-faint">{p.missing.slice(0, 4).join(', ')}</div>
                      </div>
                    )}
                    {p.replacing && (
                      <div className="rounded well px-3 py-2 text-xs text-gold">
                        This folder already holds an export. Tracks that haven’t changed are kept; everything else is replaced.
                      </div>
                    )}
                    {/* A warning, not a block: the other targets are fine anywhere,
                        and staging a stick's contents in a folder is legitimate. */}
                    {p.not_drive_root.length > 0 && (
                      <div className="rounded well px-3 py-2 text-xs text-gold">
                        {p.not_drive_root.map(nameOf).join(' and ')} is only found at the top of a
                        drive, and this folder isn’t one — players won’t see it unless you move the
                        folder’s contents to a drive’s top level.
                      </div>
                    )}
                    {lockable && (
                      <label className="flex cursor-pointer items-start gap-2.5">
                        <input
                          type="checkbox"
                          className="mt-0.5"
                          checked={lockGrids}
                          onChange={(e) => setLockGrids(e.target.checked)}
                        />
                        <span className="min-w-0">
                          <span className="block text-sm text-text">Lock beatgrids</span>
                          <span className="block text-xs text-faint">
                            Stops tracks being re-analysed when they’re loaded, which can change the
                            BPM or move the grid.
                          </span>
                        </span>
                      </label>
                    )}
                    <div className="text-xs text-faint">
                      Writes a library for {set.targets.map(nameOf).join(' and ')}, sharing one copy
                      of every track under <span className="font-mono">Contents/</span>, in your own
                      folder structure.
                    </div>
                  </div>
                )}

                {blocked === 'destination_not_empty' && (
                  <div className="rounded border border-pink/40 bg-well px-3 py-2 text-xs text-pink">
                    That folder already has files in it that Konduktor didn’t put there. An export
                    replaces what it wrote last time, so it will only write into an empty folder or
                    one of its own. Choose another folder.
                  </div>
                )}
                {blocked === 'nothing_to_export' && p && p.tracks === 0 && (
                  <div className="rounded well px-3 py-2 text-xs text-muted">
                    There’s nothing to export yet — add some tracks or a playlist.
                  </div>
                )}
                {/* Tracks exist but none of their audio does: saying "nothing to
                    export" here sends the user looking in the wrong place. */}
                {blocked === 'nothing_to_export' && p && p.tracks > 0 && (
                  <div className="rounded well px-3 py-2 text-xs text-pink">
                    None of the {p.tracks} track{p.tracks === 1 ? '' : 's'} can be exported — their
                    audio files are not on this disk:
                    <div className="mt-1 text-faint">{p.missing.slice(0, 4).join(', ')}</div>
                  </div>
                )}
                {blocked === 'unsupported_target' && (
                  <div className="rounded border border-pink/40 bg-well px-3 py-2 text-xs text-pink">
                    Konduktor can’t write that kind of library yet.
                  </div>
                )}
              </>
            )}

            {job && (
              <div className="space-y-3">
                <div className="text-text">
                  {job.state === 'done' ? finishedLine(job.result) : job.message || 'Working…'}
                </div>
                <div className="h-2 overflow-hidden rounded-full bg-well">
                  <div
                    className={`h-full transition-[width] duration-200 ${
                      job.state === 'failed' ? 'bg-pink' : 'bg-accent'
                    } ${pct == null ? 'w-1/3 animate-pulse' : ''}`}
                    style={pct == null ? undefined : { width: `${pct}%` }}
                  />
                </div>
                {/* Tracks say their count in the message ("Analysing … (12/207)"). */}
                {pct != null && running && job.unit === 'bytes' && (
                  <div className="text-xs text-faint">
                    {gb(job.done)} of {gb(job.total)}
                  </div>
                )}
                {job.state === 'cancelled' && (
                  <div className="text-xs text-muted">
                    Cancelled. Everything this export copied was removed, and no library was
                    written.
                  </div>
                )}
                {job.state === 'failed' && (
                  <div className="text-xs text-pink">
                    {job.error} — nothing was left behind: no audio, and no library for any platform.
                  </div>
                )}
                {job.state === 'done' && (
                  <div className="space-y-1 text-xs text-mint">
                    <div>Done — the folder is ready.</div>
                    {(
                      (job.result as { libraries?: { platform: string; library: string }[] } | null)
                        ?.libraries ?? []
                    ).map((l) => (
                      <div key={l.platform} className="text-faint">
                        {nameOf(l.platform)}: <span className="font-mono">{l.library}</span>
                      </div>
                    ))}
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
                  className="btn-glass rounded-full px-3 py-1.5 text-sm text-muted hover:text-text"
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
                  className="btn-glass rounded-full px-3 py-1.5 text-sm text-muted hover:text-text"
                >
                  {finished ? 'Close' : 'Cancel'}
                </button>
                {!finished && (
                  <button
                    disabled={
                      starting || moving || preview.isFetching || !p || !!blocked || p.exportable === 0 || p.enough_space === false
                    }
                    onClick={start}
                    className="rounded-full btn-primary px-3 py-1.5 text-sm font-semibold disabled:opacity-40"
                  >
                    {starting ? 'Starting…' : `Export ${p && !blocked ? p.exportable : ''}`.trim()}
                  </button>
                )}
              </>
            )}
          </div>
        </div>
      </div>
    </>,
    document.body,
  )
}

/** "207 tracks exported — 207 unchanged, nothing copied." Worded from the result's counts. */
function finishedLine(result: Record<string, unknown> | null): string {
  const r = (result ?? {}) as { tracks?: number; unchanged?: number }
  const tracks = r.tracks ?? 0
  const unchanged = r.unchanged ?? 0
  const head = `${tracks} track${tracks === 1 ? '' : 's'} exported`
  if (!unchanged) return head
  const copied = tracks - unchanged
  return `${head} — ${unchanged} unchanged, ${
    copied ? `${copied} copied` : 'nothing copied'
  }`
}
