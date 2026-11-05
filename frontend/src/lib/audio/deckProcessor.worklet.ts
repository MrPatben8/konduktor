// The deck's AudioWorkletProcessor: a thin shell around DeckCore (see there).
// Loaded once per AudioContext by `context.ts` (`?worker&url`, so Vite bundles
// its imports). Commands arrive on the port and apply before the next block;
// the playhead is reported at the START of a block, stamped with that block's
// context time, so the main thread can extrapolate it smoothly.

import { DeckCore, type DeckSource } from './deckCore'
import { RubberBand } from './stretcher'

// AudioWorkletGlobalScope — not in TypeScript's DOM lib.
declare const sampleRate: number
declare const currentTime: number
declare function registerProcessor(name: string, ctor: unknown): void
declare class AudioWorkletProcessor {
  readonly port: MessagePort
  constructor(options?: unknown)
}

/** Report the playhead every this many blocks (~5.8 ms at 44.1 kHz), and
 *  always on the block after a command. */
const REPORT_EVERY = 2

export type DeckCommand =
  | { op: 'play' }
  | { op: 'pause' }
  | { op: 'seek'; sec: number }
  | { op: 'loop'; start: number; end: number; on: boolean }
  | { op: 'gains'; levels: number[] }
  | { op: 'rate'; rate: number }
  | { op: 'keylock'; on: boolean }
  | { op: 'leadIn'; on: boolean }
  | { op: 'scratchBegin'; sec: number; id: number }
  | { op: 'scratchRegrab'; sec: number; id: number }
  | { op: 'scratchTarget'; sec: number }
  | { op: 'scratchRelease'; resume: boolean }

export type DeckInbound =
  | { type: 'load'; gen: number; source: DeckSource }
  | { type: 'cmd'; gen: number; cmd: DeckCommand }
  | { type: 'dispose' }

export interface DeckReport {
  type: 'state'
  /** The last position-changing command applied. */
  gen: number
  /** Context time of the block this position starts. */
  time: number
  pos: number
  speed: number
  playing: boolean
  scratching: boolean
  keylockAvailable: boolean
}

export type DeckOutbound =
  | DeckReport
  | {
      type: 'event'
      event: 'ended' | 'scratchDone'
      gen: number
      /** The grab (`scratchBegin` / `scratchRegrab` id) a scratchDone ends. */
      scratch: number
      pos: number
      playing: boolean
    }
  | { type: 'error'; message: string }

class DeckProcessor extends AudioWorkletProcessor {
  private readonly core: DeckCore
  private readonly rbOk: boolean
  private gen = 0
  private tick = 0
  private dirty = true
  private alive = true

  constructor(options: { processorOptions?: { wasm?: ArrayBuffer | null } }) {
    super()
    let rb: RubberBand | null = null
    const wasm = options.processorOptions?.wasm
    if (wasm) {
      try {
        rb = new RubberBand(new WebAssembly.Module(wasm))
      } catch (e) {
        this.port.postMessage({ type: 'error', message: `keylock unavailable: ${e}` })
      }
    }
    this.rbOk = rb != null
    this.core = new DeckCore(sampleRate, rb)
    this.port.onmessage = (e: MessageEvent<DeckInbound>) => this.receive(e.data)
  }

  private receive(m: DeckInbound): void {
    if (m.type === 'dispose') {
      this.core.dispose()
      this.alive = false
      return
    }
    this.gen = m.gen
    this.dirty = true
    if (m.type === 'load') {
      this.core.load(m.source)
      return
    }
    const c = m.cmd
    const core = this.core
    switch (c.op) {
      case 'play':
        return core.play()
      case 'pause':
        return core.pause()
      case 'seek':
        return core.seek(c.sec)
      case 'loop':
        return core.setLoop(c.start, c.end, c.on)
      case 'gains':
        return core.setGains(c.levels)
      case 'rate':
        return core.setRate(c.rate)
      case 'keylock':
        return core.setKeylock(c.on)
      case 'leadIn':
        return core.setLeadIn(c.on)
      case 'scratchBegin':
        return core.scratchBegin(c.sec, c.id)
      case 'scratchRegrab':
        return core.scratchRegrab(c.sec, c.id)
      case 'scratchTarget':
        return core.scratchTarget(c.sec)
      case 'scratchRelease':
        return core.scratchRelease(c.resume)
    }
  }

  process(_inputs: Float32Array[][], outputs: Float32Array[][]): boolean {
    if (!this.alive) return false
    const out = outputs[0]
    if (this.dirty || ++this.tick >= REPORT_EVERY) {
      this.tick = 0
      this.dirty = false
      const core = this.core
      const report: DeckReport = {
        type: 'state',
        gen: this.gen,
        time: currentTime,
        pos: core.positionSec,
        speed: core.speed,
        playing: core.playing,
        scratching: core.scratching,
        keylockAvailable: this.rbOk,
      }
      this.port.postMessage(report)
    }
    this.core.render(out[0], out[1] ?? out[0])
    for (const event of this.core.events) {
      this.port.postMessage({
        type: 'event',
        event,
        gen: this.gen,
        scratch: this.core.scratchId,
        pos: this.core.positionSec,
        playing: this.core.playing,
      })
    }
    return true
  }
}

registerProcessor('konduktor-deck', DeckProcessor)
