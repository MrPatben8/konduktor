import { createPortal } from 'react-dom'
import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type NormalizeOptions } from '../api'
import { localLibraryProblem } from '../lib/platformCopy'
import { FolderPicker } from './FolderPicker'
import { LevelInput, useAppSetting } from './AppSettings'

/**
 * Settings → Local libraries: where a "This computer" set is pushed.
 *
 * This computer's, not the set's: a remote library's sets are kept on its
 * server, but which Traktor collection and rekordbox library a laptop DJs from,
 * and where their audio goes, belong to the laptop. So they are prefs —
 * `localLibraries` ({platform: path | null}, null = detected), `localAudioRoot`,
 * `localNormalise` and `localLevel`.
 *
 * The audio folder is SHARED by every push, so a file has one level: Normalise
 * lives here rather than on a set. A library is chosen by its FOLDER (the
 * `Traktor 4.x` folder, rekordbox's), the file in it being the platform's own.
 */

const LIBRARY_FILE: Record<string, string> = { traktor: 'collection.nml', rekordbox: 'master.db' }

export function LocalLibrariesSettings({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  const locals = useQuery({ queryKey: ['local-libraries'], queryFn: api.localLibraries })
  const prefs = useQuery({ queryKey: ['prefs'], queryFn: api.getPrefs })
  const defaultMeasure = useAppSetting('levelMeasure')
  const defaultTarget = useAppSetting('levelTarget')
  const defaultCeiling = useAppSetting('levelCeiling')
  const [picking, setPicking] = useState<string | null>(null)   // a platform, or 'audio'
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && !picking && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, picking])

  const p = prefs.data ?? {}
  const overrides = (p.localLibraries as Record<string, string | null> | undefined) ?? {}
  const normalise = p.localNormalise === true
  const stored = p.localLevel as Partial<NormalizeOptions> | undefined
  const level: NormalizeOptions = {
    measure: stored?.measure ?? defaultMeasure,
    target: stored?.target ?? defaultTarget,
    ceiling: stored?.ceiling ?? defaultCeiling,
  }

  const patch = async (body: Record<string, unknown>) => {
    setError(null)
    try {
      qc.setQueryData(['prefs'], await api.patchPrefs(body))
      qc.invalidateQueries({ queryKey: ['local-libraries'] })
      qc.invalidateQueries({ queryKey: ['push-preview'] })
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const choose = (folder: string) => {
    if (picking === 'audio') {
      patch({ localAudioRoot: folder })
    } else if (picking) {
      const sep = folder.includes('\\') && !folder.includes('/') ? '\\' : '/'
      patch({ localLibraries: { ...overrides, [picking]: `${folder.replace(/[\\/]+$/, '')}${sep}${LIBRARY_FILE[picking]}` } })
    }
  }

  const d = locals.data
  const pickerValue =
    picking === 'audio'
      ? d?.audio_root ?? ''
      : (d?.libraries.find((l) => l.platform === picking)?.path ?? '').replace(/[\\/][^\\/]*$/, '')

  return createPortal(
    <>
      {picking && (
        <FolderPicker value={pickerValue} onChange={choose} onClose={() => setPicking(null)} />
      )}
      <div
        aria-modal="true"
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-6 backdrop-blur-[3px]"
        onClick={onClose}
      >
        <div className="flex max-h-full w-full max-w-lg flex-col overflow-hidden glass-overlay" onClick={(e) => e.stopPropagation()}>
          <div className="flex items-center justify-between border-b border-line px-5 py-3">
            <h2 className="text-sm font-semibold text-text">Local libraries</h2>
            <button onClick={onClose} className="rounded px-1.5 text-faint hover:text-text" aria-label="Close">
              ×
            </button>
          </div>
          <div className="min-h-0 space-y-5 overflow-y-auto px-5 py-4 text-sm">
            <p className="text-muted">
              Where an export set made for <span className="text-text">This computer</span> is pushed:
              the DJ apps you play from here, and one folder for their audio.
            </p>

            <section className="space-y-2">
              <h3 className="text-[11px] uppercase tracking-wider text-faint">DJ apps</h3>
              {(d?.libraries ?? []).map((lib) => {
                const problem = lib.blocked === 'open_in_konduktor' ? null : localLibraryProblem(lib)
                return (
                  <div key={lib.platform} className="rounded-md well px-3 py-2">
                    <div className="flex items-center gap-2">
                      <span className="text-text">{lib.name}</span>
                      <span className="text-[11px] text-faint">{lib.detected ? 'found automatically' : 'chosen'}</span>
                      <span className="ml-auto flex gap-1">
                        {!lib.detected && (
                          <button
                            onClick={() => patch({ localLibraries: { ...overrides, [lib.platform]: null } })}
                            className="btn-glass rounded-full px-2.5 py-0.5 text-xs text-muted hover:text-text"
                          >
                            Find automatically
                          </button>
                        )}
                        <button
                          onClick={() => setPicking(lib.platform)}
                          className="btn-glass rounded-full px-2.5 py-0.5 text-xs text-muted hover:text-text"
                        >
                          Change…
                        </button>
                      </span>
                    </div>
                    <div title={lib.path ?? undefined} className="mt-0.5 truncate font-mono text-[11px] text-faint">
                      {lib.path ?? '—'}
                    </div>
                    {problem && <div className="mt-0.5 text-[11px] text-pink">{problem}</div>}
                  </div>
                )
              })}
            </section>

            <section className="space-y-2">
              <h3 className="text-[11px] uppercase tracking-wider text-faint">Audio folder</h3>
              <div className="flex items-center gap-2">
                <span title={d?.audio_root} dir="rtl"
                  className="min-w-0 flex-1 truncate rounded-md well px-2 py-1.5 text-left font-mono text-xs text-text">
                  {d?.audio_root ?? '…'}
                </span>
                {d && d.audio_root !== d.audio_root_default && (
                  <button onClick={() => patch({ localAudioRoot: null })}
                    className="shrink-0 btn-glass rounded-full px-2.5 py-1 text-xs text-muted hover:text-text">
                    Reset
                  </button>
                )}
                <button onClick={() => setPicking('audio')}
                  className="shrink-0 btn-glass rounded-full px-2.5 py-1 text-xs text-muted hover:text-text">
                  Change…
                </button>
              </div>
              <p className="text-[11px] text-faint">
                Every push copies its tracks here, once each, however many sets or apps use them. A
                file no push uses any more is deleted. Changing the folder copies everything again
                on the next push.
              </p>
            </section>

            <section className="space-y-2">
              <h3 className="text-[11px] uppercase tracking-wider text-faint">Level</h3>
              <label className="flex cursor-pointer items-start gap-2.5">
                <input type="checkbox" className="mt-0.5" checked={normalise}
                  onChange={(e) => patch({ localNormalise: e.target.checked })} />
                <span>
                  <span className="block text-text">Normalise audio</span>
                  <span className="block text-xs text-faint">
                    Brings the copies to one level. Your own files are never changed.
                  </span>
                </span>
              </label>
              {normalise && (
                <div className="ml-6 space-y-1.5 text-xs">
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-muted">Level by</span>
                    <select
                      className="rounded-lg well px-2 py-1 text-xs text-text outline-none"
                      value={level.measure}
                      onChange={(e) => patch({ localLevel: { ...level, measure: e.target.value } })}
                    >
                      <option value="lufs">LUFS</option>
                      <option value="rms">RMS (loudest sections)</option>
                    </select>
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-muted">Target</span>
                    <LevelInput value={level.target} unit={level.measure === 'lufs' ? 'LUFS' : 'dB'} min={-30} max={0}
                      onCommit={(v) => patch({ localLevel: { ...level, target: v } })} />
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-muted" title="A raise stops here, so nothing clips">Peak ceiling</span>
                    <LevelInput value={level.ceiling} unit="dBTP" min={-12} max={0}
                      onCommit={(v) => patch({ localLevel: { ...level, ceiling: v } })} />
                  </div>
                </div>
              )}
            </section>
            {error && <div className="text-xs text-pink">{error}</div>}
          </div>
        </div>
      </div>
    </>,
    document.body,
  )
}
