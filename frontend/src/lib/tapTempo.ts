// Tap tempo: turn a run of button presses into a BPM worth committing.
//
// A human taps with roughly 20–30 ms of jitter per press, which at 125 BPM is
// several BPM of error in any ONE interval. So:
//   - the tempo is the least-squares slope of tap time against tap number, which
//     uses every tap in the window rather than just the first and last;
//   - nothing is reported until MIN_TAPS, since two or three taps cannot tell
//     126 from 131;
//   - the result snaps to a whole BPM (or a half where it sits clearly between
//     two), because tapping cannot resolve finer than that and nearly every
//     dance track is on a whole number — the ±0.01 nudges do the rest;
//   - a pause, or a press far off the running tempo (a double press, a missed
//     beat), starts a new run instead of dragging the estimate.

export const MIN_TAPS = 4
const MAX_TAPS = 16
/** Longest gap between taps that still continues a run (30 BPM). */
export const RESET_MS = 2000
/** How far one interval may stray from the running period before it restarts. */
const OUTLIER = 0.35
/** Within this of a whole BPM, snap to it; otherwise to the nearest half. */
const WHOLE_SNAP = 0.3

export interface TapReading {
  /** Taps in the current run, including this one. */
  count: number
  /** Snapped BPM, or null until the run is long enough to trust. */
  bpm: number | null
}

/** Least-squares slope of times against index: the mean beat period, in ms. */
function period(taps: number[]): number {
  const n = taps.length
  const meanI = (n - 1) / 2
  const meanT = taps.reduce((a, b) => a + b, 0) / n
  let num = 0
  let den = 0
  for (let i = 0; i < n; i++) {
    num += (i - meanI) * (taps[i] - meanT)
    den += (i - meanI) ** 2
  }
  return num / den
}

export function snapBpm(bpm: number): number {
  const whole = Math.round(bpm)
  return Math.abs(bpm - whole) <= WHOLE_SNAP ? whole : Math.round(bpm * 2) / 2
}

export class TapTempo {
  private taps: number[] = []

  /** Register a press at `now` (ms, monotonic). */
  tap(now: number): TapReading {
    const taps = this.taps
    const last = taps[taps.length - 1]
    if (last !== undefined) {
      const gap = now - last
      const expected = taps.length >= 2 ? period(taps) : gap
      if (gap > RESET_MS || gap <= 0 || Math.abs(gap - expected) > expected * OUTLIER) {
        taps.length = 0
      }
    }
    taps.push(now)
    if (taps.length > MAX_TAPS) taps.shift()
    if (taps.length < MIN_TAPS) return { count: taps.length, bpm: null }
    return { count: taps.length, bpm: snapBpm(60000 / period(taps)) }
  }

  reset() {
    this.taps.length = 0
  }
}
