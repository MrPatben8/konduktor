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

/** The narrowest fade a fully sharp crossfader leaves (of its whole travel). */
export const XF_MIN_WIDTH = 0.04

/** How much of the travel a channel takes to fade out, at `sharpness` 0..1. */
export const xfadeWidth = (sharpness: number): number => 1 - clamp(sharpness, 0, 1) * (1 - XF_MIN_WIDTH)

/**
 * The crossfader (-1 = all A, 1 = all B) as the two channels' gains, at a
 * SHARPNESS from 0 to 1 (decided 2026-11-03: fully adjustable). Each channel
 * fades on a quarter-cosine over the last `xfadeWidth` of the travel toward the
 * other side and is at full level before it. At 0 that is the whole travel —
 * constant power, both at -3 dB in the middle, for blends; turned up, both stay
 * full across a widening middle (no dip), until at 1 a channel cuts over the
 * last 4%, for cuts and scratching.
 */
export function xfadeGains(x: number, sharpness: number): [number, number] {
  const t = (clamp(x, -1, 1) + 1) / 2 // 0 = all A
  const w = xfadeWidth(sharpness)
  const fade = (u: number) => Math.cos((Math.PI / 2) * clamp((u - (1 - w)) / w, 0, 1))
  return [fade(t), fade(1 - t)]
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
