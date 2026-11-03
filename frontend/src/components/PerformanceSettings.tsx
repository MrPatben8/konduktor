import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../api'
import { setMixer } from '../lib/audio/mixerState'
import { usePrefsWriter } from '../lib/prefs'
import { APP_SETTING_DEFAULTS, Check, Choice, useAppSetting } from './AppSettings'
import { Modal } from './Modal'
import { Fader } from './mixer/Controls'
import { CrossfaderCurveGraph } from './mixer/CrossfaderCurve'

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
 * the mixer and the colour effects' parameters. Values live in the app
 * settings (userprefs.json, `APP_SETTING_DEFAULTS`); PrepStrip copies the
 * mixer's into `mixerState`.
 */
export function PerformanceSettings({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  const writePrefs = usePrefsWriter()
  const perfStems = useAppSetting('perfStems')
  const keylockDefault = useAppSetting('keylockDefault')
  const eqKill = useAppSetting('eqKill')
  const sharpnessSetting = useAppSetting('crossfaderSharpness')
  const fxFilterResonance = useAppSetting('fxFilterResonance')
  const fxEchoBeats = useAppSetting('fxEchoBeats')
  const fxSpaceSize = useAppSetting('fxSpaceSize')
  const fxNoiseResonance = useAppSetting('fxNoiseResonance')
  const fxCrushDrive = useAppSetting('fxCrushDrive')
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
          <p className="text-xs text-faint">
            The second deck and the mixer (P, or the decks button in the deck's header).
          </p>
          <section className="space-y-2">
            <h3 className={section}>Decks</h3>
            <Check
              checked={perfStems}
              onChange={(v) => void set({ perfStems: v })}
              title="Load stem tracks as stems"
              text="A stem track loaded into a deck in performance mode plays its four stems (mute and solo work). Off, it plays its mix: lighter on an older computer, since keylock then stretches one stream instead of four. Applies to the next track loaded."
            />
            <Check
              checked={keylockDefault}
              onChange={(v) => void set({ keylockDefault: v })}
              title="Keylock on"
              text="Each deck starts with keylock on: a tempo change keeps the pitch. Each deck's KEY button still switches it."
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
                    defaultValue={0}
                    onChange={changeSharpness}
                    label="Crossfader curve sharpness"
                    describe={(v) => `${Math.round(v * 100)}% sharp — double-click for smooth`}
                    className="w-full"
                  />
                  <div className="flex justify-between text-[10px] uppercase tracking-wider text-faint">
                    <span>Smooth</span>
                    <span>Sharp</span>
                  </div>
                </div>
              </div>
              <span className="block text-xs text-faint">
                Smooth blends: each channel fades across the whole travel, both at −3 dB in the middle.
                Sharper keeps both at full level across a widening middle and fades only near the ends —
                at full, a cut over the last few percent, for scratching.
              </span>
            </div>
          </section>
          <section className="space-y-2">
            <h3 className={section}>Colour effects</h3>
            <span className="block text-xs text-faint">Each effect's setting, on both channels.</span>
            <Choice
              title="Filter resonance"
              value={String(fxFilterResonance)}
              options={LEVELS}
              onChange={(v) => void set({ fxFilterResonance: Number(v) })}
            />
            <Choice
              title="Echo length"
              value={String(fxEchoBeats)}
              options={[
                ['0.125', '1/8 beat'],
                ['0.25', '1/4 beat'],
                ['0.5', '1/2 beat'],
                ['0.75', '3/4 beat'],
                ['1', '1 beat'],
              ]}
              onChange={(v) => void set({ fxEchoBeats: Number(v) })}
            />
            <Choice
              title="Space size"
              value={String(fxSpaceSize)}
              options={[
                ['1', 'Small (1 s)'],
                ['2.5', 'Medium (2.5 s)'],
                ['4', 'Large (4 s)'],
                ['6', 'Huge (6 s)'],
              ]}
              onChange={(v) => void set({ fxSpaceSize: Number(v) })}
            />
            <Choice
              title="Noise resonance"
              value={String(fxNoiseResonance)}
              options={LEVELS}
              onChange={(v) => void set({ fxNoiseResonance: Number(v) })}
            />
            <Choice
              title="Crush drive"
              value={String(fxCrushDrive)}
              options={[
                ['1', 'Clean (×1)'],
                ['2', 'Warm (×2)'],
                ['4', 'Hot (×4)'],
              ]}
              onChange={(v) => void set({ fxCrushDrive: Number(v) })}
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
