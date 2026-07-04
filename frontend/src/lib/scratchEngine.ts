// A minimal "scratch" engine: while the user drags the waveform we play the
// decoded audio at a position that glides toward the cursor's target. Fast drags
// → high pitch, reverse drags → reverse audio, holding still → silence — the
// characteristic scratch behaviour, which a plain <audio> element can't do
// (no reverse, no arbitrary rate).
//
// On release the engine coasts: it keeps the drag velocity and eases it toward a
// target speed with friction (momentum/flywheel feel) — to a stop if playback was
// paused, or up to normal speed if it was playing. When it settles, `finished`
// flips true and the caller hands the position back to the <audio> element.
//
// Uses a ScriptProcessorNode: deprecated but universally supported and runs on
// the main thread, so `position`/`finished` are directly readable for visual
// sync. Moving to an AudioWorklet is the natural hardening step later.

const BUFFER_SIZE = 512
const GLIDE = 400 // samples: how tightly position chases the target while dragging
const RATE_CAP = 12 // max samples advanced per output sample (pitch ceiling)
const INERTIA_FRICTION = 0.9998 // per-sample easing toward the target velocity (~0.4s spin-down)
const SETTLE_EPS = 0.02 // |velocity - target| below this → settled

type Mode = 'drag' | 'inertia' | 'done'

export class ScratchEngine {
  private channels: Float32Array[] = []
  private length = 0
  private sr = 44100
  private ctxRate = 44100
  private node: ScriptProcessorNode | null = null
  private position = 0 // in source samples
  private target = 0 // drag target, in source samples
  private velocity = 0 // source samples advanced per output sample
  private targetVelocity = 0 // inertia goal
  private mode: Mode = 'drag'

  load(buffer: AudioBuffer): void {
    this.channels = []
    for (let c = 0; c < buffer.numberOfChannels; c++) {
      this.channels.push(buffer.getChannelData(c))
    }
    this.length = buffer.length
    this.sr = buffer.sampleRate
  }

  get loaded(): boolean {
    return this.length > 0
  }

  get finished(): boolean {
    return this.mode === 'done'
  }

  getPositionSec(): number {
    return this.position / this.sr
  }

  /** Enter scratch mode at the given position (creates the audio node if needed). */
  begin(ctx: AudioContext, positionSec: number): void {
    if (!this.loaded) return
    this.ctxRate = ctx.sampleRate
    this.position = positionSec * this.sr
    this.target = this.position
    this.velocity = 0
    this.mode = 'drag'
    if (!this.node) {
      const node = ctx.createScriptProcessor(BUFFER_SIZE, 0, 2)
      node.onaudioprocess = (e) => this.render(e)
      node.connect(ctx.destination)
      this.node = node
    }
  }

  /** Re-grab while the platter is still coasting: resume dragging from here. */
  regrab(): void {
    this.mode = 'drag'
    this.target = this.position
  }

  setTargetSec(sec: number): void {
    this.target = Math.max(0, Math.min(this.length - 1, sec * this.sr))
  }

  /** Release the platter: coast with the current velocity, easing toward normal
   *  speed (resume=true) or a stop (resume=false). `finished` flips when settled. */
  release(resume: boolean): void {
    this.targetVelocity = resume ? this.sr / this.ctxRate : 0
    this.mode = 'inertia'
  }

  /** Tear down the audio node; returns the final position (seconds). */
  end(): number {
    if (this.node) {
      this.node.disconnect()
      this.node.onaudioprocess = null
      this.node = null
    }
    return this.position / this.sr
  }

  private sample(outs: Float32Array[], i: number, chN: number): void {
    const i0 = Math.floor(this.position)
    const frac = this.position - i0
    for (let c = 0; c < chN; c++) {
      const src = this.channels[Math.min(c, this.channels.length - 1)]
      outs[c][i] = i0 + 1 < this.length ? src[i0] * (1 - frac) + src[i0 + 1] * frac : src[i0]
    }
  }

  private silence(outs: Float32Array[], i: number, chN: number): void {
    for (let c = 0; c < chN; c++) outs[c][i] = 0
  }

  private render(e: AudioProcessingEvent): void {
    const out = e.outputBuffer
    const n = out.length
    const chN = out.numberOfChannels
    const outs: Float32Array[] = []
    for (let c = 0; c < chN; c++) outs.push(out.getChannelData(c))

    for (let i = 0; i < n; i++) {
      if (this.mode === 'drag') {
        const diff = this.target - this.position
        if (Math.abs(diff) < 1) {
          // Hand at rest on the platter → no movement, no sound.
          this.velocity = 0
          this.silence(outs, i, chN)
          continue
        }
        let step = diff / GLIDE
        if (step > RATE_CAP) step = RATE_CAP
        else if (step < -RATE_CAP) step = -RATE_CAP
        this.position += step
        this.velocity = this.velocity * 0.9 + step * 0.1 // smoothed, for release momentum
      } else if (this.mode === 'inertia') {
        this.velocity = this.targetVelocity + (this.velocity - this.targetVelocity) * INERTIA_FRICTION
        this.position += this.velocity
        if (Math.abs(this.velocity - this.targetVelocity) < SETTLE_EPS) {
          this.mode = 'done'
        }
      } else {
        this.silence(outs, i, chN)
        continue
      }

      // Clamp to the track; hitting an edge kills momentum.
      if (this.position < 0) {
        this.position = 0
        this.velocity = 0
        if (this.mode === 'inertia') this.mode = 'done'
      } else if (this.position > this.length - 1) {
        this.position = this.length - 1
        this.velocity = 0
        if (this.mode === 'inertia') this.mode = 'done'
      }
      this.sample(outs, i, chN)
    }
  }
}
