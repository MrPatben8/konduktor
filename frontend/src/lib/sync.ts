// Beat sync between two decks (performance mode, decided 2026-11-02): SYNC
// matches a follower's tempo to the tempo MASTER's and lines its beats up, from
// the two beatgrids — the same marker-list math as everything else
// (`beatgrid.ts`), so a flexible grid syncs to the segment the playhead is in.
// Pure (sync.test.ts); PrepStrip runs it.

import type { BeatGrid } from './beatgrid'

export interface DeckTempo {
  grid: BeatGrid | null
  /** Playhead (s, track time). */
  pos: number
  /** Playback rate (1 = the file's own tempo). */
  rate: number
}

/** BPM as heard: the grid's tempo where the playhead is, times the rate. */
export function effectiveBpm(d: DeckTempo): number | null {
  return d.grid ? d.grid.bpmAt(d.pos) * d.rate : null
}

/** How many master beats one follower beat is felt as: ½, 1 or 2 — whichever
 *  brings the two tempos closest (an 87 BPM grid against 174 syncs as double). */
export const OCTAVES = [0.5, 1, 2]

export function octaveFor(masterBpm: number, followerBpm: number): number {
  let best = 1
  let bestErr = Infinity
  for (const m of OCTAVES) {
    const err = Math.abs(Math.log(masterBpm / (followerBpm * m)))
    if (err < bestErr - 1e-9) {
      bestErr = err
      best = m
    }
  }
  return best
}

/**
 * The rate that puts the follower on the master's tempo, and the octave it is
 * matched at. Null without both grids.
 */
export function syncRate(master: DeckTempo, follower: DeckTempo): { rate: number; octave: number } | null {
  const m = effectiveBpm(master)
  if (m == null || !follower.grid) return null
  const f = follower.grid.bpmAt(follower.pos)
  const octave = octaveFor(m, f)
  return { rate: m / (f * octave), octave }
}

/** Where a beat position sits between beats, 0..1. */
const frac = (x: number) => x - Math.floor(x)

/**
 * How far the follower's beats are from the master's, in MASTER beats, in
 * [-0.5, 0.5): positive = the follower is ahead. Null without both grids.
 */
export function phaseOffset(master: DeckTempo, follower: DeckTempo, octave: number): number | null {
  if (!master.grid || !follower.grid) return null
  const pm = frac(master.grid.beatIndexAt(master.pos))
  const pf = frac(follower.grid.beatIndexAt(follower.pos) * octave)
  let d = pf - pm
  if (d >= 0.5) d -= 1
  if (d < -0.5) d += 1
  return d
}

/** The follower's playhead with its beats on the master's (the smallest move;
 *  it may fall before the track — performance mode's lead-in). */
export function alignedPosition(follower: DeckTempo, offset: number, octave: number): number {
  const g = follower.grid!
  return g.timeOfBeat(g.beatIndexAt(follower.pos) - offset / octave)
}

/** The pitch ranges a deck offers (fraction of the tempo either way). */
export const PITCH_RANGES = [0.08, 0.16, 0.5]

/** The narrowest range that holds `rate`, so a sync never pins the fader. */
export function rangeFor(rate: number, current: number): number {
  const need = Math.abs(rate - 1)
  if (need <= current + 1e-9) return current
  return PITCH_RANGES.find((r) => need <= r + 1e-9) ?? PITCH_RANGES[PITCH_RANGES.length - 1]
}

// ---- pitch bend (the phase meter's drag) ------------------------------------

/** The bend at full drag (±5%), reached at BEND_PX; Shift is four times finer. */
export const BEND_MAX = 0.05
export const BEND_PX = 80

/** A drag of `dx` px as a rate multiplier: squared, so small drags are fine
 *  nudges and a long one a firm push. Right = faster. */
export function bendFor(dx: number, fine: boolean): number {
  const b = Math.max(-1, Math.min(1, dx / BEND_PX))
  return 1 + BEND_MAX * b * Math.abs(b) * (fine ? 0.25 : 1)
}
