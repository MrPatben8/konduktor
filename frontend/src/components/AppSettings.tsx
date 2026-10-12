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
