/// <reference types="node" />
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { DeckCore, QUANTUM, WARM_QUANTA, type DeckSource } from './deckCore'
import { extrapolate } from './position'
import { RubberBand } from './stretcher'

const SR = 44100
const rb = () => new RubberBand(new WebAssembly.Module(readFileSync(new URL('./rubberband.wasm', import.meta.url))))

/** A mono track whose every sample is distinct: sample i = i / len. */
function ramp(len: number): DeckSource {
  const ch = new Float32Array(len)
  for (let i = 0; i < len; i++) ch[i] = i / len
  return { stems: [[ch]], length: len, sampleRate: SR }
}

function sine(freq: number, seconds: number, clicksEvery?: number): Float32Array {
  const len = Math.round(seconds * SR)
  const ch = new Float32Array(len)
  for (let i = 0; i < len; i++) ch[i] = 0.5 * Math.sin((2 * Math.PI * freq * i) / SR)
  if (clicksEvery) for (let i = 0; i < len; i += clicksEvery) ch[i] = 1
  return ch
}

/** Render `blocks` blocks; returns the left channel. */
function run(core: DeckCore, blocks: number): Float32Array {
  const out = new Float32Array(blocks * QUANTUM)
  const l = new Float32Array(QUANTUM)
  const r = new Float32Array(QUANTUM)
  for (let b = 0; b < blocks; b++) {
    core.render(l, r)
    out.set(l, b * QUANTUM)
  }
  return out
}

/** Frequency by counting upward zero crossings. */
function pitch(x: Float32Array): number {
  let n = 0
  for (let i = 1; i < x.length; i++) if (x[i - 1] < 0 && x[i] >= 0) n++
  return (n * SR) / x.length
}

describe('DeckCore transport', () => {
  it('plays the source sample for sample at rate 1', () => {
    const core = new DeckCore(SR, null)
    const src = ramp(SR)
    core.load(src)
    core.play()
    const out = run(core, 20)
    for (let i = 0; i < out.length; i++) expect(out[i]).toBeCloseTo(src.stems[0][0][i], 6)
    expect(core.positionSec).toBeCloseTo((20 * QUANTUM) / SR, 9)
  })

  it('is silent and still while paused, and resumes where it stopped', () => {
    const core = new DeckCore(SR, null)
    core.load(ramp(SR))
    core.play()
    run(core, 4)
    core.pause()
    const at = core.positionSec
    expect(run(core, 4).every((v) => v === 0)).toBe(true)
    expect(core.positionSec).toBe(at)
    core.play()
    expect(run(core, 1)[0]).toBeCloseTo((4 * QUANTUM) / SR, 6)
  })

  it('loops seamlessly between whole-sample bounds', () => {
    const core = new DeckCore(SR, null)
    const src = ramp(SR)
    core.load(src)
    const start = 1000
    const end = 1300
    core.seek(start / SR)
    core.setLoop(start / SR, end / SR, true)
    core.play()
    const out = run(core, 10)
    for (let i = 0; i < out.length; i++) {
      expect(out[i]).toBeCloseTo(src.stems[0][0][start + (i % (end - start))], 6)
    }
  })

  it('jumps into a loop engaged behind the playhead', () => {
    const core = new DeckCore(SR, null)
    core.load(ramp(SR))
    core.seek(0.5)
    core.setLoop(0.1, 0.2, true)
    expect(core.positionSec).toBeCloseTo(0.1, 9)
  })

  it('varispeed: rate 2 reads every other sample', () => {
    const core = new DeckCore(SR, null)
    const src = ramp(SR)
    core.load(src)
    core.setRate(2)
    core.play()
    const out = run(core, 5)
    for (let i = 0; i < out.length; i++) expect(out[i]).toBeCloseTo(src.stems[0][0][2 * i], 5)
  })

  it('reports the end of the track once and stops', () => {
    const core = new DeckCore(SR, null)
    core.load(ramp(QUANTUM * 3 + 10))
    core.play()
    const events: string[] = []
    for (let b = 0; b < 6; b++) {
      run(core, 1)
      events.push(...core.events)
    }
    expect(events).toEqual(['ended'])
    expect(core.playing).toBe(false)
  })

  it('plays stems in lockstep, and a mute ramps one out', () => {
    const a = sine(220, 1)
    const b = sine(330, 1)
    const core = new DeckCore(SR, null)
    core.load({ stems: [[a], [b]], length: a.length, sampleRate: SR })
    core.play()
    const both = run(core, 4)
    for (let i = 0; i < both.length; i++) expect(both[i]).toBeCloseTo(a[i] + b[i], 5)
    core.setGains([1, 0])
    run(core, 40) // the 4 ms ramp settles
    const at = Math.round(core.positionSec * SR)
    const solo = run(core, 4)
    for (let i = 0; i < solo.length; i++) expect(solo[i]).toBeCloseTo(a[at + i], 4)
  })
})

describe('DeckCore scratch', () => {
  it('follows the drag, then coasts back to playing speed and plays on by itself', () => {
    const core = new DeckCore(SR, null)
    core.load(ramp(SR * 4))
    core.play()
    run(core, 10)
    core.scratchBegin(1)
    expect(core.scratching).toBe(true)
    core.scratchTarget(1.2)
    run(core, 200)
    expect(core.positionSec).toBeGreaterThan(1.1)
    core.scratchRelease(true)
    const events: string[] = []
    for (let b = 0; b < 2000 && !events.length; b++) {
      run(core, 1)
      events.push(...core.events)
    }
    expect(events).toEqual(['scratchDone'])
    expect(core.playing).toBe(true)
    const before = core.positionSec
    run(core, 10)
    expect(core.positionSec - before).toBeCloseTo((10 * QUANTUM) / SR, 6)
  })

  it('a release without resume settles paused', () => {
    const core = new DeckCore(SR, null)
    core.load(ramp(SR * 4))
    core.scratchBegin(1)
    core.scratchTarget(1.5)
    run(core, 100)
    core.scratchRelease(false)
    for (let b = 0; b < 3000 && core.scratching; b++) run(core, 1)
    expect(core.scratching).toBe(false)
    expect(core.playing).toBe(false)
  })
})

describe('DeckCore keylock', () => {
  it('keeps the pitch while the tempo changes; plain varispeed does not', () => {
    const tone = sine(440, 6)
    const src: DeckSource = { stems: [[tone]], length: tone.length, sampleRate: SR }

    const plain = new DeckCore(SR, rb())
    plain.load(src)
    plain.setRate(1.1)
    plain.play()
    expect(pitch(run(plain, 300).subarray(50 * QUANTUM))).toBeCloseTo(484, -1)

    const locked = new DeckCore(SR, rb())
    locked.load(src)
    locked.setKeylock(true)
    locked.setRate(1.1)
    locked.play()
    const out = run(locked, 300)
    expect(locked.stretching).toBe(true)
    expect(pitch(out.subarray(50 * QUANTUM))).toBeCloseTo(440, -1)
    // The tempo still changed: the playhead ran at 1.1x.
    expect(locked.positionSec).toBeCloseTo((1.1 * 300 * QUANTUM) / SR, 6)
  })

  it('answers a seek at once (varispeed while priming), then hands over to the stretcher', () => {
    const tone = sine(440, 6)
    const core = new DeckCore(SR, rb())
    core.load({ stems: [[tone]], length: tone.length, sampleRate: SR })
    core.setKeylock(true)
    core.setRate(0.94)
    core.play()
    run(core, 100)
    core.seek(3)
    const first = run(core, 1)
    expect(Math.max(...first.map(Math.abs))).toBeGreaterThan(0.3) // sound in the very first block
    expect(core.stretching).toBe(false)
    run(core, WARM_QUANTA + 4)
    expect(core.stretching).toBe(true)
  })

  it('stretched output stays aligned with the playhead', () => {
    // A click every 0.5 s of SOURCE; at 1.05x with keylock each should be heard
    // where the playhead says it is, within a few ms.
    const every = SR / 2
    const tone = sine(110, 8, every).map((v, i) => (i % every === 0 ? 1 : v * 0.1))
    const core = new DeckCore(SR, rb())
    core.load({ stems: [[tone]], length: tone.length, sampleRate: SR })
    core.setKeylock(true)
    core.setRate(1.05)
    core.play()
    const blocks = 1200
    const out = new Float32Array(blocks * QUANTUM)
    const posAt = new Float64Array(blocks)
    const l = new Float32Array(QUANTUM)
    const r = new Float32Array(QUANTUM)
    for (let b = 0; b < blocks; b++) {
      posAt[b] = core.positionSec
      core.render(l, r)
      out.set(l, b * QUANTUM)
    }
    // Expected output frame of the click at source second s: the block whose
    // playhead passes s, plus the fraction within it.
    const errors: number[] = []
    for (let s = 1; s < 3; s += 0.5) {
      const b = posAt.findIndex((p) => p > s) - 1
      const expected = b * QUANTUM + ((s - posAt[b]) * SR) / 1.05
      let best = 0
      let at = 0
      for (let i = Math.round(expected - 2000); i < expected + 2000; i++) {
        if (Math.abs(out[i]) > best) {
          best = Math.abs(out[i])
          at = i
        }
      }
      errors.push(((at - expected) / SR) * 1000)
    }
    for (const e of errors) expect(Math.abs(e)).toBeLessThan(6)
  })

  it('every stem gets its own stretcher and a mute stays instant', () => {
    const a = sine(220, 4)
    const b = sine(660, 4)
    const core = new DeckCore(SR, rb())
    core.load({ stems: [[a], [b]], length: a.length, sampleRate: SR })
    core.setKeylock(true)
    core.setRate(1.08)
    core.play()
    run(core, 60)
    expect(core.stretching).toBe(true)
    core.setGains([1, 0])
    run(core, 40)
    expect(pitch(run(core, 200))).toBeCloseTo(220, -1)
  })

  it('back at rate 1 the stretcher is bypassed', () => {
    const tone = sine(440, 4)
    const core = new DeckCore(SR, rb())
    core.load({ stems: [[tone]], length: tone.length, sampleRate: SR })
    core.setKeylock(true)
    core.setRate(1.05)
    core.play()
    run(core, 60)
    expect(core.stretching).toBe(true)
    core.setRate(1)
    run(core, 2)
    expect(core.stretching).toBe(false)
    const at = Math.round(core.positionSec * SR)
    const out = run(core, 4)
    for (let i = 0; i < out.length; i++) expect(out[i]).toBeCloseTo(tone[at + i], 5)
  })
})

describe('extrapolate', () => {
  const noLoop = { start: 0, end: 0, on: false }
  it('holds still while paused', () => {
    expect(extrapolate({ pos: 5, time: 1, speed: 1, playing: false, scratching: false }, 3, noLoop, 100)).toBe(5)
  })
  it('advances by the speed and clamps at the end', () => {
    const m = { pos: 5, time: 1, speed: 1.1, playing: true, scratching: false }
    expect(extrapolate(m, 3, noLoop, 100)).toBeCloseTo(7.2, 9)
    expect(extrapolate(m, 300, noLoop, 100)).toBe(100)
  })
  it('wraps inside an engaged loop', () => {
    const m = { pos: 9, time: 0, speed: 1, playing: true, scratching: false }
    expect(extrapolate(m, 2.5, { start: 8, end: 10, on: true }, 100)).toBeCloseTo(9.5, 9)
  })
})
