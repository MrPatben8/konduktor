import { describe, expect, it } from 'vitest'
import { buildBeatGrid } from './beatgrid'
import { alignedPosition, bendFor, effectiveBpm, octaveFor, phaseOffset, rangeFor, syncRate, type DeckTempo } from './sync'

const grid = (start: number, bpm: number) => buildBeatGrid([{ start, bpm }])
const deck = (g: ReturnType<typeof buildBeatGrid>, pos: number, rate = 1): DeckTempo => ({ grid: g, pos, rate })

describe('tempo', () => {
  it('effective BPM is the grid tempo times the rate', () => {
    expect(effectiveBpm(deck(grid(0, 128), 10, 1.02))).toBeCloseTo(130.56, 9)
    expect(effectiveBpm(deck(null, 10))).toBeNull()
  })

  it('matches a follower to the master', () => {
    const r = syncRate(deck(grid(0, 128), 5, 1.01), deck(grid(0, 124), 5))!
    expect(r.octave).toBe(1)
    expect(124 * r.rate).toBeCloseTo(128 * 1.01, 9)
  })

  it('syncs half and double time at the nearer octave', () => {
    expect(octaveFor(174, 87)).toBe(2)
    expect(octaveFor(87, 172)).toBe(0.5)
    expect(octaveFor(128, 126)).toBe(1)
    const r = syncRate(deck(grid(0, 174), 0), deck(grid(0, 86), 0))!
    expect(r.octave).toBe(2)
    expect(r.rate).toBeCloseTo(174 / 172, 9)
  })

  it('a flexible grid syncs to the segment under the playhead', () => {
    const flex = buildBeatGrid([
      { start: 0, bpm: 120 },
      { start: 30, bpm: 125 },
    ])
    expect(syncRate(deck(grid(0, 125), 0), deck(flex, 10))!.rate).toBeCloseTo(125 / 120, 9)
    expect(syncRate(deck(grid(0, 125), 0), deck(flex, 40))!.rate).toBeCloseTo(1, 9)
  })
})

describe('phase', () => {
  const beat = 60 / 120 // 0.5 s
  it('is zero on the beat and signed within half a beat', () => {
    const m = deck(grid(0, 120), 4 * beat)
    expect(phaseOffset(m, deck(grid(0.1, 120), 0.1 + 7 * beat), 1)).toBeCloseTo(0, 9)
    expect(phaseOffset(m, deck(grid(0, 120), 4 * beat + 0.25 * beat), 1)).toBeCloseTo(0.25, 9) // follower ahead
    expect(phaseOffset(m, deck(grid(0, 120), 4 * beat - 0.1 * beat), 1)).toBeCloseTo(-0.1, 9)
    expect(phaseOffset(m, deck(grid(0, 120), 4 * beat + 0.6 * beat), 1)).toBeCloseTo(-0.4, 9)
  })

  it('aligning moves the follower by the smallest amount onto the master’s beat', () => {
    const m = deck(grid(0, 120), 10 * beat + 0.3 * beat)
    const f = deck(grid(0.2, 128), 50)
    const off = phaseOffset(m, f, 1)!
    const at = alignedPosition(f, off, 1)
    expect(phaseOffset(m, { ...f, pos: at }, 1)).toBeCloseTo(0, 9)
    expect(Math.abs(at - 50)).toBeLessThanOrEqual(0.5 * (60 / 128) + 1e-9)
  })

  it('compares in master beats at double time', () => {
    // Follower at 87 felt as 174: half a follower beat is a whole master beat.
    const m = deck(grid(0, 174), 0)
    const half = 0.5 * (60 / 87)
    expect(phaseOffset(m, deck(grid(0, 87), half), 2)).toBeCloseTo(0, 9)
    const f = deck(grid(0, 87), half + 0.1 * (60 / 87))
    const off = phaseOffset(m, f, 2)!
    expect(off).toBeCloseTo(0.2, 9)
    expect(phaseOffset(m, { ...f, pos: alignedPosition(f, off, 2) }, 2)).toBeCloseTo(0, 9)
  })
})

describe('rangeFor', () => {
  it('widens the pitch range only as far as needed', () => {
    expect(rangeFor(1.05, 0.08)).toBe(0.08)
    expect(rangeFor(1.12, 0.08)).toBe(0.16)
    expect(rangeFor(0.7, 0.08)).toBe(0.5)
    expect(rangeFor(1.05, 0.5)).toBe(0.5)
  })
})

describe('bendFor', () => {
  it('is none at rest, ±5% at full drag, finer with Shift, squared in between', () => {
    expect(bendFor(0, false)).toBe(1)
    expect(bendFor(80, false)).toBeCloseTo(1.05, 9)
    expect(bendFor(-200, false)).toBeCloseTo(0.95, 9)
    expect(bendFor(40, false)).toBeCloseTo(1.0125, 9)
    expect(bendFor(80, true)).toBeCloseTo(1.0125, 9)
  })
})
