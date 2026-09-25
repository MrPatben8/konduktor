import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type ImportPreview, type JobStatus } from '../api'
import { FolderPicker } from './FolderPicker'

/**
 * Import a browsed device into the loaded collection.
 *
 * Two phases in one dialog, because they are one decision: a confirm step that
 * says what is about to happen, and a progress step that shows it happening.
 *
 * The preview is fetched fresh every time this opens and whenever the
 * destination changes. It is never cached: a stick can be re-exported between
 * looking and importing, and a stale preview is worse than none.
 */

const POLL_MS = 300

function gb(bytes: number): string {
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(0)} MB`
  return `${(bytes / 1e3).toFixed(0)} kB`
}

interface Props {
  /** Playlist to import, or null for the whole device. */
  playlistId: string | null
  deviceLabel: string
  onClose: () => void
  onDone: (msg: string) => void
  onError: (msg: string) => void
}

export function ImportDialog({ playlistId, deviceLabel, onClose, onDone, onError }: Props) {
  const qc = useQueryClient()
  // The BASE folder, and whether to put this device's audio in its own
  // subfolder of it. Kept apart rather than as one path because they are two
  // different decisions: where your music lives, and how one import is filed
  // inside it. Joining them for display would make the toggle look like it was
  // editing the folder you picked.
  const [baseFolder, setBaseFolder] = useState('')
  const [useSubfolder, setUseSubfolder] = useState(true)
  const [browsing, setBrowsing] = useState(false)
  const [job, setJob] = useState<JobStatus | null>(null)
  const [starting, setStarting] = useState(false)
  const pollRef = useRef<number | null>(null)

  // Both come from prefs, so the choice survives the dialog closing. `~` is
  // expanded server-side, so the default needs no knowledge of the home path.
  const prefs = useQuery({ queryKey: ['prefs'], queryFn: api.getPrefs })
  const prefsLoaded = prefs.isSuccess
  useEffect(() => {
    if (!prefsLoaded || baseFolder) return
    const p = prefs.data as { importFolder?: string; importSubfolder?: boolean }
    setBaseFolder(p?.importFolder || '~/Music/Konduktor Imports')
    if (typeof p?.importSubfolder === 'boolean') setUseSubfolder(p.importSubfolder)
  }, [prefsLoaded, prefs.data, baseFolder])

  const remember = (patch: Record<string, unknown>) => {
    api.patchPrefs(patch).catch(() => {
      /* best-effort: a failed pref write must not block an import */
    })
  }

  const destination = useSubfolder && baseFolder ? `${baseFolder}/${deviceLabel}` : baseFolder

  const body = {
    destination,
    playlist_ids: playlistId ? [playlistId] : [],
    track_ids: [],
    folder_name: deviceLabel,
  }

  const preview = useQuery<ImportPreview>({
    queryKey: ['import-preview', destination, playlistId],
    queryFn: () => api.importPreview(body),
    enabled: !!destination && !job,
    staleTime: 0,
    gcTime: 0,
  })

  // Poll while the job runs. Cleared on unmount so closing mid-import does not
  // leave a timer firing at a dead component — the import itself keeps going,
  // which is the point of it being a job.
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
            // The collection changed underneath every cached view of it.
            qc.invalidateQueries({ queryKey: ['tracks'] })
            qc.invalidateQueries({ queryKey: ['playlists'] })
            qc.invalidateQueries({ queryKey: ['collection'] })
            qc.invalidateQueries({ queryKey: ['state'] })
            qc.invalidateQueries({ queryKey: ['facets'] })
            onDone(
              `Imported ${r?.tracks ?? 0} track${r?.tracks === 1 ? '' : 's'}` +
                (r?.playlists ? ` and ${r.playlists} playlist${r.playlists === 1 ? '' : 's'}` : ''),
            )
          } else if (next.state === 'failed') {
            onError(next.error || 'Import failed')
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
  }, [job, qc, onDone, onError])

  const start = async () => {
    setStarting(true)
    try {
      setJob(await api.startImport(body))
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

  return (
    <>
    {browsing && (
      <FolderPicker
        value={baseFolder}
        onChange={(path) => {
          setBaseFolder(path)
          remember({ importFolder: path })
        }}
        onClose={() => setBrowsing(false)}
      />
    )}
    <div aria-modal="true" className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="w-full max-w-lg rounded-lg border border-line bg-ink-900 shadow-xl">
        <div className="border-b border-line px-5 py-3">
          <h2 className="text-sm font-semibold text-text">
            Import from {deviceLabel}
            {playlistId ? '' : ' — everything'}
          </h2>
        </div>

        <div className="space-y-4 px-5 py-4 text-sm">
          {!job && (
            <>
              <div>
                <span className="mb-1 block text-xs text-muted">Copy audio into</span>
                <div className="flex items-center gap-2">
                  <span
                    title={baseFolder}
                    className="min-w-0 flex-1 truncate rounded-md border border-line bg-ink-950 px-2 py-1.5 font-mono text-xs text-text"
                    dir="rtl"
                  >
                    {baseFolder || 'Loading…'}
                  </span>
                  <button
                    onClick={() => setBrowsing(true)}
                    className="shrink-0 rounded-md border border-line px-3 py-1.5 text-sm text-muted hover:text-text"
                  >
                    Browse…
                  </button>
                </div>

                <label className="mt-2 flex cursor-pointer items-center gap-2 text-xs text-muted">
                  <input
                    type="checkbox"
                    checked={useSubfolder}
                    onChange={(e) => {
                      setUseSubfolder(e.target.checked)
                      remember({ importSubfolder: e.target.checked })
                    }}
                    className="accent-accent"
                  />
                  Put them in a “{deviceLabel}” subfolder
                </label>

                {/* The resolved path, always visible: the toggle changes where
                    files actually land, and that should never have to be
                    inferred from a checkbox. */}
                <div className="mt-1 truncate font-mono text-[11px] text-faint" dir="rtl">
                  {destination}
                </div>
              </div>

              {preview.isLoading && <div className="text-faint">Checking…</div>}
              {preview.isError && (
                <div className="text-pink">{(preview.error as Error).message}</div>
              )}

              {p && (
                <div className="space-y-2">
                  <div className="text-text">
                    {p.importable} track{p.importable === 1 ? '' : 's'}
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

                  {/* Reported and then skipped, rather than failing the whole
                      import halfway through a multi-minute copy. */}
                  {p.missing.length > 0 && (
                    <div className="rounded border border-line bg-ink-950 px-3 py-2 text-xs text-pink">
                      {p.missing.length} track{p.missing.length === 1 ? '' : 's'} will be skipped —
                      the audio file is not on the drive:
                      <div className="mt-1 text-faint">{p.missing.slice(0, 4).join(', ')}</div>
                    </div>
                  )}

                  {/* A warning, not a block: everything is imported as a new
                      entry, so a re-import legitimately duplicates. */}
                  {p.duplicates.length > 0 && (
                    <div className="rounded border border-line bg-ink-950 px-3 py-2 text-xs text-gold">
                      {p.duplicates.length} of these look like tracks your collection already has.
                      They will be added again as new entries.
                    </div>
                  )}

                  <div className="text-xs text-faint">
                    Playlists land in a folder called “{deviceLabel}”.
                  </div>
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
                  } ${pct == null ? 'animate-pulse w-1/3' : ''}`}
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
                  Cancelled. Nothing was added, and the copied files were removed.
                </div>
              )}
              {job.state === 'failed' && <div className="text-xs text-pink">{job.error}</div>}
              {job.state === 'done' && (
                <div className="text-xs text-mint">Done — the tracks are in your collection.</div>
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
                Cancel import
              </button>
              <button
                onClick={onClose}
                className="rounded-md px-3 py-1.5 text-sm text-faint hover:text-text"
                title="The import keeps running"
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
                    starting || !p || p.importable === 0 || p.enough_space === false
                  }
                  onClick={start}
                  className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-ink-950 hover:brightness-110 disabled:opacity-40"
                >
                  {starting ? 'Starting…' : `Import ${p?.importable ?? ''}`.trim()}
                </button>
              )}
            </>
          )}
        </div>
      </div>
    </div>
    </>
  )
}
