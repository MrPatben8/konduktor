import { describe, expect, it } from 'vitest'

import { BEATS_PER_BAR, buildBeatGrid, type GridMarker } from './beatgrid'

const grid = (markers: GridMarker[]) => {
  const g = buildBeatGrid(markers)
  if (!g) throw new Error('expected a grid')
  return g
}

// 120 BPM => 0.5 s per beat, which keeps the expected values exact in binary.
const CONST = [{ start: 1, bpm: 120 }]
// A tempo change at 10 s: 120 BPM (0.5 s/beat) then 60 BPM (1 s/beat).
const FLEX = [
  { start: 1, bpm: 120 },
  { start: 10, bpm: 60 },
]

describe('buildBeatGrid', () => {
  it('returns null when there is no grid', () => {
    expect(buildBeatGrid(undefined)).toBeNull()
    expect(buildBeatGrid(null)).toBeNull()
    expect(buildBeatGrid([])).toBeNull()
  })

  it('drops unusable markers and returns null if none survive', () => {
    expect(buildBeatGrid([{ start: 0, bpm: 0 }])).toBeNull()
    expect(buildBeatGrid([{ start: 0, bpm: NaN }])).toBeNull()
    expect(buildBeatGrid([{ start: Infinity, bpm: 120 }])).toBeNull()
  })

  it('sorts unsorted input and clamps negative starts', () => {
    const g = grid([
      { start: 10, bpm: 60 },
      { start: -5, bpm: 120 },
    ])
    expect(g.markers.map((m) => m.start)).toEqual([0, 10])
  })

  it('collapses markers at the same instant', () => {
    const g = grid([
      { start: 5, bpm: 120 },
      { start: 5, bpm: 140 },
    ])
    expect(g.count).toBe(1)
    expect(g.markers[0].bpm).toBe(140)
  })

  it('clamps absurd BPMs so beat duration is always finite and non-zero', () => {
    const g = grid([{ start: 0, bpm: 100000 }])
    expect(Number.isFinite(g.beatDurationAt(0))).toBe(true)
    expect(g.beatDurationAt(0)).toBeGreaterThan(0)
  })
})

describe('single marker behaves exactly as the old single-BPM math', () => {
  // This is the acceptance property for the whole marker-list model: 99.98% of
  // tracks have one marker and must be completely unaffected by it.
  const g = grid(CONST)
  const anchor = CONST[0].start
  const beat = 60 / CONST[0].bpm
  const oldSnap = (t: number) => Math.max(0, anchor + Math.round((t - anchor) / beat) * beat)

  it('matches the old snapTime across the track', () => {
    for (let t = 0; t < 120; t += 0.137) {
      expect(g.snapToBeat(t)).toBeCloseTo(oldSnap(t), 9)
    }
  })

  it('matches the old beat-jump arithmetic', () => {
    for (const n of [1, 4, 8, 32, -1, -4]) {
      expect(g.advanceBeats(20, n)).toBeCloseTo(20 + n * beat, 9)
    }
  })

  it('extrapolates backwards before the first marker, as before', () => {
    expect(g.beatIndexAt(anchor - beat)).toBeCloseTo(-1, 9)
    expect(g.markerIndexAt(0)).toBe(0)
  })

  it('clamps snapping at zero', () => {
    expect(g.snapToBeat(-99)).toBeGreaterThanOrEqual(0)
  })
})

describe('beat space is invertible', () => {
  for (const [label, markers] of [
    ['constant', CONST],
    ['flexible', FLEX],
  ] as const) {
    it(`timeOfBeat inverts beatIndexAt (${label})`, () => {
      const g = grid([...markers])
      for (let t = 0; t < 40; t += 0.233) {
        expect(g.timeOfBeat(g.beatIndexAt(t))).toBeCloseTo(t, 9)
      }
    })

    it(`advanceBeats is reversible (${label})`, () => {
      const g = grid([...markers])
      for (const n of [1, 4, 16, -3]) {
        for (const t of [2, 9.5, 10, 12.25, 30]) {
          expect(g.advanceBeats(g.advanceBeats(t, n), -n)).toBeCloseTo(t, 9)
        }
      }
    })

    it(`beatsBetween inverts advanceBeats (${label})`, () => {
      const g = grid([...markers])
      expect(g.beatsBetween(3, g.advanceBeats(3, 8))).toBeCloseTo(8, 9)
    })
  }
})

describe('flexible grid', () => {
  const g = grid(FLEX)

  it('reports the governing tempo per position', () => {
    expect(g.bpmAt(5)).toBe(120)
    expect(g.bpmAt(15)).toBe(60)
    // Exactly on a marker selects THAT marker, so the readout cannot flicker.
    expect(g.bpmAt(10)).toBe(60)
    // Before the first marker, marker 0 extrapolates backwards.
    expect(g.bpmAt(0)).toBe(120)
  })

  it('counts whole beats in a bounded segment', () => {
    // 1s -> 10s at 0.5 s/beat = 18 beats, so the second segment starts at 18.
    expect(g.segments[0].beatCount).toBe(18)
    expect(g.segments[1].baseIndex).toBe(18)
    expect(g.segments[1].beatCount).toBe(Infinity)
  })

  it('advances across a tempo change in beat space, not seconds', () => {
    // Two beats before the seam, then four beats forward: two at 0.5 s to reach
    // the marker, then two at 1 s past it.
    expect(g.advanceBeats(9, 4)).toBeCloseTo(12, 9)
  })

  it('keeps no beat closer than half a beat to the next marker', () => {
    // The Math.round beat-count rule is what guarantees snapping stays
    // unambiguous when a marker does not land on an exact beat.
    const off = grid([
      { start: 0, bpm: 120 },
      { start: 10.2, bpm: 60 },
    ])
    const seg = off.segments[0]
    const lastBeat = seg.start + (seg.beatCount - 1) * seg.beatDur
    expect(off.markers[1].start - lastBeat).toBeGreaterThan(seg.beatDur * 0.5)
  })

  it('restarts bar phase at each marker', () => {
    // Each marker is a downbeat of its own segment.
    expect(g.nearestBeat(1).beatInBar).toBe(0)
    expect(g.nearestBeat(10).beatInBar).toBe(0)
    // ...and phase counts up from there within the segment.
    expect(g.nearestBeat(12).beatInBar).toBe(2 % BEATS_PER_BAR)
  })

  it('snaps to a real beat near a marker boundary', () => {
    const snapped = g.snapToBeat(10.1)
    expect(Math.abs(g.beatIndexAt(snapped) - Math.round(g.beatIndexAt(snapped)))).toBeLessThan(1e-9)
  })
})

describe('marker lookup helpers', () => {
  const g = grid(FLEX)

  it('finds neighbouring markers for playhead navigation', () => {
    expect(g.nextMarkerIndex(5, 0.01)).toBe(1)
    expect(g.nextMarkerIndex(50, 0.01)).toBe(-1)
    expect(g.prevMarkerIndex(5, 0.01)).toBe(0)
    expect(g.prevMarkerIndex(0, 0.01)).toBe(-1)
  })

  it('detects standing on a marker within tolerance', () => {
    expect(g.isOnMarker(10.005, 0.01)).toBe(true)
    expect(g.isOnMarker(10.5, 0.01)).toBe(false)
  })

  it('returns only the segments overlapping a window', () => {
    expect(g.segmentsIn(0, 5).map((s) => s.index)).toEqual([0])
    expect(g.segmentsIn(20, 30).map((s) => s.index)).toEqual([1])
    expect(g.segmentsIn(5, 20).map((s) => s.index)).toEqual([0, 1])
  })

  it('lists markers inside a window', () => {
    expect(g.markersIn(0, 5)).toEqual([0])
    expect(g.markersIn(0, 20)).toEqual([0, 1])
    expect(g.markersIn(20, 30)).toEqual([])
  })
})
