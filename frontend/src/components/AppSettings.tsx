import { createPortal } from 'react-dom'
import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'

/**
 * Settings → Konduktor: preferences about the app itself rather than about a
 * library or a feature (those have their own entries in the gear menu). Each
 * setting is a pref in userprefs.json, written as soon as it is changed.
 */

/** The prefs this dialog owns, and what each is when never set. */
export const APP_SETTING_DEFAULTS = {
  expandPlaylistFolders: false,
  /** Read by the backend when an export runs (`main.run_export`). */
  exportRootFolder: false,
  /** Decimal places on the deck's ELAPSED / REMAIN readouts (0–3). */
  timeReadoutDecimals: 1,
  /** Normalize's defaults (each run can change them): the measure brought to
   *  the target, and the true-peak ceiling a raise stops at. */
  levelMeasure: 'lufs' as 'lufs' | 'rms',
  levelTarget: -9,
  levelCeiling: -1,
}

type AppSettingKey = keyof typeof APP_SETTING_DEFAULTS

/** One app setting, read from the shared ['prefs'] query (the default until it loads). */
export function useAppSetting<K extends AppSettingKey>(key: K): (typeof APP_SETTING_DEFAULTS)[K] {
  const prefs = useQuery({ queryKey: ['prefs'], queryFn: api.getPrefs }).data
  const v = prefs?.[key]
  return typeof v === typeof APP_SETTING_DEFAULTS[key] ? (v as (typeof APP_SETTING_DEFAULTS)[K]) : APP_SETTING_DEFAULTS[key]
}

export function AppSettings({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  const expandFolders = useAppSetting('expandPlaylistFolders')
  const exportRootFolder = useAppSetting('exportRootFolder')
  const timeDecimals = useAppSetting('timeReadoutDecimals')
  const levelMeasure = useAppSetting('levelMeasure')
  const levelTarget = useAppSetting('levelTarget')
  const levelCeiling = useAppSetting('levelCeiling')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const set = async (patch: Partial<typeof APP_SETTING_DEFAULTS>) => {
    setError(null)
    try {
      qc.setQueryData(['prefs'], await api.patchPrefs(patch))
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return createPortal(
    <div
      aria-modal="true"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-6 backdrop-blur-[3px]"
      onClick={onClose}
    >
      <div className="flex w-full max-w-md flex-col overflow-hidden glass-overlay" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-line px-5 py-3">
          <h2 className="text-sm font-semibold text-text">Konduktor settings</h2>
          <button onClick={onClose} className="rounded px-1.5 text-faint hover:text-text" aria-label="Close">
            ×
          </button>
        </div>
        <div className="space-y-4 px-5 py-4 text-sm">
          <section className="space-y-2">
            <h3 className="text-[11px] font-semibold uppercase tracking-wider text-faint">Sidebar</h3>
            <label className="flex cursor-pointer items-start gap-2.5">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={expandFolders}
                onChange={(e) => void set({ expandPlaylistFolders: e.target.checked })}
              />
              <span className="min-w-0">
                <span className="block text-sm text-text">Expand playlist folders by default</span>
                <span className="block text-xs text-faint">
                  Show every folder open when a library is opened. Off, they start collapsed.
                </span>
              </span>
            </label>
          </section>
          <section className="space-y-2">
            <h3 className="text-[11px] font-semibold uppercase tracking-wider text-faint">Exports</h3>
            <label className="flex cursor-pointer items-start gap-2.5">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={exportRootFolder}
                onChange={(e) => void set({ exportRootFolder: e.target.checked })}
              />
              <span className="min-w-0">
                <span className="block text-sm text-text">Put exported playlists in a folder</span>
                <span className="block text-xs text-faint">
                  Nest an export's playlists in one folder named after the export. Off, they sit at the
                  top level of the exported library. Applies from the next export.
                </span>
              </span>
            </label>
          </section>
          <section className="space-y-2">
            <h3 className="text-[11px] font-semibold uppercase tracking-wider text-faint">Deck</h3>
            <label className="flex items-start justify-between gap-4">
              <span className="min-w-0">
                <span className="block text-sm text-text">Time readout precision</span>
                <span className="block text-xs text-faint">
                  Decimal places on the Elapsed and Remain readouts. 1 shows tenths, 3 milliseconds.
                </span>
              </span>
              <select
                className="shrink-0 rounded-lg well px-2 py-1 text-sm text-text outline-none"
                value={timeDecimals}
                onChange={(e) => void set({ timeReadoutDecimals: Number(e.target.value) })}
              >
                {['00:00', '00:00.0', '00:00.00', '00:00.000'].map((sample, n) => (
                  <option key={n} value={n}>
                    {n} — {sample}
                  </option>
                ))}
              </select>
            </label>
          </section>
          <section className="space-y-2">
            <h3 className="text-[11px] font-semibold uppercase tracking-wider text-faint">Normalize</h3>
            <span className="block text-xs text-faint">
              The defaults Mastering ▸ Normalize… starts from; each run can change them.
            </span>
            <label className="flex items-center justify-between gap-4">
              <span className="text-sm text-text">Level by</span>
              <select
                className="shrink-0 rounded-lg well px-2 py-1 text-sm text-text outline-none"
                value={levelMeasure}
                onChange={(e) => void set({ levelMeasure: e.target.value as 'lufs' | 'rms' })}
              >
                <option value="lufs">LUFS (integrated loudness)</option>
                <option value="rms">RMS (loudest sections)</option>
              </select>
            </label>
            <label className="flex items-center justify-between gap-4">
              <span className="text-sm text-text">Target</span>
              <LevelInput value={levelTarget} unit={levelMeasure === 'lufs' ? 'LUFS' : 'dB'} min={-30} max={0}
                onCommit={(v) => void set({ levelTarget: v })} />
            </label>
            <label className="flex items-center justify-between gap-4">
              <span className="min-w-0">
                <span className="block text-sm text-text">Peak ceiling</span>
                <span className="block text-xs text-faint">A raise stops here, so nothing clips.</span>
              </span>
              <LevelInput value={levelCeiling} unit="dBTP" min={-12} max={0}
                onCommit={(v) => void set({ levelCeiling: v })} />
            </label>
          </section>
          {error && <div className="text-xs text-pink">{error}</div>}
        </div>
        <div className="flex justify-end gap-2 border-t border-line px-5 py-3">
          <button onClick={onClose} className="rounded-full btn-primary px-4 py-1.5 text-sm font-semibold">
            Done
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

/** A dB value typed in place, committed on blur / Enter, clamped to its range. */
export function LevelInput({ value, unit, min, max, onCommit }: {
  value: number; unit: string; min: number; max: number; onCommit: (v: number) => void
}) {
  const [text, setText] = useState(String(value))
  useEffect(() => setText(String(value)), [value])
  const commit = () => {
    const v = Number(text.replace(',', '.'))
    if (!Number.isFinite(v)) return setText(String(value))
    const clamped = Math.min(max, Math.max(min, Math.round(v * 10) / 10))
    setText(String(clamped))
    if (clamped !== value) onCommit(clamped)
  }
  return (
    <span className="flex shrink-0 items-center gap-1.5">
      <input
        className="w-16 rounded-lg well px-2 py-1 text-right font-mono text-sm tabular-nums text-text outline-none"
        value={text}
        inputMode="decimal"
        onChange={(e) => setText(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
      />
      <span className="w-10 text-xs text-faint">{unit}</span>
    </span>
  )
}
