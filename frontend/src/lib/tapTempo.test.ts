import { describe, expect, it } from 'vitest'

import { MIN_TAPS, TapTempo, snapBpm } from './tapTempo'

/** Tap `n` beats at `bpm`, each press offset by the matching `jitter` ms. */
function run(bpm: number, n: number, jitter: number[] = []) {
  const t = new TapTempo()
  const ms = 60000 / bpm
  let last = { count: 0, bpm: null as number | null }
  for (let i = 0; i < n; i++) last = t.tap(1000 + i * ms + (jitter[i % jitter.length] ?? 0))
  return last
}

describe('snapBpm', () => {
  it('snaps near-whole values to the whole BPM', () => {
    expect(snapBpm(124.8)).toBe(125)
    expect(snapBpm(125.29)).toBe(125)
  })
  it('falls back to the nearest half between wholes', () => {
    expect(snapBpm(127.45)).toBe(127.5)
    expect(snapBpm(127.62)).toBe(127.5)
  })
})

describe('TapTempo', () => {
  it('reports nothing until enough taps', () => {
    expect(run(125, MIN_TAPS - 1).bpm).toBeNull()
    expect(run(125, MIN_TAPS).bpm).toBe(125)
  })

  it('averages out human jitter', () => {
    // ±25 ms per press: any single interval here is off by up to ~6 BPM.
    expect(run(126, 12, [0, 25, -20, 10, -25, 15, -5, 20]).bpm).toBe(126)
  })

  it('restarts after a pause', () => {
    const t = new TapTempo()
    for (let i = 0; i < 5; i++) t.tap(i * 500)
    expect(t.tap(5 * 500 + 3000).count).toBe(1)
  })

  it('restarts on a press far off the running tempo', () => {
    const t = new TapTempo()
    for (let i = 0; i < 5; i++) t.tap(i * 500)
    expect(t.tap(4 * 500 + 120).count).toBe(1) // double press
  })
})
