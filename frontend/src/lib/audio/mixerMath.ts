// The mixer's control curves: how a knob or fader position becomes a gain or a
// filter setting. Pure, so they are tested on their own (mixerMath.test.ts);
// `mixer.ts` applies them to the audio graph.

export const dbToGain = (db: number): number => 10 ** (db / 20)
export const gainToDb = (g: number): number => (g > 0 ? 20 * Math.log10(g) : -Infinity)
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v))

/** An EQ band's boost at full right. */
export const EQ_BOOST_DB = 6
/** An EQ band's cut at full left, DJM-style (the alternative is a full kill). */
export const EQ_CUT_DB = -26
/** The isolator's crossovers (Hz) — where the deck's waveform splits its bands. */
export const EQ_LOW_HZ = 250
export const EQ_HIGH_HZ = 2500

/**
 * An EQ knob (-1..1, centre = flat) as a linear gain. Right boosts to +6 dB.
 * Left cuts in dB to -26 dB; with `kill`, the last fifth of the travel fades
 * on from there to silence, so the band can disappear entirely.
 */
export function eqGain(v: number, kill: boolean): number {
  v = clamp(v, -1, 1)
  if (v >= 0) return dbToGain(EQ_BOOST_DB * v)
  const g = dbToGain(-EQ_CUT_DB * v)
  if (!kill || v > -0.8) return g
  return g * ((1 + v) / 0.2)
}

/** The trim knob's range either side of the track's auto gain (dB). */
export const TRIM_RANGE_DB = 12

/** A channel fader (0..1) as a gain: an audio taper, -6 dB at ~0.7. */
export const faderGain = (f: number): number => clamp(f, 0, 1) ** 2

export type CrossfaderCurve = 'smooth' | 'sharp'

/**
 * The crossfader (-1 = all A, 1 = all B) as the two channels' gains. Smooth is
 * constant-power: both at -3 dB in the middle, for blends. Sharp keeps both at
 * full until the last tenth of the travel, for cuts and scratching.
 */
export function xfadeGains(x: number, curve: CrossfaderCurve): [number, number] {
  x = clamp(x, -1, 1)
  if (curve === 'smooth') {
    const t = ((x + 1) / 2) * (Math.PI / 2)
    return [Math.cos(t), Math.sin(t)]
  }
  return [x <= 0.9 ? 1 : (1 - x) / 0.1, x >= -0.9 ? 1 : (1 + x) / 0.1]
}

/** The colour knob's dead centre: inside it, the effect is out of the path. */
export const FX_CENTRE = 0.02

/**
 * The filter at colour `c` (-1..1): left a low-pass falling from 20 kHz to
 * 150 Hz, right a high-pass rising from 20 Hz to 8 kHz, both on a log scale so
 * the sweep sounds even. Null at the centre (bypassed).
 */
export function filterSweep(c: number): { type: 'lowpass' | 'highpass'; freq: number } | null {
  c = clamp(c, -1, 1)
  if (Math.abs(c) < FX_CENTRE) return null
  const a = Math.abs(c)
  return c < 0
    ? { type: 'lowpass', freq: 20000 * (150 / 20000) ** a }
    : { type: 'highpass', freq: 20 * (8000 / 20) ** a }
}

/** The echo lengths offered (beats). */
export const ECHO_BEATS = [1 / 8, 1 / 4, 1 / 2, 3 / 4, 1]

/** Seconds of echo for `beats` at `bpm` (120 when the tempo is unknown). */
export function echoSeconds(beats: number, bpm: number): number {
  return (beats * 60) / (bpm > 0 ? bpm : 120)
}
