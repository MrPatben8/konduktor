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
 * How far the FILTER knob sweeps (settings, decided 2026-11-03): `full` runs
 * the low-pass down to 20 Hz and the high-pass up to 20 kHz, so either end is
 * near silence; `musical` stops at 150 Hz / 8 kHz, where the track is thinned
 * but still there; `gentle` at 500 Hz / 2 kHz, where it stays clearly heard.
 */
export type FilterRange = 'full' | 'musical' | 'gentle'
export const FILTER_RANGES: Record<FilterRange, { low: number; high: number }> = {
  full: { low: 20, high: 20000 },
  musical: { low: 150, high: 8000 },
  gentle: { low: 500, high: 2000 },
}

/**
 * The filter at knob `c` (-1..1): left a low-pass falling from 20 kHz, right a
 * high-pass rising from 20 Hz, to the range's ends, on a log scale so the
 * sweep sounds even. Null at the centre (bypassed).
 */
export function filterSweep(
  c: number,
  range: FilterRange = 'full',
): { type: 'lowpass' | 'highpass'; freq: number } | null {
  c = clamp(c, -1, 1)
  if (Math.abs(c) < FX_CENTRE) return null
  const a = Math.abs(c)
  const end = FILTER_RANGES[range]
  return c < 0
    ? { type: 'lowpass', freq: 20000 * (end.low / 20000) ** a }
    : { type: 'highpass', freq: 20 * (end.high / 20) ** a }
}

/**
 * The Filter FX grow as the knob opens (decided 2026-11-03), in STEPS of two
 * at each further quarter of its travel, so every length stays a musical one
 * of the tempo: 0 near the centre, 3 in the last quarter.
 */
export const knobStep = (a: number): number => Math.min(3, Math.floor(clamp(a, 0, 1) * 4))

/** The phaser's sweep (beats): `slowest` (the setting), then ½, ¼, ⅛ of it —
 *  faster as the filter opens. */
export const phaserBeatsAt = (slowest: number, a: number): number => slowest / 2 ** knobStep(a)

/** The delay's length (beats): `shortest` (the setting), then ×2, ×4, ×8 —
 *  longer as the filter opens. */
export const delayBeatsAt = (shortest: number, a: number): number => shortest * 2 ** knobStep(a)

/** The long reverb is this many times the set (short) one; the knob blends
 *  from the short into the long as it opens. */
export const REVERB_GROWTH = 3

/** Seconds for `beats` at `bpm` (120 when the tempo is unknown) — the delay's
 *  length, the phaser's sweep. */
export function beatSeconds(beats: number, bpm: number): number {
  return (beats * 60) / (bpm > 0 ? bpm : 120)
}
