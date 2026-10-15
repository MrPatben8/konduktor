import { describe, expect, it } from 'vitest'

import { laneScales, makeSpans, rawSpans, rawWaveFromMono, RAW_BLOCK, tintAt } from './rawWave'
import type { WaveColumn } from './waveform'

const SR = 44100

/** `sec` of silence with a one-sample click of `amp` at sample `at`. */
function click(sec: number, at: number, amp = 0.8) {
  const mono = new Float32Array(Math.round(sec * SR))
  mono[at] = amp
  return rawWaveFromMono(mono, SR)
}

/** The bin whose centre time is nearest `sec`. */
const binAt = (s: ReturnType<typeof makeSpans>, sec: number) => {
  let best = 0
  for (let k = 0; k < s.n; k++) if (Math.abs(s.t[k] - sec) < Math.abs(s.t[best] - sec)) best = k
  return best
}

describe('rawWaveFromMono', () => {
  it('summarises min/max per block and finds the peak', () => {
    const mono = new Float32Array(RAW_BLOCK * 3)
    mono[5] = 0.5
    mono[RAW_BLOCK + 7] = -0.9
    const raw = rawWaveFromMono(mono, SR)
    expect(raw.min.length).toBe(3)
    expect(raw.max[0]).toBeCloseTo(0.5)
    expect(raw.min[1]).toBeCloseTo(-0.9)
    expect(raw.peak).toBeCloseTo(0.9)
  })
})

describe('rawSpans', () => {
  // A 0.25 s view across 1200 px: ~9 samples a pixel — the SAMPLE path.
  // A 16 s view: ~588 a pixel — the BLOCK path.
  for (const [label, view] of [['deep zoom', 0.25], ['wide zoom', 16]] as const) {
    it(`puts a click in the pixel that holds it (${label})`, () => {
      const at = 2 * SR + 123
      const raw = click(30, at)
      const w = 1200
      const start = at / SR - view / 2
      const s = rawSpans(raw, start, start + view, w, makeSpans())
      const k = binAt(s, at / SR)
      expect(s.hi[k]).toBeCloseTo(0.8)
      // Its pixel is where the playhead (the view's centre) is.
      expect(Math.abs(Math.round(s.x0 + k) - w / 2)).toBeLessThanOrEqual(1)
      // Far from it, silence.
      expect(s.hi[k + 20]).toBe(0)
      expect(s.hi[k - 20]).toBe(0)
    })
  }

  it('leaves bins outside the track empty', () => {
    const raw = click(1, 100)
    const s = rawSpans(raw, -0.5, 0.5, 1000, makeSpans())
    expect(Number.isNaN(s.lo[10])).toBe(true)
    expect(Number.isNaN(s.lo[900])).toBe(false)
  })

  it('draws a sine as one continuous line at deep zoom', () => {
    const mono = new Float32Array(SR)
    for (let i = 0; i < mono.length; i++) mono[i] = Math.sin((2 * Math.PI * 60 * i) / SR) * 0.7
    const s = rawSpans(rawWaveFromMono(mono, SR), 0.4, 0.65, 1200, makeSpans())
    for (let k = 1; k < s.n; k++) {
      // Each stroke overlaps the previous: no gap a line could fall through.
      expect(s.lo[k]).toBeLessThanOrEqual(s.hi[k - 1] + 1e-6)
      expect(s.hi[k]).toBeGreaterThanOrEqual(s.lo[k - 1] - 1e-6)
    }
  })

  it('interpolates when a pixel is narrower than a sample', () => {
    const mono = new Float32Array(SR)
    for (let i = 0; i < mono.length; i++) mono[i] = i / mono.length
    // 0.01 s across 4000 px: ~0.11 samples a pixel.
    const s = rawSpans(rawWaveFromMono(mono, SR), 0.5, 0.51, 4000, makeSpans())
    const k = 2000
    expect(s.lo[k]).toBeCloseTo(s.t[k], 3)
  })

  it('agrees with itself across the block/sample switch-over', () => {
    const mono = new Float32Array(10 * SR)
    for (let i = 0; i < mono.length; i++) mono[i] = Math.sin(i * 0.001) * Math.sin(i * 0.37) * 0.9
    const raw = rawWaveFromMono(mono, SR)
    const w = 1000
    // Just either side of 128 samples a pixel.
    const sample = rawSpans(raw, 1, 1 + (127 * w) / SR, w, makeSpans())
    const block = rawSpans(raw, 1, 1 + (129 * w) / SR, w, makeSpans())
    // At the same time, the block path's extent contains the sample path's
    // (it may smear by part of a block, never miss a peak).
    for (const k of [100, 400, 700]) {
      const j = binAt(block, sample.t[k])
      const near = [j - 1, j, j + 1]
      expect(Math.min(...near.map((i) => block.lo[i]))).toBeLessThanOrEqual(sample.lo[k] + 1e-6)
      expect(Math.max(...near.map((i) => block.hi[i]))).toBeGreaterThanOrEqual(sample.hi[k] - 1e-6)
    }
  })

  it('bins in track space, so scrolling does not regroup samples', () => {
    const mono = new Float32Array(4 * SR)
    for (let i = 0; i < mono.length; i++) mono[i] = Math.sin(i * 0.013) * Math.sin(i * 0.41)
    const raw = rawWaveFromMono(mono, SR)
    const w = 800
    const view = 8 // wide: block path
    const a = rawSpans(raw, 0.5, 0.5 + view, w, makeSpans())
    const lo = Array.from(a.lo.subarray(0, a.n))
    const shift = (view / w) * 0.37 // a fraction of a pixel
    const b = rawSpans(raw, 0.5 + shift, 0.5 + shift + view, w, makeSpans())
    expect(Array.from(b.lo.subarray(0, b.n))).toEqual(lo)
  })
})

describe('laneScales', () => {
  it('scales each stem by its own peak', () => {
    const [a, b] = laneScales([1, 0.5], 100)
    expect(a).toBeCloseTo(100)
    expect(b).toBeCloseTo(200)
  })
  it('never magnifies a near-silent stem past 5% of the loudest', () => {
    const [, quiet] = laneScales([1, 0.001], 100)
    expect(quiet).toBeCloseTo(100 / 0.05)
  })
})

describe('tintAt', () => {
  const col = (low: number, mid: number, high: number): WaveColumn => ({ peak: 1, low, mid, high })
  const rgb = (s: string) => s.slice(4, -1).split(',').map(Number)
  const saturation = (s: string) => Math.max(...rgb(s)) - Math.min(...rgb(s))
  it('keeps each band its bar colour', () => {
    const [r, g, b] = rgb(tintAt([col(1, 0, 0)], 1, 0.5))
    expect(r).toBeGreaterThan(g)
    expect(g).toBeGreaterThan(b) // orange
    const [r2, , b2] = rgb(tintAt([col(0, 0, 1)], 1, 0.5))
    expect(b2).toBeGreaterThan(r2) // cyan
  })
  it('never greys out where bands mix', () => {
    // A plain weighted mean of orange + violet + cyan spreads only 26-56 here.
    for (const mix of [col(1, 1, 1), col(1, 0, 1), col(0.6, 1, 0.4)]) {
      expect(saturation(tintAt([mix], 1, 0.5))).toBeGreaterThan(90)
    }
  })
})
