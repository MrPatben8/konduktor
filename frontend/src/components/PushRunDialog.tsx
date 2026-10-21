import { createPortal } from 'react-dom'
import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type ExportRunOptions, type ExportSet, type JobStatus, type PushPreview, type PushTarget } from '../api'
import { askConfirm } from '../lib/confirm'
import { localLibraryProblem } from '../lib/platformCopy'

/**
 * Push a "computer" set into this computer's Traktor / rekordbox: confirm what
 * is about to happen, then watch it happen. The sibling of `ExportRunDialog`,
 * with the same rule — the preview takes the same body as the run, and is
 * fetched fresh every time, never cached: the set's references are live.
 *
 * A running DJ app BLOCKS the push (Traktor writes its collection back when it
 * quits, undoing it), and the preview re-checks every two seconds while it does,
 * so quitting the app is all it takes. Overwrite is confirmed once more, naming
 * the other pushes it removes. Normalise is not here: the pushed files are
 * shared by every set, so their level is a setting (Settings → Local libraries).
 *
 * A library the set names that is not on THIS computer is skipped, not a block:
 * a set's targets are shared by every computer that opens the library.
 */

const POLL_MS = 300

function gb(bytes: number): string {
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(0)} MB`
  return `${(bytes / 1e3).toFixed(0)} kB`
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`

interface Props {
  set: ExportSet
  onClose: () => void
  onDone: (msg: string) => void
  onError: (msg: string) => void
}

interface TargetResult {
  name: string
  added: number
  updated: number
  removed: number
  kept: string[]
  replaced?: boolean
}

export function PushRunDialog({ set, onClose, onDone, onError }: Props) {
  const qc = useQueryClient()
  const [job, setJob] = useState<JobStatus | null>(null)
  const [starting, setStarting] = useState(false)
  // Remembered on the set, sent with the click, as an export's are.
  const [lockGrids, setLockGrids] = useState(set.lock_grids ?? true)
  const [autoGain, setAutoGain] = useState(set.auto_gain ?? true)
  const pollRef = useRef<number | null>(null)
  const options: ExportRunOptions = { lock_grids: lockGrids, auto_gain: autoGain }
  const overwrite = set.mode === 'overwrite'

  const preview = useQuery<PushPreview>({
    queryKey: ['push-preview', set.id, options],
    queryFn: () => api.pushPreview(set.id, options),
    enabled: !job,
    staleTime: 0,
    gcTime: 0,
    // Quitting the DJ app is the fix for the commonest block: notice it.
    refetchInterval: (q) => (q.state.data?.blocked === 'app_running' ? 2000 : false),
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
            qc.invalidateQueries({ queryKey: ['local-libraries'] })
            onDone(doneMessage(set.name, next.result))
          } else if (next.state === 'failed') {
            onError(next.error || 'Push failed')
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
  }, [job, qc, set.name, onDone, onError])

  const p = preview.data
  const blocked = p?.blocked ?? null
  const active = p?.targets.filter((t) => !t.skipped) ?? []

  const start = async () => {
    if (overwrite && p) {
      const wiped = active.flatMap((t) => t.wipes_sets)
      const ok = await askConfirm({
        title: `Replace ${active.map((t) => t.name).join(' and ')}’s library?`,
        body:
          `Everything in it is replaced by “${set.name}” — your own tracks and playlists too` +
          (wiped.length ? `, and the pushes of ${wiped.map((n) => `“${n}”`).join(', ')}` : '') +
          '. A backup is kept, but the DJ app will only show this set.',
        confirmLabel: 'Replace',
      })
      if (!ok) return
    }
    setStarting(true)
    try {
      setJob(await api.runPush(set.id, options))
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

  const running = job?.state === 'running'
  const finished = job != null && job.state !== 'running'
  const pct =
    job?.state === 'done' ? 100 : job && job.total > 0 ? Math.min(100, (job.done / job.total) * 100) : null
  const results = ((job?.result as { targets?: TargetResult[] } | null)?.targets ?? [])

  return createPortal(
    <div aria-modal="true" className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-[3px] p-4">
      <div className="w-full max-w-lg glass-overlay">
        <div className="border-b border-line px-5 py-3">
          <h2 className="text-sm font-semibold text-text">
            {overwrite ? 'Replace with' : 'Push'} “{set.name}”
          </h2>
        </div>

        <div className="space-y-4 px-5 py-4 text-sm">
          {!job && (
            <>
              {preview.isLoading && <div className="text-faint">Checking…</div>}
              {preview.isError && <div className="text-pink">{(preview.error as Error).message}</div>}

              {p && (
                <div className="space-y-1.5">
                  {p.targets.map((t) => (
                    <TargetLine key={t.platform} target={t} overwrite={overwrite} />
                  ))}
                </div>
              )}

              {p && blocked !== 'nothing_to_push' && (
                <div className="space-y-2">
                  <div className="text-text">
                    {plural(p.pushable, 'track')}
                    {p.playlists.length > 0 && ` · ${plural(p.playlists.length, 'playlist')}`}
                    <span className="text-muted">
                      {p.unchanged > 0 && ` · ${p.unchanged} already copied`} · {gb(p.total_bytes)} to copy
                    </span>
                  </div>
                  <div className="text-xs text-faint">
                    Into <span className="font-mono">{p.audio_root}</span>
                    {p.free_bytes != null && (
                      <span className={p.enough_space === false ? 'text-pink' : ''}>
                        {' '}· {gb(p.free_bytes)} free{p.enough_space === false && ' — not enough space'}
                      </span>
                    )}
                  </div>
                  {p.missing.length > 0 && (
                    <div className="rounded well px-3 py-2 text-xs text-pink">
                      {plural(p.missing.length, 'track')} will be skipped — the audio file is not on this disk:
                      <div className="mt-1 text-faint">{p.missing.slice(0, 4).join(', ')}</div>
                    </div>
                  )}
                  {overwrite && active.some((t) => t.wipes_sets.length) && (
                    <div className="rounded well px-3 py-2 text-xs text-gold">
                      Also removes what{' '}
                      {[...new Set(active.flatMap((t) => t.wipes_sets))].map((n) => `“${n}”`).join(', ')}{' '}
                      pushed there, and deletes its files.
                    </div>
                  )}
                  {active.some((t) => t.platform === 'traktor') && (
                    <label className="flex cursor-pointer items-start gap-2.5">
                      <input type="checkbox" className="mt-0.5" checked={lockGrids}
                        onChange={(e) => setLockGrids(e.target.checked)} />
                      <span className="min-w-0">
                        <span className="block text-sm text-text">Lock beatgrids</span>
                        <span className="block text-xs text-faint">
                          Stops Traktor re-analysing the tracks when they’re loaded, which can change the
                          BPM or move the grid.
                        </span>
                      </span>
                    </label>
                  )}
                  <label className="flex cursor-pointer items-start gap-2.5">
                    <input type="checkbox" className="mt-0.5" checked={autoGain}
                      onChange={(e) => setAutoGain(e.target.checked)} />
                    <span className="min-w-0">
                      <span className="block text-sm text-text">Auto gain</span>
                      <span className="block text-xs text-faint">
                        Sets each track’s auto gain from its loudness, so tracks load at matching levels.
                      </span>
                    </span>
                  </label>
                  <div className="text-xs text-faint">
                    {p.normalised > 0
                      ? `${plural(p.normalised, 'copy', 'copies')} brought to the shared level. `
                      : ''}
                    Normalising is set for all pushes in Settings → Local libraries.
                  </div>
                  {p.to_measure > 0 && (
                    <div className="text-xs text-faint">
                      {plural(p.to_measure, 'track')} measured first (about a second each).
                    </div>
                  )}
                </div>
              )}

              {blocked === 'app_running' && (
                <div className="rounded border border-pink/40 bg-well px-3 py-2 text-xs text-pink">
                  Quit {active.filter((t) => t.running).map((t) => t.name).join(' and ')} to push —
                  it would overwrite the push when it next saves its library.
                </div>
              )}
              {blocked === 'target_unavailable' && (
                <div className="rounded border border-pink/40 bg-well px-3 py-2 text-xs text-pink">
                  Konduktor can’t push into every library this set is for. Check Settings → Local
                  libraries, or edit the set.
                </div>
              )}
              {blocked === 'no_library_here' && p && (
                <div className="rounded border border-pink/40 bg-well px-3 py-2 text-xs text-pink">
                  {p.targets.map((t) => t.name).join(' and ')}{' '}
                  {p.targets.length === 1 ? 'isn’t' : 'aren’t'} on this computer, so there’s nothing to
                  push into. Set {p.targets.length === 1 ? 'its' : 'their'} location in Settings → Local
                  libraries, or edit the set.
                </div>
              )}
              {blocked === 'nothing_to_push' && p && (
                <div className={`rounded well px-3 py-2 text-xs ${p.tracks ? 'text-pink' : 'text-muted'}`}>
                  {p.tracks === 0
                    ? 'There’s nothing to push yet — add some tracks or a playlist.'
                    : `None of the ${plural(p.tracks, 'track')} can be pushed — their audio files are not on this disk.`}
                </div>
              )}
            </>
          )}

          {job && (
            <div className="space-y-3">
              <div className="text-text">{job.state === 'done' ? 'Pushed.' : job.message || 'Working…'}</div>
              <div className="h-2 overflow-hidden rounded-full bg-well">
                <div
                  className={`h-full transition-[width] duration-200 ${job.state === 'failed' ? 'bg-pink' : 'bg-accent'} ${
                    pct == null ? 'w-1/3 animate-pulse' : ''
                  }`}
                  style={pct == null ? undefined : { width: `${pct}%` }}
                />
              </div>
              {pct != null && running && job.unit === 'bytes' && (
                <div className="text-xs text-faint">{gb(job.done)} of {gb(job.total)}</div>
              )}
              {job.state === 'cancelled' && (
                <div className="text-xs text-muted">
                  Cancelled. A library already saved keeps the push; any other was left as it was.
                </div>
              )}
              {job.state === 'failed' && <div className="text-xs text-pink">{job.error}</div>}
              {job.state === 'done' &&
                results.map((r) => (
                  <div key={r.name} className="space-y-0.5 text-xs">
                    <div className="text-mint">{resultLine(r)}</div>
                    {r.kept.length > 0 && (
                      <div className="text-gold">
                        Left in {r.name}: {r.kept.slice(0, 4).join(', ')}
                        {r.kept.length > 4 && ` and ${r.kept.length - 4} more`} — {r.name} still lists
                        them in its History, Sampler or another list, so they stay. The next push tries
                        again.
                      </div>
                    )}
                  </div>
                ))}
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2 border-t border-line px-5 py-3">
          {running ? (
            <>
              <button onClick={cancel} className="btn-glass rounded-full px-3 py-1.5 text-sm text-muted hover:text-text">
                Cancel push
              </button>
              <button onClick={onClose} className="rounded-md px-3 py-1.5 text-sm text-faint hover:text-text"
                title="The push keeps running">
                Hide
              </button>
            </>
          ) : (
            <>
              <button onClick={onClose} className="btn-glass rounded-full px-3 py-1.5 text-sm text-muted hover:text-text">
                {finished ? 'Close' : 'Cancel'}
              </button>
              {!finished && (
                <button
                  disabled={starting || preview.isFetching || !p || !!blocked || p.enough_space === false}
                  onClick={start}
                  className="rounded-full btn-primary px-3 py-1.5 text-sm font-semibold disabled:opacity-40"
                >
                  {starting ? 'Starting…' : overwrite ? 'Replace' : `Push ${p && !blocked ? p.pushable : ''}`.trim()}
                </button>
              )}
            </>
          )}
        </div>
      </div>
    </div>,
    document.body,
  )
}

function TargetLine({ target, overwrite }: { target: PushTarget; overwrite: boolean }) {
  if (target.skipped) {
    return (
      <div className="rounded-md well px-3 py-2">
        <div className="flex items-baseline gap-2">
          <span className="text-faint">{target.name}</span>
          <span className="ml-auto text-xs text-faint">not on this computer — skipped</span>
        </div>
      </div>
    )
  }
  const problem = localLibraryProblem({ ...target, detected: true })
  const counts = overwrite
    ? 'replaced by this set'
    : [
        target.add && `${target.add} to add`,
        target.already && `${target.already} already there`,
        target.remove && `${target.remove} to remove`,
      ].filter(Boolean).join(' · ') || 'nothing to change'
  return (
    <div className="rounded-md well px-3 py-2">
      <div className="flex items-baseline gap-2">
        <span className="text-text">{target.name}</span>
        <span className={`ml-auto text-xs ${problem || target.running ? 'text-pink' : 'text-muted'}`}>
          {problem ?? (target.running ? 'running' : counts)}
        </span>
      </div>
      {target.path && (
        <div title={target.path} dir="rtl" className="truncate text-left font-mono text-[11px] text-faint">
          {target.path}
        </div>
      )}
    </div>
  )
}

function resultLine(r: TargetResult): string {
  if (r.replaced) return `${r.name}: replaced — ${plural(r.added, 'track')}`
  const parts = [
    r.added && `${r.added} added`,
    r.updated && `${r.updated} updated`,
    r.removed && `${r.removed} removed`,
  ].filter(Boolean)
  return `${r.name}: ${parts.length ? parts.join(', ') : 'already up to date'}`
}

function doneMessage(name: string, result: Record<string, unknown> | null): string {
  const r = result as { targets?: TargetResult[]; skipped_targets?: string[] } | null
  const targets = (r?.targets ?? []).map((t) => t.name)
  const skipped = r?.skipped_targets ?? []
  return `Pushed “${name}” into ${targets.join(' and ') || 'this computer'}` +
    (skipped.length ? ` (${skipped.join(' and ')} skipped — not on this computer)` : '')
}
