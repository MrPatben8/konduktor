import { createPortal } from 'react-dom'
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  api,
  type JobStatus,
  type PlaylistNode,
  type StemConvertOptions,
  type StemEngineStatus,
  type StemTarget,
} from '../api'
import { FolderPicker } from './FolderPicker'
import { useServerFolder } from './AddFilesDialog'
import { useCaps } from '../lib/capabilities'

/**
 * Convert to Stems — one dialog (decided, see the stem-conversion discussion
 * log): where the files go, what the collection does, a live preview, Convert.
 *
 * Every option is REMEMBERED (`stemConvert` in prefs) — unlike Add Files'
 * ask-every-time — so Replace may be the preselected mode; that is why its
 * consequence is stated in the option itself, not only at Save.
 *
 * When the engine is missing the dialog downloads it inline; closing it lets
 * the download carry on (App shows it in the status bar via `onDownloading`).
 */

interface Remembered {
  mode: 'replace' | 'destination'
  destination: string
  collection: 'repoint' | 'add'
  playlist: 'none' | 'existing' | 'new'
  playlistId: string
  playlistName: string
}

const DEFAULTS: Remembered = {
  mode: 'replace',
  destination: '~/Music/Stems',
  collection: 'repoint',
  playlist: 'new',
  playlistId: '',
  playlistName: 'Stems',
}

/** Real-time factors (seconds of audio per second of work), measured on an M3
 *  Pro for MPS and CPU; the Windows figures are estimates until measured. */
const SPEED: Record<string, number> = {
  'macos-arm64:gpu': 5.4,
  'macos-arm64:cpu': 1.2,
  'windows-x64-cuda:gpu': 8,
  'windows-x64-cuda:cpu': 0.8,
  'windows-x64-cpu:cpu': 0.8,
}

export function size(bytes: number): string {
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`
  if (bytes >= 1e6) return `${Math.round(bytes / 1e6)} MB`
  return `${Math.max(1, Math.round(bytes / 1e3))} kB`
}

function duration(seconds: number): string {
  const m = Math.round(seconds / 60)
  if (m < 1) return 'under a minute'
  if (m < 90) return `${m} min`
  return `${(seconds / 3600).toFixed(1)} h`
}

function flatten(nodes: PlaylistNode[], path = ''): { id: string; label: string }[] {
  return nodes.flatMap((n) => [
    ...(n.can_add_tracks && n.kind === 'playlist' ? [{ id: n.id, label: path + n.name }] : []),
    ...flatten(n.children ?? [], `${path}${n.name} / `),
  ])
}

interface Props {
  trackIds: string[]
  onClose: () => void
  /** The conversion job started — App tracks it in the status bar. */
  onStarted: (job: JobStatus) => void
  /** An engine download started — App shows it in the status bar. */
  onDownloading: (job: JobStatus) => void
  onError: (msg: string) => void
}

export function ConvertStemsDialog({ trackIds, onClose, onStarted, onDownloading, onError }: Props) {
  const qc = useQueryClient()
  const prefs = useQuery({ queryKey: ['prefs'], queryFn: api.getPrefs })
  const [opts, setOpts] = useState<Remembered | null>(null)
  const [browsing, setBrowsing] = useState(false)
  const [showSkipped, setShowSkipped] = useState(false)
  const [starting, setStarting] = useState(false)
  const [target, setTarget] = useState<StemTarget | null>(null)
  // A remote library: its stem files are written ON THE SERVER, so the
  // destination is one of its folders (remembered per remote), never the
  // local one `stemConvert` remembers for this computer's libraries.
  const remote = useCaps().tracks.audio_destination === 'library'
  const server = useServerFolder(remote, 'remoteStemFolders', 'Stems')
  const [serverDest, setServerDest] = useState<string | null>(null)

  useEffect(() => {
    if (opts || !prefs.isSuccess) return
    const saved = (prefs.data as { stemConvert?: Partial<Remembered> }).stemConvert
    setOpts({ ...DEFAULTS, ...(saved ?? {}) })
  }, [prefs.isSuccess, prefs.data, opts])

  const engine = useQuery({ queryKey: ['stem-engine'], queryFn: api.stemEngine })
  const installJobId = engine.data?.install_job ?? null
  const install = useQuery({
    queryKey: ['job', installJobId],
    queryFn: () => api.job(installJobId!),
    enabled: !!installJobId,
    refetchInterval: (q) => (q.state.data && q.state.data.state !== 'running' ? false : 400),
  })
  useEffect(() => {
    if (install.data && install.data.state !== 'running') {
      if (install.data.state === 'failed') onError(install.data.error || 'The download failed')
      qc.invalidateQueries({ queryKey: ['stem-engine'] })
    }
  }, [install.data, qc, onError])

  const playlists = useQuery({ queryKey: ['playlists'], queryFn: api.playlists })
  const choices = useMemo(() => flatten(playlists.data ?? []), [playlists.data])

  const destination = remote ? (serverDest ?? server.folder) : (opts?.destination ?? '')
  const body: StemConvertOptions | null = opts && {
    mode: opts.mode,
    destination: opts.mode === 'destination' ? destination : null,
    collection: opts.mode === 'replace' ? 'repoint' : opts.collection,
    playlist_id:
      opts.mode === 'destination' && opts.collection === 'add' && opts.playlist === 'existing'
        ? opts.playlistId || null
        : null,
    new_playlist:
      opts.mode === 'destination' && opts.collection === 'add' && opts.playlist === 'new'
        ? opts.playlistName.trim() || 'Stems'
        : null,
  }
  const preview = useQuery({
    queryKey: ['stem-preview', trackIds, body],
    queryFn: () => api.stemPreview(trackIds, body!),
    enabled: !!body,
    staleTime: 0,
    gcTime: 0,
  })

  const set = (patch: Partial<Remembered>) => setOpts((o) => (o ? { ...o, ...patch } : o))
  const st: StemEngineStatus | undefined = engine.data
  const ready = !!st?.installed && !!st?.weights.installed
  const downloading = install.data?.state === 'running'
  const offered = st?.offered_targets ?? []
  const chosenTarget = target ?? (offered.includes('windows-x64-cuda') ? 'windows-x64-cuda' : offered[0])
  const needBytes =
    (st?.installed ? 0 : (chosenTarget && st?.download_sizes[chosenTarget]) || 0) +
    (st?.weights.installed ? 0 : st?.weights.size ?? 0)

  const plan = preview.data
  const n = plan?.convert.length ?? 0
  const allReused = !!plan && plan.convert.every((c) => c.reuse)
  const device = (st?.device ?? 'auto') === 'cpu' ? 'cpu' : 'gpu'
  const rtf = st?.installed ? SPEED[`${st.installed.target}:${device}`] ?? SPEED[`${st.installed.target}:cpu`] : null
  const canConvert = !!plan && n > 0 && !plan.blocked && (ready || allReused) && !starting
  // With nothing to convert the reasons ARE the answer: show them without a click.
  const skippedOpen = showSkipped || (!!plan && n === 0 && plan.skipped.length > 0)
  const whyNot = !plan
    ? undefined
    : n === 0
      ? 'Nothing to convert — see why each track is skipped'
      : plan.blocked
        ? plan.blocked
        : !ready && !allReused
          ? 'The stem engine is not installed yet'
          : undefined

  const download = async () => {
    try {
      const job = await api.stemEngineInstall(chosenTarget ?? null)
      onDownloading(job)
      qc.invalidateQueries({ queryKey: ['stem-engine'] })
    } catch (e) {
      onError((e as Error).message)
    }
  }

  const convert = async () => {
    if (!body || !opts) return
    setStarting(true)
    try {
      api.patchPrefs({ stemConvert: opts }).then((p) => qc.setQueryData(['prefs'], p)).catch(() => {})
      onStarted(await api.stemConvert(trackIds, body))
      onClose()
    } catch (e) {
      onError((e as Error).message)
      setStarting(false)
    }
  }

  const radio = (
    name: string,
    checked: boolean,
    onPick: () => void,
    label: string,
    detail: ReactNode,
    disabled = false,
  ) => (
    <label
      className={`flex gap-3 rounded-lg px-3 py-2 ${disabled ? 'opacity-40' : 'cursor-pointer'} ${
        checked ? 'is-selected' : disabled ? '' : 'hover:bg-ink-850'
      }`}
    >
      <input
        type="radio"
        name={name}
        checked={checked}
        disabled={disabled}
        onChange={onPick}
        className="mt-0.5 accent-accent"
      />
      <span className="min-w-0 flex-1">
        <span className="block text-text">{label}</span>
        <span className="block text-xs text-faint">{detail}</span>
      </span>
    </label>
  )

  const count = trackIds.length
  return createPortal(
    <>
      {browsing && opts && (
        <FolderPicker
          value={destination}
          listing={remote ? 'library' : 'host'}
          onChange={(path) => {
            if (remote) {
              setServerDest(path)
              server.remember(path)
            } else set({ destination: path })
          }}
          onClose={() => setBrowsing(false)}
        />
      )}
      <div
        aria-modal="true"
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-[3px]"
        onKeyDown={(e) => e.key === 'Escape' && !starting && onClose()}
      >
        <div className="flex max-h-[90vh] w-full max-w-lg flex-col overflow-hidden glass-overlay">
          <div className="border-b border-line px-5 py-3">
            <h2 className="text-sm font-semibold text-text">
              Convert {count} track{count === 1 ? '' : 's'} to stems
            </h2>
          </div>

          {!opts ? (
            <div className="px-5 py-6 text-sm text-faint">Loading…</div>
          ) : (
            <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-4 text-sm">
              <section className="space-y-1">
                <div className="px-1 text-[11px] font-semibold uppercase tracking-wider text-faint">Where</div>
                {radio(
                  'stem-mode',
                  opts.mode === 'replace',
                  () => set({ mode: 'replace' }),
                  'Replace the originals',
                  <>
                    <span className="block">Track.mp3 becomes Track.stem.m4a, in the same folder.</span>
                    {opts.mode === 'replace' && (
                      <span className="mt-1 block text-gold">
                        Saving deletes the {n || ''} original file{n === 1 ? '' : 's'}
                        {plan?.original_bytes ? ` (${size(plan.original_bytes)})` : ''} for good — and any
                        other app or older collection version using them loses them. Discard keeps them.
                      </span>
                    )}
                  </>,
                )}
                {radio(
                  'stem-mode',
                  opts.mode === 'destination',
                  () => set({ mode: 'destination' }),
                  'Save to a folder',
                  <span className="flex items-center gap-2">
                    <span className="min-w-0 truncate font-mono" dir="rtl" title={destination}>
                      {`‎${destination}‎`}
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
              </section>

              {opts.mode === 'destination' && (
                <section className="space-y-1">
                  <div className="px-1 text-[11px] font-semibold uppercase tracking-wider text-faint">
                    In the collection
                  </div>
                  {radio(
                    'stem-collection',
                    opts.collection === 'repoint',
                    () => set({ collection: 'repoint' }),
                    'Point each track at its stem file',
                    'The track keeps its playlists, cues and history. The original file stays where it is.',
                  )}
                  {radio(
                    'stem-collection',
                    opts.collection === 'add',
                    () => set({ collection: 'add' }),
                    'Add the stem files as new tracks',
                    'With the same cues, grid and tags. The original tracks stay as they are.',
                  )}
                  {opts.collection === 'add' && (
                    <div className="flex items-center gap-2 pl-10 text-xs">
                      <select
                        value={opts.playlist === 'existing' ? `pl:${opts.playlistId}` : opts.playlist}
                        onChange={(e) => {
                          const v = e.target.value
                          if (v.startsWith('pl:')) set({ playlist: 'existing', playlistId: v.slice(3) })
                          else set({ playlist: v as Remembered['playlist'] })
                        }}
                        className="well rounded-md px-2 py-1 text-text"
                      >
                        <option value="none">No playlist</option>
                        <option value="new">New playlist…</option>
                        {choices.map((c) => (
                          <option key={c.id} value={`pl:${c.id}`}>
                            {c.label}
                          </option>
                        ))}
                      </select>
                      {opts.playlist === 'new' && (
                        <input
                          value={opts.playlistName}
                          onChange={(e) => set({ playlistName: e.target.value })}
                          className="well min-w-0 flex-1 rounded-md px-2 py-1 text-text"
                          placeholder="Playlist name"
                        />
                      )}
                    </div>
                  )}
                </section>
              )}

              {!ready && st && (
                <section className="rounded-lg well px-3 py-3 text-xs">
                  {!st.supported ? (
                    <div className="text-pink">There is no stem engine for this computer.</div>
                  ) : downloading && install.data ? (
                    <div className="space-y-2">
                      <div className="text-text">{install.data.message || 'Downloading…'}</div>
                      <div className="h-2 overflow-hidden rounded-full bg-ink-800">
                        <div
                          className="h-full bg-accent transition-[width] duration-200"
                          style={{ width: `${install.data.total ? (install.data.done / install.data.total) * 100 : 0}%` }}
                        />
                      </div>
                      <div className="text-faint">
                        {size(install.data.done)} of {size(install.data.total || needBytes)} · closing this keeps it
                        going
                      </div>
                    </div>
                  ) : !st.manifest_available && !st.installed ? (
                    <div className="text-muted">
                      The stem engine is a one-time download, and it cannot be reached right now. Check the internet
                      connection — or install it from a file in Settings → Stems.
                    </div>
                  ) : (
                    <div className="space-y-2">
                      <div className="text-text">
                        The stem engine is a one-time download ({size(needBytes)}).
                      </div>
                      {offered.length > 1 && !st.installed && (
                        <div className="space-y-1">
                          {offered.map((t) => (
                            <label key={t} className="flex items-center gap-2 text-muted">
                              <input
                                type="radio"
                                name="stem-target"
                                checked={chosenTarget === t}
                                onChange={() => setTarget(t)}
                                className="accent-accent"
                              />
                              {t === 'windows-x64-cuda'
                                ? `Use the ${st.nvidia?.name ?? 'NVIDIA'} GPU (${size(st.download_sizes[t] ?? 0)}) — much faster`
                                : `Processor only (${size(st.download_sizes[t] ?? 0)})`}
                            </label>
                          ))}
                        </div>
                      )}
                      <button onClick={download} className="btn-primary rounded-full px-3 py-1 text-xs font-semibold">
                        Download
                      </button>
                    </div>
                  )}
                </section>
              )}

              {preview.isError && <div className="text-pink">{(preview.error as Error).message}</div>}
              {plan && (
                <section className="space-y-1 text-xs">
                  <div className="text-text">
                    {n} to convert
                    {plan.skipped.length > 0 && (
                      <>
                        {' · '}
                        <button onClick={() => setShowSkipped((v) => !v)} className="text-muted hover:text-text">
                          {plan.skipped.length} skipped {skippedOpen ? '▾' : '▸'}
                        </button>
                      </>
                    )}
                  </div>
                  {skippedOpen && (
                    <ul className="max-h-32 space-y-0.5 overflow-y-auto rounded-md well px-3 py-2">
                      {plan.skipped.map((s) => (
                        <li key={s.track_id} className="flex gap-2">
                          <span className="min-w-0 flex-1 truncate text-muted" title={s.title}>
                            {s.title}
                          </span>
                          <span className="shrink-0 text-faint">{s.reason}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                  {n > 0 && (
                    <div className="text-faint">
                      {plan.seconds > 0 && rtf ? `About ${duration(plan.seconds / rtf)} on this computer · ` : ''}
                      {plan.seconds === 0 && allReused ? 'Already converted earlier — nothing to separate · ' : ''}
                      {size(plan.bytes)} of stem files
                    </div>
                  )}
                  {/* A remote library: the sources come down and the stem
                      files go up — said, with the time it adds. */}
                  {n > 0 && plan.transfer && plan.transfer.download + plan.transfer.upload > 0 && (
                    <div className="text-faint">
                      {size(plan.transfer.download)} to download and {size(plan.transfer.upload)} to upload
                      {' · about '}
                      {duration((plan.transfer.download + plan.transfer.upload) / plan.transfer.speed)}
                      {plan.transfer.measured ? '' : ' (estimated)'} more
                    </div>
                  )}
                  {n > 0 && plan.transfer && (
                    <ul className="space-y-0.5 text-faint">
                      {plan.space.map((sp) => (
                        <li key={`${sp.volume}-${sp.folder}`}>
                          {sp.volume}: {size(sp.bytes)} needed
                          {sp.free != null ? `, ${size(sp.free)} free` : ''}
                        </li>
                      ))}
                    </ul>
                  )}
                  {plan.blocked && <div className="text-pink">{plan.blocked}</div>}
                </section>
              )}
            </div>
          )}

          <div className="flex justify-end gap-2 border-t border-line px-5 py-3">
            <button
              onClick={onClose}
              disabled={starting}
              className="btn-glass rounded-full px-3 py-1.5 text-sm text-muted hover:text-text"
            >
              {downloading ? 'Close' : 'Cancel'}
            </button>
            <button
              disabled={!canConvert}
              onClick={convert}
              title={whyNot}
              className="btn-primary rounded-full px-3 py-1.5 text-sm font-semibold disabled:opacity-40"
            >
              {starting ? 'Starting…' : `Convert${n ? ` ${n}` : ''}`}
            </button>
          </div>
        </div>
      </div>
    </>,
    document.body,
  )
}
