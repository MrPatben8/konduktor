import { createPortal } from 'react-dom'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type FolderAddPreview, type FolderAddRequest, type JobStatus } from '../api'
import { FolderPicker } from './FolderPicker'

/**
 * Add browsed files to the collection — and, optionally, to a playlist or export.
 *
 * Copy vs. leave-in-place is ASKED every time, with nothing preselected: the
 * right answer depends on the drive (a stick is about to be unplugged) and on
 * how the DJ keeps their music, and a remembered default would quietly apply
 * last week's answer to a different drive.
 *
 * A playlist or export target still needs the files in the collection first —
 * both hold collection track ids — so the dialog says so rather than doing it
 * silently. Files the collection already holds are not added again, but still
 * go into the target.
 */

const POLL_MS = 300

function gb(bytes: number): string {
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(0)} MB`
  return `${(bytes / 1e3).toFixed(0)} kB`
}

export interface AddTarget {
  kind: 'playlist' | 'export'
  id: string
  name: string
}

interface Props {
  trackIds: string[]
  target: AddTarget | null
  onClose: () => void
  onDone: (msg: string) => void
  onError: (msg: string) => void
}

export function AddFilesDialog({ trackIds, target, onClose, onDone, onError }: Props) {
  const qc = useQueryClient()
  const [mode, setMode] = useState<'copy' | 'reference' | null>(null)
  const [folder, setFolder] = useState('')
  const [browsing, setBrowsing] = useState(false)
  const [job, setJob] = useState<JobStatus | null>(null)
  const [starting, setStarting] = useState(false)
  const pollRef = useRef<number | null>(null)

  // The same import folder as a device import: one place your copied music goes.
  const prefs = useQuery({ queryKey: ['prefs'], queryFn: api.getPrefs })
  const prefsLoaded = prefs.isSuccess
  useEffect(() => {
    if (!prefsLoaded || folder) return
    const p = prefs.data as { importFolder?: string }
    setFolder(p?.importFolder || '~/Music/Konduktor Imports')
  }, [prefsLoaded, prefs.data, folder])

  const body: FolderAddRequest = {
    track_ids: trackIds,
    // The preview needs A mode to size anything; "reference" costs nothing to
    // ask about and is replaced as soon as the user picks.
    mode: mode ?? 'reference',
    destination: mode === 'copy' ? folder : null,
    playlist_id: target?.kind === 'playlist' ? target.id : null,
    export_id: target?.kind === 'export' ? target.id : null,
  }

  const preview = useQuery<FolderAddPreview>({
    queryKey: ['folder-add-preview', trackIds, mode, folder, target?.id],
    queryFn: () => api.folderAddPreview(body),
    enabled: !job && (mode !== 'copy' || !!folder),
    staleTime: 0,
    gcTime: 0,
  })

  useEffect(() => {
    if (!job || job.state !== 'running') return
    pollRef.current = window.setInterval(async () => {
      try {
        const next = await api.job(job.id)
        setJob(next)
        if (next.state === 'running') return
        if (pollRef.current) window.clearInterval(pollRef.current)
        if (next.state === 'done') {
          const r = next.result as { tracks?: number; track_ids?: string[] } | null
          qc.invalidateQueries({ queryKey: ['tracks'] })
          qc.invalidateQueries({ queryKey: ['playlists'] })
          qc.invalidateQueries({ queryKey: ['playlist'] })
          qc.invalidateQueries({ queryKey: ['collection'] })
          qc.invalidateQueries({ queryKey: ['state'] })
          qc.invalidateQueries({ queryKey: ['facets'] })
          qc.invalidateQueries({ queryKey: ['folder-tracks'] })
          if (target?.kind === 'export') {
            qc.invalidateQueries({ queryKey: ['exports'] })
            qc.invalidateQueries({ queryKey: ['export-contents', target.id] })
            qc.invalidateQueries({ queryKey: ['export-tracks', target.id] })
            qc.invalidateQueries({ queryKey: ['export-loose', target.id] })
          }
          const added = r?.tracks ?? 0
          const total = r?.track_ids?.length ?? added
          onDone(
            target
              ? `Added ${total} track${total === 1 ? '' : 's'} to ${target.name}` +
                  (added ? ` (${added} new to your collection)` : '')
              : `Added ${added} track${added === 1 ? '' : 's'} to your collection`,
          )
        } else if (next.state === 'failed') {
          onError(next.error || 'Adding failed')
        }
      } catch (e) {
        onError((e as Error).message)
        if (pollRef.current) window.clearInterval(pollRef.current)
      }
    }, POLL_MS)
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current)
    }
  }, [job, qc, onDone, onError, target])

  const start = async () => {
    setStarting(true)
    try {
      setJob(await api.folderAdd(body))
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
  const n = trackIds.length
  const newCount = p?.importable ?? 0
  const canAdd =
    !starting && mode !== null && !!p && (newCount > 0 || (!!target && p.existing.length > 0)) &&
    p.enough_space !== false

  const title = target
    ? `Add ${n} track${n === 1 ? '' : 's'} to ${target.name}`
    : `Add ${n} track${n === 1 ? '' : 's'} to your collection`

  const option = (value: 'copy' | 'reference', label: string, detail: ReactNode) => (
    <label
      className={`flex cursor-pointer gap-3 rounded-lg px-3 py-2 ${
        mode === value ? 'is-selected' : 'hover:bg-ink-850'
      }`}
    >
      <input
        type="radio"
        name="add-mode"
        checked={mode === value}
        onChange={() => setMode(value)}
        className="mt-0.5 accent-accent"
      />
      <span className="min-w-0 flex-1">
        <span className="block text-text">{label}</span>
        <span className="block text-xs text-faint">{detail}</span>
      </span>
    </label>
  )

  return createPortal(
    <>
      {browsing && (
        <FolderPicker
          value={folder}
          onChange={(path) => {
            setFolder(path)
            api.patchPrefs({ importFolder: path }).catch(() => {})
          }}
          onClose={() => setBrowsing(false)}
        />
      )}
      <div
        aria-modal="true"
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-[3px]"
      >
        <div className="w-full max-w-lg glass-overlay">
          <div className="border-b border-line px-5 py-3">
            <h2 className="text-sm font-semibold text-text">{title}</h2>
          </div>

          <div className="space-y-4 px-5 py-4 text-sm">
            {!job && (
              <>
                {target && (
                  <div className="text-xs text-muted">
                    {target.kind === 'playlist' ? 'Playlists' : 'Exports'} hold tracks from your
                    collection, so these are added to the collection first.
                  </div>
                )}

                <div className="space-y-1">
                  {option(
                    'copy',
                    'Copy into your import folder',
                    <span className="flex items-center gap-2">
                      <span className="min-w-0 truncate font-mono" dir="rtl" title={folder}>
                        {/* LRM marks: in an rtl box (for left-truncation) a
                            leading "~" would otherwise be moved to the end. */}
                        {folder ? `\u200E${folder}\u200E` : 'Loading…'}
                      </span>
                      <button
                        onClick={(e) => {
                          e.preventDefault()
                          setBrowsing(true)
                        }}
                        className="shrink-0 text-accent hover:underline"
                      >
                        Change…
                      </button>
                    </span>,
                  )}
                  {option(
                    'reference',
                    'Leave them where they are',
                    'Your collection points at these files. Nothing is copied.',
                  )}
                </div>

                {mode === 'reference' && p?.removable && (
                  <div className="rounded well px-3 py-2 text-xs text-gold">
                    These files are on an external drive. They will go missing from your
                    collection whenever the drive is unplugged.
                  </div>
                )}

                {preview.isError && (
                  <div className="text-pink">{(preview.error as Error).message}</div>
                )}

                {mode && p && (
                  <div className="space-y-2">
                    <div className="text-text">
                      {newCount} new to your collection
                      {mode === 'copy' && (
                        <span className="text-muted"> · {gb(p.total_bytes)} to copy</span>
                      )}
                    </div>
                    {mode === 'copy' && p.free_bytes != null && (
                      <div className={p.enough_space === false ? 'text-pink' : 'text-faint'}>
                        {gb(p.free_bytes)} free
                        {p.enough_space === false && ' — not enough space'}
                      </div>
                    )}
                    {p.existing.length > 0 && (
                      <div className="rounded well px-3 py-2 text-xs text-muted">
                        {p.existing.length} {p.existing.length === 1 ? 'is' : 'are'} already in
                        your collection and will not be added again
                        {target ? ` — but will still go into ${target.name}` : ''}.
                      </div>
                    )}
                    {p.missing.length > 0 && (
                      <div className="rounded well px-3 py-2 text-xs text-pink">
                        {p.missing.length} file{p.missing.length === 1 ? ' is' : 's are'} no longer
                        there and will be skipped.
                      </div>
                    )}
                    {mode === 'copy' && p.duplicates.length > 0 && (
                      <div className="rounded well px-3 py-2 text-xs text-gold">
                        {p.duplicates.length} of these look like tracks your collection already
                        has. The copies will be added as new entries.
                      </div>
                    )}
                  </div>
                )}
              </>
            )}

            {job && (
              <div className="space-y-3">
                <div className="text-text">{job.message || 'Working…'}</div>
                <div className="h-2 overflow-hidden rounded-full bg-well">
                  <div
                    className={`h-full transition-[width] duration-200 ${
                      job.state === 'failed' ? 'bg-pink' : 'bg-accent'
                    } ${pct == null ? 'w-1/3 animate-pulse' : ''}`}
                    style={pct == null ? undefined : { width: `${pct}%` }}
                  />
                </div>
                {job.state === 'cancelled' && (
                  <div className="text-xs text-muted">
                    Cancelled. Nothing was added, and any copied files were removed.
                  </div>
                )}
                {job.state === 'failed' && <div className="text-xs text-pink">{job.error}</div>}
                {job.state === 'done' && <div className="text-xs text-mint">Done.</div>}
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
                  Cancel
                </button>
                <button
                  onClick={onClose}
                  className="rounded-md px-3 py-1.5 text-sm text-faint hover:text-text"
                  title="It keeps running"
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
                    disabled={!canAdd}
                    onClick={start}
                    title={mode === null ? 'Choose whether to copy the files first' : undefined}
                    className="rounded-full btn-primary px-3 py-1.5 text-sm font-semibold disabled:opacity-40"
                  >
                    {starting ? 'Starting…' : 'Add'}
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
