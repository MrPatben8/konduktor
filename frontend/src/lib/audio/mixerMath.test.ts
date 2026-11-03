import { describe, expect, it } from 'vitest'
import { echoBeats, echoSeconds, eqGain, faderGain, filterSweep, gainToDb, xfadeGains } from './mixerMath'

describe('eqGain', () => {
  it('is flat at the centre, +6 dB at full right', () => {
    expect(eqGain(0, false)).toBe(1)
    expect(gainToDb(eqGain(1, false))).toBeCloseTo(6, 6)
    expect(gainToDb(eqGain(0.5, true))).toBeCloseTo(3, 6)
  })
  it('cuts to -26 dB, DJM-style', () => {
    expect(gainToDb(eqGain(-1, false))).toBeCloseTo(-26, 6)
    expect(gainToDb(eqGain(-0.5, false))).toBeCloseTo(-13, 6)
  })
  it('kills to silence over the last fifth, continuously', () => {
    expect(eqGain(-1, true)).toBe(0)
    expect(eqGain(-0.8, true)).toBeCloseTo(eqGain(-0.8, false), 9)
    expect(eqGain(-0.5, true)).toBe(eqGain(-0.5, false))
    let prev = Infinity
    for (let v = -0.7; v >= -1; v -= 0.01) {
      const g = eqGain(v, true)
      expect(g).toBeLessThanOrEqual(prev)
      prev = g
    }
  })
})

describe('xfadeGains', () => {
  it('smooth: constant power, -3 dB each at the centre', () => {
    for (const x of [-1, -0.4, 0, 0.3, 1]) {
      const [a, b] = xfadeGains(x, 'smooth')
      expect(a * a + b * b).toBeCloseTo(1, 9)
    }
    expect(gainToDb(xfadeGains(0, 'smooth')[0])).toBeCloseTo(-3.01, 2)
    expect(xfadeGains(-1, 'smooth')).toEqual([1, expect.closeTo(0, 9)])
  })
  it('sharp: both full until the last tenth', () => {
    expect(xfadeGains(0, 'sharp')).toEqual([1, 1])
    expect(xfadeGains(0.9, 'sharp')).toEqual([1, 1])
    expect(xfadeGains(1, 'sharp')).toEqual([0, 1])
    expect(xfadeGains(-0.95, 'sharp')[1]).toBeCloseTo(0.5, 9)
  })
})

describe('faderGain', () => {
  it('runs 0 → 1 on an audio taper', () => {
    expect(faderGain(0)).toBe(0)
    expect(faderGain(1)).toBe(1)
    expect(gainToDb(faderGain(0.7071))).toBeCloseTo(-6.02, 1)
  })
})

describe('filterSweep', () => {
  it('is bypassed at the centre', () => {
    expect(filterSweep(0)).toBeNull()
    expect(filterSweep(0.01)).toBeNull()
  })
  it('low-passes left and high-passes right, on a log scale', () => {
    expect(filterSweep(-1)).toEqual({ type: 'lowpass', freq: expect.closeTo(150, 6) })
    expect(filterSweep(1)).toEqual({ type: 'highpass', freq: expect.closeTo(8000, 6) })
    const half = filterSweep(-0.5)!
    expect(half.freq).toBeCloseTo(Math.sqrt(20000 * 150), 3)
  })
})

describe('echo', () => {
  it('picks its length in beats from the parameter and follows the tempo', () => {
    expect(echoBeats(0)).toBe(1 / 8)
    expect(echoBeats(0.5)).toBe(1 / 2)
    expect(echoBeats(1)).toBe(1)
    expect(echoSeconds(1, 120)).toBeCloseTo(0.5, 9)
    expect(echoSeconds(0.5, 128)).toBeCloseTo(60 / 128 / 2, 9)
    expect(echoSeconds(1, 0)).toBeCloseTo(0.5, 9) // unknown tempo: 120
  })
})
