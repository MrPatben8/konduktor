// The main thread's handle on one deck's audio worklet (`deckCore.ts` does the
// work). Same surface as the PlaybackEngine it replaced, plus rate, keylock
// and scratch, which used to be a separate engine on a separate AudioContext.
//
// The worklet runs on the audio thread, so every command is asynchronous. The
// engine therefore answers from its own model of the deck — the last report,
// extrapolated by the context clock, and updated at once by each command — so
// `getPosition()` right after `seek(t)` is `t`, as callers expect. Each
// position-changing command bumps a generation; a report from before it is
// ignored, so a stale position can never snap the playhead back.

import type { DecodedAudio } from '../waveform'
import { audioContext, loadDeckWorklet, resumeAudio } from './context'
import type { DeckCommand, DeckInbound, DeckOutbound } from './deckProcessor.worklet'
import { extrapolate, type Loop, type Model } from './position'

/** As `deckCore`'s: the furthest a lead-in reaches before the track (s). */
const LEAD_IN_SEC = 32

export class DeckEngine {
  readonly output: GainNode
  private readonly ctx: AudioContext
  private readonly node: AudioWorkletNode
  private gen = 0
  private m: Model = { pos: 0, time: 0, speed: 0, playing: false, scratching: false }
  private loop: Loop = { start: 0, end: 0, on: false }
  private rate = 1
  private leadIn = false
  private _duration = 0
  private _keylockAvailable = false
  private onEnded: (() => void) | null = null
  private onScratchDone: ((playing: boolean) => void) | null = null

  /** The worklet module loads asynchronously, once per app. `destination` is
   *  where the deck plays into: its mixer channel. */
  static async create(destination: AudioNode): Promise<DeckEngine> {
    const wasm = await loadDeckWorklet()
    return new DeckEngine(wasm, destination)
  }

  private constructor(wasm: ArrayBuffer | null, destination: AudioNode) {
    this.ctx = audioContext()
    this.node = new AudioWorkletNode(this.ctx, 'konduktor-deck', {
      numberOfInputs: 0,
      numberOfOutputs: 1,
      outputChannelCount: [2],
      processorOptions: { wasm },
    })
    this.output = this.ctx.createGain()
    this.node.connect(this.output)
    this.output.connect(destination)
    this.node.port.onmessage = (e: MessageEvent<DeckOutbound>) => this.receive(e.data)
  }

  private receive(msg: DeckOutbound): void {
    if (msg.type === 'error') {
      console.warn(msg.message)
      return
    }
    if (msg.gen !== this.gen) return // a report from before the latest command
    if (msg.type === 'state') {
      this._keylockAvailable = msg.keylockAvailable
      this.m = { pos: msg.pos, time: msg.time, speed: msg.speed, playing: msg.playing, scratching: msg.scratching }
      return
    }
    this.m = { ...this.m, pos: msg.pos, time: this.ctx.currentTime, speed: msg.playing ? this.rate : 0, playing: msg.playing, scratching: false }
    if (msg.event === 'ended') this.onEnded?.()
    else this.onScratchDone?.(msg.playing)
  }

  private send(cmd: DeckCommand, positional = true): void {
    if (positional) this.gen++
    const m: DeckInbound = { type: 'cmd', gen: this.gen, cmd }
    this.node.port.postMessage(m)
  }

  /** Re-take the model at "now" before a change, so the change starts from
   *  the true position. */
  private reanchor(): void {
    this.m = { ...this.m, pos: this.getPosition(), time: this.ctx.currentTime }
  }

  // ---- loading ------------------------------------------------------------

  /** One track, or several stems played in lockstep. The PCM is TRANSFERRED to
   *  the audio thread: the caller must not use these arrays afterwards. */
  load(audio: DecodedAudio | DecodedAudio[]): void {
    const list = Array.isArray(audio) ? audio : [audio]
    const length = Math.min(...list.map((a) => a.length))
    const sampleRate = list[0].sampleRate
    const stems = list.map((a) => a.channels)
    const transfer = [...new Set(stems.flat().map((c) => c.buffer as ArrayBuffer))]
    this.gen++
    const m: DeckInbound = { type: 'load', gen: this.gen, source: { stems, length, sampleRate } }
    this.node.port.postMessage(m, transfer)
    this._duration = length / sampleRate
    this.m = { pos: 0, time: this.ctx.currentTime, speed: 0, playing: false, scratching: false }
    this.loop = { start: 0, end: 0, on: false }
  }

  setOnEnded(cb: () => void): void {
    this.onEnded = cb
  }

  /** Called when a scratch's coast settles; the deck is then playing (if the
   *  release asked to resume) or paused — it hands itself back. */
  setOnScratchDone(cb: (playing: boolean) => void): void {
    this.onScratchDone = cb
  }

  get ready(): boolean {
    return this._duration > 0
  }
  get duration(): number {
    return this._duration
  }
  get playing(): boolean {
    return this.m.playing
  }
  get scratching(): boolean {
    return this.m.scratching
  }
  get loopEnabled(): boolean {
    return this.loop.on
  }
  /** Rubber Band loaded in the worklet (known after its first report). */
  get keylockAvailable(): boolean {
    return this._keylockAvailable
  }

  getPosition(): number {
    return extrapolate(this.m, this.ctx.currentTime, this.loop, this._duration)
  }

  // ---- transport ------------------------------------------------------------

  play(): void {
    if (!this.ready || this.m.playing) return
    resumeAudio()
    let pos = this.m.pos
    if (pos >= this._duration - 0.01) pos = 0 // restart from the top at the end
    this.m = { pos, time: this.ctx.currentTime, speed: this.rate, playing: true, scratching: false }
    this.send({ op: 'play' })
  }

  pause(): void {
    if (!this.m.playing) return
    this.m = { ...this.m, pos: this.getPosition(), time: this.ctx.currentTime, speed: 0, playing: false }
    this.send({ op: 'pause' })
  }

  seek(t: number): void {
    const end = this.loop.on ? Math.max(this._duration, this.loop.end) : this._duration
    const sec = Math.max(this.leadIn ? -LEAD_IN_SEC : 0, Math.min(t, end))
    this.m = { ...this.m, pos: sec, time: this.ctx.currentTime }
    this.send({ op: 'seek', sec })
  }

  /** Set the loop region and whether it is active. Bounds apply live if playing. */
  setLoop(start: number, end: number, enabled: boolean): void {
    this.reanchor()
    this.loop = { start, end, on: enabled && end > start }
    if (this.loop.on && this.m.pos >= end) this.m = { ...this.m, pos: start }
    this.send({ op: 'loop', start, end, on: enabled })
  }

  setLoopEnabled(enabled: boolean): void {
    this.setLoop(this.loop.start, this.loop.end, enabled)
  }

  /** Per-stem levels (0 = muted) — the stems' mute/solo. Ramped, not stepped. */
  setGains(levels: number[]): void {
    this.send({ op: 'gains', levels }, false)
  }

  /** Playback speed (1 = the file's own tempo): the pitch fader. */
  setRate(rate: number): void {
    this.reanchor()
    this.rate = rate
    if (this.m.playing) this.m = { ...this.m, speed: rate }
    this.send({ op: 'rate', rate })
  }

  /** Performance mode: a seek may land before the track (silence until it
   *  starts) — and a loop may reach past its end. */
  setLeadIn(on: boolean): void {
    this.leadIn = on
    if (!on && this.m.pos < 0) this.m = { ...this.m, pos: 0, time: this.ctx.currentTime }
    this.send({ op: 'leadIn', on })
  }

  /** Keylock: keep the pitch while the rate changes. */
  setKeylock(on: boolean): void {
    this.send({ op: 'keylock', on }, false)
  }

  // ---- scratch ----------------------------------------------------------------

  scratchBegin(sec: number): void {
    resumeAudio()
    this.m = { pos: sec, time: this.ctx.currentTime, speed: 0, playing: false, scratching: true }
    this.send({ op: 'scratchBegin', sec })
  }

  /** Re-grab while the platter is still coasting. */
  scratchRegrab(): void {
    this.send({ op: 'scratchRegrab' }, false)
  }

  scratchTarget(sec: number): void {
    this.send({ op: 'scratchTarget', sec }, false)
  }

  /** Release the platter: coast to playing speed (resume) or to a stop. */
  scratchRelease(resume: boolean): void {
    this.send({ op: 'scratchRelease', resume }, false)
  }

  dispose(): void {
    const m: DeckInbound = { type: 'dispose' }
    this.node.port.postMessage(m)
    this.node.disconnect()
    this.output.disconnect()
  }
}
