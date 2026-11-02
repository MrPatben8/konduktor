// One deck's audio, rendered a block at a time: the body of the deck's
// AudioWorklet (`deckProcessor.worklet.ts`), kept free of worklet globals so it
// can be driven directly in tests.
//
// ONE chain does everything the deck plays: transport, seamless loops, the
// stems in lockstep (one position for all of them), mute/solo as ramped gains,
// varispeed (the pitch fader), keylock and scratch. It replaced an
// AudioBufferSourceNode for playback plus a ScriptProcessorNode on a SECOND
// AudioContext for scratch: those two could not share a context (the scratch
// node went silent once a source had played), so there was no single place a
// mixer could sit.
//
// Positions are SOURCE samples (the decoded audio's own rate); a step of
// `rate * srcRate / ctxRate` source samples per output frame plays at `rate`.
//
// KEYLOCK runs the stems through Rubber Band (R2, real time) whenever the rate
// is not 1. A stretcher has to be primed before its output lines up with the
// playhead, and priming four stems at once would blow one block's budget — a
// dropout on every hotcue. So after a seek (or play, loop jump, scratch
// hand-back, rate change while priming) the deck plays the plain varispeed path
// at once, primes the stretchers over the next blocks aimed at where the
// playhead WILL be, and crossfades over one block when the stretched output is
// aligned. Near rate 1 those few ms of varispeed are inaudible.

import { MAX_BLOCK, type RubberBand, Stretcher } from './stretcher'

/** An AudioWorklet's block length. */
export const QUANTUM = 128

export interface DeckSource {
  /** Per stem (a plain track is one), per channel (1 or 2). */
  stems: Float32Array[][]
  /** Frames, the same for every stem. */
  length: number
  sampleRate: number
}

export type DeckEvent = 'ended' | 'scratchDone'

type Mode = 'stopped' | 'playing' | 'drag' | 'inertia'
type StretchState = 'off' | 'warming' | 'on'

// Scratch feel — the values the ScriptProcessor scratch engine was tuned with.
const GLIDE = 400 // samples: how tightly position chases the target while dragging
const RATE_CAP = 12 // max source samples per output frame (pitch ceiling)
const INERTIA_FRICTION = 0.9998 // per-frame easing toward the target velocity (~0.4 s)
const SETTLE_EPS = 0.02 // |velocity - target| below this = settled

/** Blocks of varispeed while the stretchers prime (~29 ms at 44.1 kHz). */
export const WARM_QUANTA = 10
/** Input frames fed per stem per block while priming — keeps a 4-stem prime
 *  well inside a block's budget beside the other deck. */
const WARM_BUDGET = 384
/** Stem mute/solo ramp (s). */
const GAIN_TAU = 0.004

export class DeckCore {
  private stems: Float32Array[][] = []
  private len = 0
  private srcRate = 44100

  private mode: Mode = 'stopped'
  /** Playhead, in source samples. */
  private pos = 0
  private rate = 1
  private keylock = false

  private loopOn = false
  private loopStart = 0
  private loopEnd = 0
  private loopLen = 0
  /** loopLen rounded, for whole-sample index maps. */
  private loopLenI = 0

  private gainTarget: number[] = []
  private gainNow: number[] = []
  private readonly gainCoef: number
  /** Per stem, this block's per-frame gains (shared by both paths in a crossfade). */
  private gainBuf: Float32Array[] = []

  // Scratch (source samples, and source samples per output frame).
  private target = 0
  private velocity = 0
  private targetVelocity = 0
  private resume = false

  // Keylock.
  private stretchers: Stretcher[] = []
  private sState: StretchState = 'off'
  private needRewarm = false
  private warmRate = 0
  /** Next pre-roll frame to feed while priming (may be < 0: silence). */
  private feedPos = 0
  private primeLeft = 0
  private discardLeft = 0
  /** Per stem: next source frame to feed (loop-wrapped). */
  private readPtr: number[] = []
  /** Source position of the stretchers' next output frame, while priming. */
  private outPos = 0

  private readonly inTmp = [new Float32Array(MAX_BLOCK), new Float32Array(MAX_BLOCK)]
  private readonly dropTmp = [new Float32Array(MAX_BLOCK), new Float32Array(MAX_BLOCK)]
  private outTmp: Float32Array[][] = []
  private readonly xL = new Float32Array(QUANTUM)
  private readonly xR = new Float32Array(QUANTUM)

  /** Things that happened during the last render, for the processor to report. */
  readonly events: DeckEvent[] = []

  readonly ctxRate: number
  private readonly rb: RubberBand | null

  constructor(ctxRate: number, rb: RubberBand | null) {
    this.ctxRate = ctxRate
    this.rb = rb
    this.gainCoef = Math.exp(-1 / (GAIN_TAU * ctxRate))
  }

  // ---- state -----------------------------------------------------------

  get loaded(): boolean {
    return this.len > 0
  }
  get positionSec(): number {
    return this.pos / this.srcRate
  }
  get playing(): boolean {
    return this.mode === 'playing'
  }
  get scratching(): boolean {
    return this.mode === 'drag' || this.mode === 'inertia'
  }
  /** Track seconds per second right now. */
  get speed(): number {
    if (this.mode === 'playing') return this.rate
    if (this.scratching) return (this.velocity * this.ctxRate) / this.srcRate
    return 0
  }
  /** Is keylock's stretched output what is heard? (tests) */
  get stretching(): boolean {
    return this.sState === 'on'
  }
  private get step(): number {
    return (this.rate * this.srcRate) / this.ctxRate
  }

  // ---- commands --------------------------------------------------------

  load(src: DeckSource): void {
    this.disposeStretchers()
    this.stems = src.stems
    this.len = src.length
    this.srcRate = src.sampleRate
    this.mode = 'stopped'
    this.pos = 0
    this.loopOn = false
    this.gainTarget = src.stems.map(() => 1)
    this.gainNow = src.stems.map(() => 1)
    this.gainBuf = src.stems.map(() => new Float32Array(QUANTUM))
    this.outTmp = src.stems.map(() => [new Float32Array(QUANTUM), new Float32Array(QUANTUM)])
    this.readPtr = src.stems.map(() => 0)
  }

  play(): void {
    if (!this.len) return
    if (this.pos >= this.len - 0.01 * this.srcRate) this.pos = 0 // restart from the top at the end
    this.mode = 'playing'
    this.needRewarm = true
  }

  pause(): void {
    if (this.mode === 'playing') this.mode = 'stopped'
    this.sState = 'off'
  }

  seek(sec: number): void {
    this.pos = Math.max(0, Math.min(sec * this.srcRate, this.len))
    this.needRewarm = true
  }

  setLoop(startSec: number, endSec: number, enabled: boolean): void {
    const wasOn = this.loopOn
    this.loopStart = startSec * this.srcRate
    this.loopEnd = endSec * this.srcRate
    this.loopLen = this.loopEnd - this.loopStart
    this.loopLenI = Math.max(1, Math.round(this.loopLen))
    this.loopOn = enabled && this.loopLen > 0
    // Already past the new end: jump into it (as an AudioBufferSourceNode did).
    if (this.loopOn && this.pos >= this.loopEnd) {
      this.pos = this.loopStart
      this.needRewarm = true
    }
    if (this.sState !== 'off') {
      // The stretchers were fed AHEAD of the playhead. Re-prime if that input
      // no longer matches the new loop: fed past the new end, or wrapped by a
      // loop that is now gone.
      const wrapped = this.readPtr.some((r) => r < this.pos)
      if (this.loopOn ? wrapped || this.readPtr.some((r) => r >= this.loopEnd) : wasOn && wrapped) {
        this.needRewarm = true
      }
    }
  }

  setGains(levels: number[]): void {
    this.gainTarget = this.stems.map((_, i) => levels[i] ?? 1)
  }

  setRate(rate: number): void {
    this.rate = Math.max(0.05, rate)
    if (this.sState === 'on') for (const s of this.stretchers) s.setRatios(1 / this.step, this.pitchScale)
  }

  setKeylock(on: boolean): void {
    this.keylock = on
  }

  scratchBegin(sec: number): void {
    if (!this.len) return
    this.mode = 'drag'
    this.pos = Math.max(0, Math.min(sec * this.srcRate, this.len - 1))
    this.target = this.pos
    this.velocity = 0
    this.sState = 'off'
  }

  /** Re-grab while the platter is still coasting: resume dragging from here. */
  scratchRegrab(): void {
    if (!this.scratching) return
    this.mode = 'drag'
    this.target = this.pos
  }

  scratchTarget(sec: number): void {
    this.target = Math.max(0, Math.min(this.len - 1, sec * this.srcRate))
  }

  /** Release the platter: coast toward playing speed (resume) or a stop. The
   *  deck then plays on (or stays paused) BY ITSELF — no hand-back round trip,
   *  so there is no gap between the coast and the music. */
  scratchRelease(resume: boolean): void {
    if (!this.scratching) return
    this.resume = resume
    this.targetVelocity = resume ? this.step : 0
    this.mode = 'inertia'
  }

  dispose(): void {
    this.disposeStretchers()
    this.stems = []
    this.len = 0
  }

  // ---- rendering ---------------------------------------------------------

  /** Render one block into `outL` / `outR` (QUANTUM frames). */
  render(outL: Float32Array, outR: Float32Array): void {
    const n = outL.length
    this.events.length = 0
    if (!this.len || this.mode === 'stopped') {
      outL.fill(0)
      outR.fill(0)
      this.advanceGains(n)
      return
    }
    this.advanceGains(n)
    if (this.scratching) {
      this.renderScratch(outL, outR, n)
      return
    }

    const wantStretch = this.keylock && this.rb != null && Math.abs(this.rate - 1) > 1e-6
    if (!wantStretch) {
      this.needRewarm = false
      if (this.sState === 'on') {
        // Leave the stretched path: crossfade it out over this block.
        this.renderStretched(this.xL, this.xR, n)
        this.renderDirect(outL, outR, n)
        crossfade(this.xL, this.xR, outL, outR, n)
      } else {
        this.renderDirect(outL, outR, n)
      }
      this.sState = 'off'
    } else {
      if (this.needRewarm || this.sState === 'off' || (this.sState === 'warming' && this.rate !== this.warmRate)) {
        this.startWarm()
      }
      this.needRewarm = false
      if (this.sState === 'warming') {
        if (this.warmStep()) {
          this.renderStretched(this.xL, this.xR, n)
          this.renderDirect(outL, outR, n)
          crossfade(outL, outR, this.xL, this.xR, n)
          outL.set(this.xL)
          outR.set(this.xR)
          this.sState = 'on'
        } else {
          this.renderDirect(outL, outR, n)
        }
      } else {
        this.renderStretched(outL, outR, n)
        this.pos = this.wrap(this.pos + this.step * n)
      }
    }

    if (!this.loopOn && this.pos >= this.len) {
      this.pos = this.len
      this.mode = 'stopped'
      this.sState = 'off'
      this.events.push('ended')
    }
  }

  private advanceGains(n: number): void {
    const a = this.gainCoef
    for (let s = 0; s < this.gainBuf.length; s++) {
      const buf = this.gainBuf[s]
      const t = this.gainTarget[s]
      let g = this.gainNow[s]
      for (let i = 0; i < n; i++) {
        g = t + (g - t) * a
        buf[i] = g
      }
      this.gainNow[s] = g
    }
  }

  private wrap(p: number): number {
    if (this.loopOn && p >= this.loopEnd) p = this.loopStart + ((p - this.loopStart) % this.loopLen)
    return p
  }

  /** Sample k of channel `ch`; past a loop's end reads from its start. */
  private at(ch: Float32Array, k: number, looped: boolean): number {
    if (looped && k >= this.loopEnd) k -= this.loopLenI
    return k >= 0 && k < this.len ? ch[k] : 0
  }

  /** Cubic (Hermite) read at fractional position p. */
  private read(ch: Float32Array, p: number, looped: boolean): number {
    const i = Math.floor(p)
    const f = p - i
    const xm1 = this.at(ch, i - 1, looped)
    const x0 = this.at(ch, i, looped)
    const x1 = this.at(ch, i + 1, looped)
    const x2 = this.at(ch, i + 2, looped)
    const c1 = 0.5 * (x1 - xm1)
    const c2 = xm1 - 2.5 * x0 + 2 * x1 - 0.5 * x2
    const c3 = 0.5 * (x2 - xm1) + 1.5 * (x0 - x1)
    return ((c3 * f + c2) * f + c1) * f + x0
  }

  /** Mix every stem at position p, frame i of the block. */
  private frame(p: number, i: number, looped: boolean, outL: Float32Array, outR: Float32Array): void {
    let l = 0
    let r = 0
    for (let s = 0; s < this.stems.length; s++) {
      const g = this.gainBuf[s][i]
      if (g < 1e-5) continue
      const chs = this.stems[s]
      const vl = this.read(chs[0], p, looped)
      l += vl * g
      r += (chs.length > 1 ? this.read(chs[1], p, looped) : vl) * g
    }
    outL[i] = l
    outR[i] = r
  }

  /** The varispeed path; advances the playhead. */
  private renderDirect(outL: Float32Array, outR: Float32Array, n: number): void {
    const step = this.step
    for (let i = 0; i < n; i++) {
      if (!this.loopOn && this.pos >= this.len) {
        outL[i] = 0
        outR[i] = 0
        continue
      }
      this.frame(this.pos, i, this.loopOn, outL, outR)
      this.pos += step
      if (this.loopOn && this.pos >= this.loopEnd) this.pos -= this.loopLen
    }
  }

  private renderScratch(outL: Float32Array, outR: Float32Array, n: number): void {
    for (let i = 0; i < n; i++) {
      if (this.mode === 'drag') {
        const diff = this.target - this.pos
        if (Math.abs(diff) < 1) {
          // Hand at rest on the platter: no movement, no sound.
          this.velocity = 0
          outL[i] = 0
          outR[i] = 0
          continue
        }
        let step = diff / GLIDE
        if (step > RATE_CAP) step = RATE_CAP
        else if (step < -RATE_CAP) step = -RATE_CAP
        this.pos += step
        this.velocity = this.velocity * 0.9 + step * 0.1 // smoothed, for release momentum
      } else if (this.mode === 'inertia') {
        this.velocity = this.targetVelocity + (this.velocity - this.targetVelocity) * INERTIA_FRICTION
        this.pos += this.velocity
        if (Math.abs(this.velocity - this.targetVelocity) < SETTLE_EPS) this.settle()
      } else {
        // Settled mid-block: the rest of it plays (or stays silent) normally.
        if (this.mode === 'playing') {
          this.frame(this.pos, i, false, outL, outR)
          this.pos += this.step
        } else {
          outL[i] = 0
          outR[i] = 0
        }
        continue
      }
      // Clamp to the track; hitting an edge kills momentum.
      if (this.pos < 0) {
        this.pos = 0
        this.velocity = 0
        if (this.mode === 'inertia') this.settle()
      } else if (this.pos > this.len - 1) {
        this.pos = this.len - 1
        this.velocity = 0
        if (this.mode === 'inertia') this.settle()
      }
      this.frame(this.pos, i, false, outL, outR)
    }
  }

  private settle(): void {
    this.mode = this.resume ? 'playing' : 'stopped'
    this.velocity = 0
    this.needRewarm = true
    this.events.push('scratchDone')
  }

  // ---- keylock -------------------------------------------------------------

  private get pitchScale(): number {
    return this.srcRate / this.ctxRate
  }

  private ensureStretchers(): void {
    if (this.stretchers.length === this.stems.length || !this.rb) return
    this.disposeStretchers()
    this.stretchers = this.stems.map(() => new Stretcher(this.rb!, this.srcRate, 2))
  }

  private disposeStretchers(): void {
    for (const s of this.stretchers) s.dispose()
    this.stretchers = []
    this.sState = 'off'
  }

  /** Aim the stretchers at where the playhead will be WARM_QUANTA blocks from
   *  now, and start priming them. */
  private startWarm(): void {
    this.ensureStretchers()
    const step = this.step
    for (const s of this.stretchers) {
      s.reset()
      s.setRatios(1 / step, this.pitchScale)
    }
    const s0 = this.stretchers[0]
    const q = Math.round(this.wrap(this.pos + WARM_QUANTA * QUANTUM * step))
    this.primeLeft = s0.startPad
    this.feedPos = q - this.primeLeft
    this.discardLeft = s0.startDelay
    this.readPtr = this.stems.map(() => q)
    this.outPos = q
    this.warmRate = this.rate
    this.sState = 'warming'
  }

  /** One block's priming work. True when the stretched output is aligned with
   *  the playhead and a block of it is ready. */
  private warmStep(): boolean {
    const s0 = this.stretchers[0]
    let budget = WARM_BUDGET
    for (let guard = 0; guard < 64; guard++) {
      if (this.primeLeft > 0) {
        if (budget <= 0) return false
        const k = Math.min(this.primeLeft, budget, MAX_BLOCK)
        this.feedPreroll(k)
        this.primeLeft -= k
        budget -= k
        continue
      }
      const avail = s0.available
      if (this.discardLeft > 0) {
        if (avail > 0) {
          this.drop(Math.min(avail, this.discardLeft, MAX_BLOCK))
          this.discardLeft -= Math.min(avail, this.discardLeft, MAX_BLOCK)
          continue
        }
      } else {
        // Primed: the next output frame plays source position `outPos`.
        const lag = this.lagFrames()
        if (lag > 0) {
          // The playhead has passed it (priming ran long): skip ahead.
          if (avail > 0) {
            const k = Math.min(avail, lag, MAX_BLOCK)
            this.drop(k)
            this.outPos = this.wrap(this.outPos + k * this.step)
            continue
          }
        } else if (lag < 0) {
          return false // primed early: wait for the playhead to arrive
        } else if (avail >= QUANTUM) {
          return true
        }
      }
      if (budget <= 0) return false
      const k = Math.min(Math.max(s0.required, 1), budget, MAX_BLOCK)
      this.feedLooped(k)
      budget -= k
    }
    return false
  }

  /** Whole output frames from `outPos` forward to the playhead (negative =
   *  `outPos` is still ahead). Modulo the loop when one is engaged. */
  private lagFrames(): number {
    const step = this.step
    let d = this.pos - this.outPos
    if (this.loopOn && this.loopLen > 0) {
      d = ((d % this.loopLen) + this.loopLen) % this.loopLen
      if (d > this.loopLen / 2) d -= this.loopLen
    }
    return Math.abs(d) < step ? 0 : Math.round(d / step)
  }

  private drop(k: number): void {
    for (const s of this.stretchers) s.retrieve(this.dropTmp, k)
  }

  /** Pre-roll before the start point: straight, zeros outside the track. */
  private feedPreroll(k: number): void {
    const [tl, tr] = this.inTmp
    for (let s = 0; s < this.stems.length; s++) {
      const chs = this.stems[s]
      for (let j = 0; j < k; j++) {
        const idx = this.feedPos + j
        const ok = idx >= 0 && idx < this.len
        tl[j] = ok ? chs[0][idx] : 0
        tr[j] = ok ? chs[chs.length > 1 ? 1 : 0][idx] : 0
      }
      this.stretchers[s].process(this.inTmp, k)
    }
    this.feedPos += k
  }

  /** Feed stem s `k` frames from its read pointer, wrapping at the loop. */
  private feedStem(s: number, k: number): void {
    const [tl, tr] = this.inTmp
    const chs = this.stems[s]
    const right = chs[chs.length > 1 ? 1 : 0]
    let idx = this.readPtr[s]
    for (let j = 0; j < k; j++) {
      if (this.loopOn && idx >= this.loopEnd) idx -= this.loopLenI
      const ok = idx >= 0 && idx < this.len
      tl[j] = ok ? chs[0][idx] : 0
      tr[j] = ok ? right[idx] : 0
      idx++
    }
    if (this.loopOn && idx >= this.loopEnd) idx -= this.loopLenI
    this.readPtr[s] = idx
    this.stretchers[s].process(this.inTmp, k)
  }

  private feedLooped(k: number): void {
    for (let s = 0; s < this.stems.length; s++) this.feedStem(s, k)
  }

  /** The stretched path: a block from every stretcher, mixed with the gains.
   *  Leaves the playhead alone (the caller advances it). */
  private renderStretched(outL: Float32Array, outR: Float32Array, n: number): void {
    outL.fill(0)
    outR.fill(0)
    for (let s = 0; s < this.stretchers.length; s++) {
      const st = this.stretchers[s]
      for (let guard = 0; st.available < n && guard < 32; guard++) {
        this.feedStem(s, Math.min(Math.max(st.required, 1), MAX_BLOCK))
      }
      const buf = this.outTmp[s]
      const got = st.retrieve(buf, n)
      const g = this.gainBuf[s]
      for (let i = 0; i < got; i++) {
        outL[i] += buf[0][i] * g[i]
        outR[i] += buf[1][i] * g[i]
      }
    }
  }
}

/** `to` = `from` fading out + `to` fading in, linearly over the block. */
function crossfade(
  fromL: Float32Array,
  fromR: Float32Array,
  toL: Float32Array,
  toR: Float32Array,
  n: number,
): void {
  for (let i = 0; i < n; i++) {
    const t = (i + 0.5) / n
    toL[i] = fromL[i] * (1 - t) + toL[i] * t
    toR[i] = fromR[i] * (1 - t) + toR[i] * t
  }
}
