import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../api'
import { setMixer } from '../lib/audio/mixerState'
import { usePrefsWriter } from '../lib/prefs'
import { APP_SETTING_DEFAULTS, Check, Choice, useAppSetting } from './AppSettings'
import { Modal } from './Modal'
import { Fader } from './mixer/Controls'
import { CrossfaderCurveGraph } from './mixer/CrossfaderCurve'

/** How far the filter sweeps (`mixerMath.FILTER_RANGES`). */
const RANGES: [string, string][] = [
  ['full', 'Full - 20 Hz / 20 kHz'],
  ['musical', 'Musical - 150 Hz / 8 kHz'],
  ['gentle', 'Gentle - 500 Hz / 2 kHz'],
]
type Range = 'full' | 'musical' | 'gentle'

/** Resonance amounts offered (0..1 of the effect's maximum). */
const LEVELS: [string, string][] = [
  ['0', 'None'],
  ['0.3', 'Gentle'],
  ['0.6', 'Medium'],
  ['1', 'Strong'],
]

const section = 'text-[11px] font-semibold uppercase tracking-wider text-faint'

/**
 * Settings ▸ Performance mode… (its own panel, decided 2026-11-03): the decks,
 * the mixer and the Filter FX. Values live in the app
 * settings (userprefs.json, `APP_SETTING_DEFAULTS`); PrepStrip copies the
 * mixer's into `mixerState`.
 */
export function PerformanceSettings({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  const writePrefs = usePrefsWriter()
  const perfStems = useAppSetting('perfStems')
  const keylockDefault = useAppSetting('keylockDefault')
  const autoFocusDeck = useAppSetting('autoFocusDeck')
  const eqKill = useAppSetting('eqKill')
  const sharpnessSetting = useAppSetting('crossfaderSharpness')
  const fxFilterRange = useAppSetting('fxFilterRange')
  const fxEffectRange = useAppSetting('fxEffectRange')
  const fxFilterResonance = useAppSetting('fxFilterResonance')
  const fxReverbSize = useAppSetting('fxReverbSize')
  const fxDelayBeats = useAppSetting('fxDelayBeats')
  const fxCrusherDrive = useAppSetting('fxCrusherDrive')
  const fxFlangerBeats = useAppSetting('fxFlangerBeats')
  const [error, setError] = useState<string | null>(null)

  // The curve moves with the slider at once — mixer included, so it can be
  // heard while dragging — and is saved through the debounced prefs writer.
  const [sharpness, setSharpness] = useState(sharpnessSetting)
  useEffect(() => setSharpness(sharpnessSetting), [sharpnessSetting])
  const changeSharpness = (v: number) => {
    const s = Math.round(v * 100) / 100
    setSharpness(s)
    setMixer({ sharpness: s })
    writePrefs({ crossfaderSharpness: s })
  }

  const set = async (patch: Partial<typeof APP_SETTING_DEFAULTS>) => {
    setError(null)
    try {
      qc.setQueryData(['prefs'], await api.patchPrefs(patch))
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const curveName =
    sharpness === 0 ? 'Smooth' : sharpness === 1 ? 'Sharp' : sharpness < 0.52 ? 'Blend' : 'Cut'

  return (
    <Modal onDismiss={onClose}>
      <div className="flex w-full max-w-md flex-col overflow-hidden glass-overlay">
        <div className="flex items-center justify-between border-b border-line px-5 py-3">
          <h2 className="text-sm font-semibold text-text">Performance mode</h2>
          <button onClick={onClose} className="rounded px-1.5 text-faint hover:text-text" aria-label="Close">
            ×
          </button>
        </div>
        <div className="max-h-[70vh] space-y-4 overflow-y-auto px-5 py-4 text-sm">
          <section className="space-y-2">
            <h3 className={section}>Decks</h3>
            <Check
              checked={perfStems}
              onChange={(v) => void set({ perfStems: v })}
              title="Load stem tracks as stems"
              text="Off plays the mix: lighter on older computers."
            />
            <Check
              checked={keylockDefault}
              onChange={(v) => void set({ keylockDefault: v })}
              title="Keylock on by default"
            />
            <Check
              checked={autoFocusDeck}
              onChange={(v) => void set({ autoFocusDeck: v })}
              title="Click a deck to focus it"
              text="Off: only its A/B badge does."
            />
          </section>
          <section className="space-y-2">
            <h3 className={section}>Mixer</h3>
            <Choice
              title="EQ full left"
              value={eqKill ? 'kill' : 'cut'}
              options={[
                ['cut', 'Cut to −26 dB'],
                ['kill', 'Kill (silence)'],
              ]}
              onChange={(v) => void set({ eqKill: v === 'kill' })}
            />
            <div className="space-y-1.5 pt-1">
              <div className="flex items-baseline justify-between">
                <span className="text-sm text-text">Crossfader curve</span>
                <span className="font-mono text-xs text-muted">
                  {curveName} · {Math.round(sharpness * 100)}%
                </span>
              </div>
              <div className="flex items-center gap-4">
                <CrossfaderCurveGraph sharpness={sharpness} />
                <div className="min-w-0 flex-1 space-y-1">
                  <Fader
                    orientation="horizontal"
                    value={sharpness}
                    min={0}
                    max={1}
                    defaultValue={APP_SETTING_DEFAULTS.crossfaderSharpness}
                    onChange={changeSharpness}
                    label="Crossfader curve sharpness"
                    describe={(v) =>
                      `${Math.round(v * 100)}% sharp — double-click to reset to ${Math.round(APP_SETTING_DEFAULTS.crossfaderSharpness * 100)}%`
                    }
                    className="w-full"
                  />
                  <div className="flex justify-between text-[10px] uppercase tracking-wider text-faint">
                    <span>Smooth</span>
                    <span>Sharp</span>
                  </div>
                </div>
              </div>
            </div>
          </section>
          <section className="space-y-2">
            <h3 className={section}>Filter FX</h3>
            <span className="block text-xs text-faint">Effects grow as the filter opens.</span>
            <Choice
              title="Filter range (Filter alone)"
              value={fxFilterRange}
              options={RANGES}
              onChange={(v) => void set({ fxFilterRange: v as Range })}
            />
            <Choice
              title="Filter range (with an effect)"
              value={fxEffectRange}
              options={RANGES}
              onChange={(v) => void set({ fxEffectRange: v as Range })}
            />
            <Choice
              title="Filter resonance"
              value={String(fxFilterResonance)}
              options={LEVELS}
              onChange={(v) => void set({ fxFilterResonance: Number(v) })}
            />
            <Choice
              title="Reverb size (shortest)"
              value={String(fxReverbSize)}
              options={[
                ['1', 'Small (1 → 3 s)'],
                ['2', 'Medium (2 → 6 s)'],
                ['3', 'Large (3 → 9 s)'],
                ['4', 'Huge (4 → 12 s)'],
              ]}
              onChange={(v) => void set({ fxReverbSize: Number(v) })}
            />
            <Choice
              title="Delay length (shortest)"
              value={String(fxDelayBeats)}
              options={[
                ['0.125', '1/8 beat'],
                ['0.25', '1/4 beat'],
                ['0.5', '1/2 beat'],
                ['0.75', '3/4 beat'],
                ['1', '1 beat'],
              ]}
              onChange={(v) => void set({ fxDelayBeats: Number(v) })}
            />
            <Choice
              title="Bit crusher drive"
              value={String(fxCrusherDrive)}
              options={[
                ['1', 'Clean (×1)'],
                ['2', 'Warm (×2)'],
                ['4', 'Hot (×4)'],
              ]}
              onChange={(v) => void set({ fxCrusherDrive: Number(v) })}
            />
            <Choice
              title="Flanger sweep (slowest)"
              value={String(fxFlangerBeats)}
              options={[
                ['1', 'Every beat'],
                ['2', 'Every 2 beats'],
                ['4', 'Every 4 beats'],
                ['8', 'Every 8 beats'],
                ['16', 'Every 16 beats'],
              ]}
              onChange={(v) => void set({ fxFlangerBeats: Number(v) })}
            />
          </section>
          {error && <div className="text-xs text-pink">{error}</div>}
        </div>
        <div className="flex justify-end gap-2 border-t border-line px-5 py-3">
          <button onClick={onClose} className="rounded-full btn-primary px-4 py-1.5 text-sm font-semibold">
            Done
          </button>
        </div>
      </div>
    </Modal>
  )
}
