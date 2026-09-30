import { createPortal } from 'react-dom'
import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type StemTarget } from '../api'
import { askConfirm } from '../lib/confirm'
import { FolderPicker } from './FolderPicker'
import { size } from './ConvertStemsDialog'

/**
 * Settings → Stems (decided contents): the engine (status, download, install
 * from files for offline machines, remove), the compute device, the conversion
 * defaults the Convert dialog remembers, and the originals awaiting Save.
 */

interface Props {
  onClose: () => void
}

export function StemsSettings({ onClose }: Props) {
  const qc = useQueryClient()
  const engine = useQuery({ queryKey: ['stem-engine'], queryFn: api.stemEngine })
  const state = useQuery({ queryKey: ['state'], queryFn: api.state, retry: false })
  const prefs = useQuery({ queryKey: ['prefs'], queryFn: api.getPrefs })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [picking, setPicking] = useState<null | 'engine' | 'weights'>(null)

  const st = engine.data
  const installJob = st?.install_job ?? null
  const install = useQuery({
    queryKey: ['job', installJob],
    queryFn: () => api.job(installJob!),
    enabled: !!installJob,
    refetchInterval: (q) => (q.state.data && q.state.data.state !== 'running' ? false : 500),
  })
  useEffect(() => {
    if (install.data && install.data.state !== 'running') {
      if (install.data.state === 'failed') setError(install.data.error || 'The download failed')
      qc.invalidateQueries({ queryKey: ['stem-engine'] })
    }
  }, [install.data, qc])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && !picking && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, picking])

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      qc.invalidateQueries({ queryKey: ['stem-engine'] })
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const device = st?.device ?? 'auto'
  const setDevice = (d: string) =>
    api.patchPrefs({ stemDevice: d }).then((p) => {
      qc.setQueryData(['prefs'], p)
      qc.invalidateQueries({ queryKey: ['stem-engine'] })
    })
  const defaults = (prefs.data as { stemConvert?: { mode?: string; destination?: string; collection?: string } })
    ?.stemConvert
  const pending = state.data?.pending_stems
  const downloading = install.data?.state === 'running'
  const targetLabel = (t: StemTarget) =>
    t === 'macos-arm64' ? 'Apple silicon (GPU)' : t === 'windows-x64-cuda' ? 'NVIDIA GPU' : 'processor only'

  return createPortal(
    <>
      {picking && (
        <FolderPicker
          value=""
          onChange={(path) => void act(() => api.stemSideload(path, picking))}
          onClose={() => setPicking(null)}
        />
      )}
      <div
        aria-modal="true"
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-6 backdrop-blur-[3px]"
        onClick={onClose}
      >
        <div
          className="flex max-h-[85vh] w-full max-w-lg flex-col overflow-hidden glass-overlay"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="flex items-center justify-between border-b border-line px-5 py-3">
            <h2 className="text-sm font-semibold text-text">Stems</h2>
            <button onClick={onClose} className="rounded px-1.5 text-faint hover:text-text" aria-label="Close">
              ×
            </button>
          </div>
          <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-4 text-sm">
            <section className="space-y-2">
              <h3 className="text-[11px] font-semibold uppercase tracking-wider text-faint">Stem engine</h3>
              {!st ? (
                <div className="text-faint">Loading…</div>
              ) : !st.supported ? (
                <div className="text-muted">There is no stem engine for this computer.</div>
              ) : (
                <>
                  <div className="text-muted">
                    {st.installed
                      ? `Installed — version ${st.installed.version}, ${targetLabel(st.installed.target)}${
                          st.installed.size ? `, ${size(st.installed.size)}` : ''
                        }`
                      : 'Not installed'}
                    {' · '}
                    {st.weights.installed ? `model installed (${size(st.weights.size)})` : 'model not installed'}
                  </div>
                  {downloading && install.data && (
                    <div className="space-y-1">
                      <div className="h-2 overflow-hidden rounded-full well">
                        <div
                          className="h-full bg-accent transition-[width] duration-200"
                          style={{ width: `${install.data.total ? (install.data.done / install.data.total) * 100 : 0}%` }}
                        />
                      </div>
                      <div className="text-xs text-faint">
                        {install.data.message} {size(install.data.done)} of {size(install.data.total)}
                      </div>
                    </div>
                  )}
                  <div className="flex flex-wrap gap-2">
                    {!(st.installed && st.weights.installed) && !downloading &&
                      st.offered_targets.map((t) => (
                        <button
                          key={t}
                          disabled={busy || !st.manifest_available}
                          onClick={() => void act(() => api.stemEngineInstall(t))}
                          className="btn-primary rounded-full px-3 py-1 text-xs font-semibold disabled:opacity-40"
                        >
                          Download ({targetLabel(t)},{' '}
                          {size((st.installed ? 0 : st.download_sizes[t] ?? 0) + (st.weights.installed ? 0 : st.weights.size))})
                        </button>
                      ))}
                    <button
                      disabled={busy || downloading}
                      onClick={() => setPicking('engine')}
                      className="btn-glass rounded-full px-3 py-1 text-xs text-muted hover:text-text disabled:opacity-40"
                      title="A folder holding the engine file(s) downloaded from the release page"
                    >
                      Install engine from a folder…
                    </button>
                    <button
                      disabled={busy || downloading}
                      onClick={() => setPicking('weights')}
                      className="btn-glass rounded-full px-3 py-1 text-xs text-muted hover:text-text disabled:opacity-40"
                      title="A folder holding htdemucs_ft.yaml and its four .safetensors files"
                    >
                      Install model from a folder…
                    </button>
                    {(st.installed || st.weights.installed) && (
                      <button
                        disabled={busy || downloading}
                        onClick={async () => {
                          const ok = await askConfirm({
                            title: 'Remove the stem engine?',
                            body: 'The engine and its model are deleted from this computer. Converting again needs the download.',
                            confirmLabel: 'Remove',
                          })
                          if (ok) void act(api.stemEngineRemove)
                        }}
                        className="rounded-full px-3 py-1 text-xs text-faint hover:text-pink disabled:opacity-40"
                      >
                        Remove…
                      </button>
                    )}
                  </div>
                  {!st.manifest_available && !st.installed && (
                    <div className="text-xs text-faint">The download cannot be reached right now.</div>
                  )}
                </>
              )}
            </section>

            <section className="space-y-2">
              <h3 className="text-[11px] font-semibold uppercase tracking-wider text-faint">Compute device</h3>
              <div className="flex gap-2">
                {[
                  ['auto', 'Automatic'],
                  ['gpu', 'GPU'],
                  ['cpu', 'Processor only'],
                ].map(([value, label]) => (
                  <button
                    key={value}
                    onClick={() => void setDevice(value)}
                    className={`rounded-full px-3 py-1 text-xs ${
                      device === value ? 'is-selected text-text' : 'btn-glass text-muted hover:text-text'
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <div className="text-xs text-faint">
                Automatic uses the fastest available. The GPU is several times faster; without one, conversion runs on
                the processor.
              </div>
            </section>

            <section className="space-y-1">
              <h3 className="text-[11px] font-semibold uppercase tracking-wider text-faint">Conversion defaults</h3>
              <div className="text-xs text-muted">
                {defaults
                  ? defaults.mode === 'destination'
                    ? `Save to ${defaults.destination ?? 'a folder'}, ${
                        defaults.collection === 'add' ? 'added as new tracks' : 'the track pointed at its stem file'
                      }`
                    : 'Replace the originals'
                  : 'Replace the originals'}
              </div>
              <div className="text-xs text-faint">The Convert to Stems dialog remembers what you chose last.</div>
              {defaults && (
                <button
                  onClick={() =>
                    api.patchPrefs({ stemConvert: null }).then((p) => qc.setQueryData(['prefs'], p))
                  }
                  className="text-xs text-accent hover:underline"
                >
                  Reset to Replace
                </button>
              )}
            </section>

            <section className="space-y-1">
              <h3 className="text-[11px] font-semibold uppercase tracking-wider text-faint">Originals awaiting Save</h3>
              <div className="text-xs text-muted">
                {pending && pending.parked
                  ? `${pending.parked} original file${pending.parked === 1 ? '' : 's'} (${size(pending.bytes)}) kept beside their stem files. Saving deletes them; Discard puts them back.`
                  : 'None.'}
              </div>
            </section>

            {error && <div className="text-xs text-pink">{error}</div>}
          </div>
        </div>
      </div>
    </>,
    document.body,
  )
}
