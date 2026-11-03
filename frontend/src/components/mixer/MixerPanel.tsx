import { useEffect, useRef, useState, type RefObject } from 'react'
import { mixer } from '../../lib/audio/mixer'
import { faderGain, filterSweep, eqGain, gainToDb, TRIM_RANGE_DB, EQ_CUT_DB } from '../../lib/audio/mixerMath'
import {
  FX_LABELS,
  FX_TYPES,
  setChannel,
  setMixer,
  useMixer,
  type Channel,
  type FxSettings,
  type FxType,
} from '../../lib/audio/mixerState'
import { Icon } from '../../lib/icons'
import { ContextMenu } from '../ContextMenu'
import { Fader, Knob } from './Controls'

/** The meters' scale (dBFS). */
const METER_FLOOR = -48
const METER_TOP = 3

const fmtDb = (db: number, places = 1) =>
  !isFinite(db) ? '−∞ dB' : `${db > 0 ? '+' : db < 0 ? '−' : ''}${Math.abs(db).toFixed(places)} dB`

function describeFx(fx: FxType, c: number, set: FxSettings): string {
  const a = Math.round(Math.abs(c) * 100)
  if (Math.abs(c) < 0.02) return `${FX_LABELS[fx]}: off (turn left or right)`
  switch (fx) {
    case 'filter': {
      const s = filterSweep(c)!
      const hz = s.freq >= 1000 ? `${(s.freq / 1000).toFixed(1)} kHz` : `${Math.round(s.freq)} Hz`
      return `Filter: ${s.type === 'lowpass' ? 'low-pass' : 'high-pass'} at ${hz} · resonance ${Math.round(set.filterResonance * 100)}%`
    }
    case 'echo':
      return `Echo ${a}% · ${beatsLabel(set.echoBeats)} · ${c < 0 ? 'darker' : 'thinner'} repeats`
    case 'space':
      return `Space ${a}% · ${set.spaceSize} s · ${c < 0 ? 'darker' : 'thinner'}`
    case 'noise':
      return `Noise ${a}% · ${c < 0 ? 'low-pass' : 'high-pass'} sweep`
    case 'crush':
      return `Crush ${a}% · drive ×${set.crushDrive}`
  }
}

function beatsLabel(b: number): string {
  return b === 1 ? '1 beat' : b === 0.75 ? '3/4 beat' : b === 0.5 ? '1/2 beat' : b === 0.25 ? '1/4 beat' : '1/8 beat'
}

/**
 * The mixer, between the decks in performance mode (decided 2026-11-02): per
 * channel TRIM (on top of the track's auto gain), HI / MID / LOW, a colour
 * effect with its own selector (its one parameter is an app setting, in
 * Settings ▸ Performance mode…), a fader and its meter; between
 * them the master meter and the limiter's light; the crossfader beneath. The
 * values live in `mixerState` and the audio in `mixer.ts`.
 */
export function MixerPanel({ autoGain }: { autoGain: Record<Channel, number | null> }) {
  const meterRefs = {
    A: useRef<HTMLDivElement>(null),
    B: useRef<HTMLDivElement>(null),
    master: useRef<HTMLDivElement>(null),
  }
  const limitRef = useRef<HTMLSpanElement>(null)

  // One frame loop for every meter, writing the DOM directly — sixty React
  // renders a second for three bars would be waste.
  useEffect(() => {
    let raf = 0
    const held = { A: METER_FLOOR, B: METER_FLOOR, master: METER_FLOOR }
    let limitUntil = 0
    const tick = (now: number) => {
      const lv = mixer().levels()
      for (const k of ['A', 'B', 'master'] as const) {
        const db = Math.max(METER_FLOOR, lv[k])
        held[k] = db >= held[k] ? db : Math.max(db, held[k] - 0.6) // fall back ~36 dB/s
        const el = meterRefs[k].current
        if (el) {
          el.style.height = `${((held[k] - METER_FLOOR) / (METER_TOP - METER_FLOOR)) * 100}%`
          el.style.background =
            held[k] > -1 ? 'var(--color-pink)' : held[k] > -9 ? 'var(--color-gold)' : 'var(--color-mint)'
        }
      }
      if (lv.limiting > 0.5) limitUntil = now + 600
      if (limitRef.current) limitRef.current.dataset.on = String(now < limitUntil)
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
    // The refs are stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const crossfader = useMixer((s) => s.crossfader)
  const sharpness = useMixer((s) => s.sharpness)

  return (
    <div className="well flex w-[24rem] shrink-0 flex-col gap-1.5 rounded-2xl px-2.5 pb-1.5 pt-2.5">
      <div className="flex min-h-0 flex-1 items-stretch gap-2">
        <Strip ch="A" autoGain={autoGain.A} meterRef={meterRefs.A} />
        <div className="flex w-3 flex-col items-center gap-1" title="Master — after the limiter">
          <MeterBar fillRef={meterRefs.master} />
          <span
            ref={limitRef}
            data-on="false"
            title="The limiter is holding the master below clipping"
            className="text-[7.5px] font-bold leading-none tracking-[0.04em] text-faint/50 data-[on=true]:text-pink data-[on=true]:[text-shadow:0_0_6px_var(--color-pink)]"
          >
            LIM
          </span>
        </div>
        <Strip ch="B" autoGain={autoGain.B} meterRef={meterRefs.B} mirrored />
      </div>
      <div className="flex h-6 shrink-0 items-center gap-2 px-1">
        <span className="font-mono text-[11px] font-semibold text-muted">A</span>
        <Fader
          orientation="horizontal"
          value={crossfader}
          min={-1}
          max={1}
          defaultValue={0}
          onChange={(v) => setMixer({ crossfader: Math.abs(v) < 0.03 ? 0 : v })}
          label="Crossfader"
          describe={(v) =>
            `Crossfader (curve ${Math.round(sharpness * 100)}% sharp — Settings ▸ Performance mode…) — ${
              Math.abs(v) < 0.03 ? 'centre' : v < 0 ? `toward A ${Math.round(-v * 100)}%` : `toward B ${Math.round(v * 100)}%`
            }`
          }
          className="flex-1"
        />
        <span className="font-mono text-[11px] font-semibold text-muted">B</span>
      </div>
    </div>
  )
}

function MeterBar({ fillRef }: { fillRef: RefObject<HTMLDivElement | null> }) {
  return (
    <div className="relative w-1.5 flex-1 overflow-hidden rounded-full bg-black/40">
      <div ref={fillRef} className="absolute inset-x-0 bottom-0 h-0 rounded-full transition-[background-color] duration-150" />
    </div>
  )
}

/** One channel: its knobs, then (toward the middle) its fader and meter. */
function Strip({
  ch,
  autoGain,
  meterRef,
  mirrored = false,
}: {
  ch: Channel
  autoGain: number | null
  meterRef: RefObject<HTMLDivElement | null>
  mirrored?: boolean
}) {
  const s = useMixer((m) => m[ch])
  const eqKill = useMixer((m) => m.eqKill)
  const fxSettings = useMixer((m) => m.fx)
  const [fxMenu, setFxMenu] = useState<{ x: number; y: number } | null>(null)
  const set = (patch: Parameters<typeof setChannel>[1]) => setChannel(ch, patch)
  const describeEq = (band: string) => (v: number) => {
    const g = eqGain(v, eqKill)
    return `${band} ${Math.abs(v) < 0.01 ? 'flat' : g === 0 ? 'killed' : fmtDb(gainToDb(g))}${
      v < 0 ? ` (full left: ${eqKill ? 'kill' : `${EQ_CUT_DB} dB`})` : ''
    }`
  }
  const eq = (band: 'high' | 'mid' | 'low', label: string, name: string) => (
    <Knob
      value={s[band]}
      min={-1}
      max={1}
      defaultValue={0}
      bipolar
      onChange={(v) => set({ [band]: Math.abs(v) < 0.03 ? 0 : v })}
      label={label}
      describe={describeEq(name)}
    />
  )
  const knobs = (
    <div className="grid grid-cols-4 content-between gap-x-1.5">
      <Knob
        value={s.trim}
        min={-TRIM_RANGE_DB}
        max={TRIM_RANGE_DB}
        defaultValue={0}
        bipolar
        color="var(--color-text)"
        onChange={(v) => set({ trim: Math.abs(v) < 0.3 ? 0 : v })}
        label="Trim"
        describe={(v) =>
          `Trim ${fmtDb(v)}${autoGain != null ? ` on the track's auto gain (${fmtDb(autoGain)})` : ' (this track has no auto gain)'}`
        }
      />
      {eq('high', 'Hi', 'High')}
      {eq('mid', 'Mid', 'Mid')}
      {eq('low', 'Low', 'Low')}
      <button
        onClick={(e) => {
          const r = e.currentTarget.getBoundingClientRect()
          setFxMenu({ x: r.left, y: r.bottom + 4 })
        }}
        title="Which effect the COLOR knob drives"
        className="btn-glass col-span-3 mt-0.5 flex h-6 items-center justify-center gap-0.5 self-start rounded-md text-[10px] font-semibold uppercase tracking-[0.06em] text-text"
      >
        {FX_LABELS[s.fx]}
        <Icon name="chevronDown" size={11} />
      </button>
      <Knob
        value={s.color}
        min={-1}
        max={1}
        defaultValue={0}
        bipolar
        color="var(--color-gold)"
        onChange={(v) => set({ color: Math.abs(v) < 0.02 ? 0 : v })}
        label="Color"
        describe={(v) => describeFx(s.fx, v, fxSettings)}
      />
    </div>
  )
  const fader = (
    <div className="flex flex-col items-center gap-0.5">
      <Fader
        orientation="vertical"
        value={s.fader}
        min={0}
        max={1}
        defaultValue={1}
        onChange={(v) => set({ fader: v })}
        label={`Channel ${ch} fader`}
        describe={(v) => `Channel ${ch}: ${fmtDb(gainToDb(faderGain(v)))}`}
        className="flex-1"
      />
      <span className="font-mono text-[10px] font-semibold leading-none text-muted">{ch}</span>
    </div>
  )
  const meter = (
    <div className="flex flex-col pb-3" title={`Channel ${ch} level (after its fader)`}>
      <MeterBar fillRef={meterRef} />
    </div>
  )
  return (
    <div className={`flex min-w-0 flex-1 items-stretch gap-1.5 ${mirrored ? 'flex-row-reverse' : ''}`}>
      {knobs}
      {fader}
      {meter}
      {fxMenu && (
        <ContextMenu
          x={fxMenu.x}
          y={fxMenu.y}
          onClose={() => setFxMenu(null)}
          items={[
            { heading: `Channel ${ch} · colour effect` },
            ...FX_TYPES.map((t) => ({
              label: FX_LABELS[t],
              hint: t === s.fx ? '✓' : undefined,
              onClick: () => set({ fx: t }),
            })),
          ]}
        />
      )}
    </div>
  )
}
