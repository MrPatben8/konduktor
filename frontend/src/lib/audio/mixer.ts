// The mixer's audio graph (performance mode, decided 2026-11-02). Both decks
// play into it; it follows the controls in `mixerState.ts`.
//
//   deck → trim → 3-band isolator → colour FX → fader ─┬→ crossfader → bus
//                                                       └→ meter
//   bus → limiter → master meter → speakers
//
// TRIM starts at the track's own auto gain (the DJ app's, `Track.auto_gain_db`;
// none = 0 dB) plus the knob. The ISOLATOR splits at 250 Hz / 2.5 kHz with
// Linkwitz-Riley (LR4) crossovers, the low band through an all-pass matching
// the upper crossover, so the three bands sum back to flat — measured to 0.0001
// dB — and a band can be cut to silence (`eqKill`). Web Audio's lowpass /
// highpass Q is in dB (Butterworth = -3.01) while its allpass Q is linear.
// The LIMITER is a DynamicsCompressor set as one (-1.5 dBFS, 20:1, 1 ms), and
// `levels().limiting` is its gain reduction. Web Audio's compressor adds an
// automatic MAKE-UP gain — (1 / its gain at 0 dBFS)^0.6, +0.57 dB at -1 / 20:1
// — which raised everything in circuit and let two loud decks peak at +0.3
// dBFS (measured); a gain after it takes exactly that back off.
//
// Outside performance mode the mixer is BYPASSED: deck A goes straight to the
// speakers, as the prep deck always has, deck B is silent, and the limiter is
// out of the path — so prep listening is unchanged.

import { audioContext } from './context'
import {
  dbToGain,
  echoSeconds,
  eqGain,
  EQ_HIGH_HZ,
  EQ_LOW_HZ,
  faderGain,
  filterSweep,
  FX_CENTRE,
  TRIM_RANGE_DB,
  xfadeGains,
} from './mixerMath'
import { getMixerState, subscribeMixer, type Channel, type ChannelState, type FxSettings, type FxType } from './mixerState'

/** Butterworth Q in Web Audio's lowpass/highpass units (dB). */
const BUTTERWORTH_DB = 20 * Math.log10(Math.SQRT1_2)
const LIMIT_DB = -1.5
const LIMIT_RATIO = 20
/** The compressor's automatic make-up gain (Web Audio spec: the inverse of its
 *  gain at 0 dBFS, to the power 0.6), in dB. */
const MAKEUP_DB = -0.6 * (LIMIT_DB + (0 - LIMIT_DB) / LIMIT_RATIO)

/** Parameter smoothing (s): fast enough to feel direct, slow enough not to click. */
const GLIDE = 0.012

function ramp(param: AudioParam, value: number, ctx: AudioContext, tau = GLIDE): void {
  param.setTargetAtTime(value, ctx.currentTime, tau)
}

function biquad(ctx: AudioContext, type: BiquadFilterType, freq: number, q: number): BiquadFilterNode {
  const f = ctx.createBiquadFilter()
  f.type = type
  f.frequency.value = freq
  f.Q.value = q
  return f
}

/** Two cascaded Butterworth sections: one LR4 half. */
function lr4(ctx: AudioContext, type: 'lowpass' | 'highpass', freq: number): [BiquadFilterNode, BiquadFilterNode] {
  const a = biquad(ctx, type, freq, BUTTERWORTH_DB)
  const b = biquad(ctx, type, freq, BUTTERWORTH_DB)
  a.connect(b)
  return [a, b]
}

function noiseBuffer(ctx: AudioContext, seconds: number): AudioBuffer {
  const buf = ctx.createBuffer(2, Math.round(ctx.sampleRate * seconds), ctx.sampleRate)
  for (let c = 0; c < 2; c++) {
    const d = buf.getChannelData(c)
    for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1
  }
  return buf
}

/** A reverb impulse: stereo noise decaying 60 dB over `seconds`. */
function impulse(ctx: AudioContext, seconds: number): AudioBuffer {
  const buf = noiseBuffer(ctx, seconds)
  const k = -6.9 / (seconds * ctx.sampleRate)
  for (let c = 0; c < 2; c++) {
    const d = buf.getChannelData(c)
    for (let i = 0; i < d.length; i++) d[i] *= Math.exp(k * i)
  }
  return buf
}

/** A bit-crusher's transfer curve at `bits` of resolution. */
function crushCurve(bits: number): Float32Array<ArrayBuffer> {
  const n = 2048
  const steps = 2 ** bits / 2
  const curve = new Float32Array(n)
  for (let i = 0; i < n; i++) {
    const x = (i / (n - 1)) * 2 - 1
    curve[i] = Math.round(x * steps) / steps
  }
  return curve
}

/**
 * One channel's colour effect: ONE knob, -1..1, out of the path at the centre;
 * which effect it drives is the channel's own choice (decided 2026-11-02), its
 * one parameter an app setting (`FxSettings`, decided 2026-11-03).
 * Every effect is built once and switched by gains, so a change never clicks;
 * echo and reverb tails ring out after the knob returns.
 */
class ColorFx {
  readonly input: GainNode
  readonly output: GainNode
  private readonly ctx: AudioContext
  private readonly dry: GainNode
  // Filter (also the music's path for Crush's tone).
  private readonly filter: BiquadFilterNode
  private readonly filterWet: GainNode
  // Echo: a send into a delay with filtered feedback.
  private readonly echoSend: GainNode
  private readonly delay: DelayNode
  private readonly echoTone: BiquadFilterNode
  private readonly feedback: GainNode
  // Space: a send into a convolver.
  private readonly spaceSend: GainNode
  private readonly spaceTone: BiquadFilterNode
  private readonly convolver: ConvolverNode
  private spaceSize = -1
  // Noise: filtered white noise mixed in.
  private readonly noiseFilter: BiquadFilterNode
  private readonly noiseGain: GainNode
  // Crush.
  private readonly crushDrive: GainNode
  private readonly shaper: WaveShaperNode
  private readonly crushWet: GainNode
  private crushBits = -1

  constructor(ctx: AudioContext) {
    this.ctx = ctx
    this.input = ctx.createGain()
    this.output = ctx.createGain()
    this.dry = ctx.createGain()
    this.input.connect(this.dry).connect(this.output)

    this.filter = biquad(ctx, 'lowpass', 20000, 0)
    this.filterWet = ctx.createGain()
    this.filterWet.gain.value = 0
    this.input.connect(this.filter).connect(this.filterWet).connect(this.output)

    this.echoSend = ctx.createGain()
    this.echoSend.gain.value = 0
    this.delay = ctx.createDelay(4)
    this.echoTone = biquad(ctx, 'lowpass', 2500, 0)
    this.feedback = ctx.createGain()
    this.feedback.gain.value = 0.45
    this.input.connect(this.echoSend).connect(this.delay)
    this.delay.connect(this.echoTone).connect(this.feedback).connect(this.delay)
    this.delay.connect(this.output)

    this.spaceSend = ctx.createGain()
    this.spaceSend.gain.value = 0
    this.spaceTone = biquad(ctx, 'lowpass', 20000, 0)
    this.convolver = ctx.createConvolver()
    this.input.connect(this.spaceSend).connect(this.spaceTone).connect(this.convolver).connect(this.output)

    const noise = ctx.createBufferSource()
    noise.buffer = noiseBuffer(ctx, 2)
    noise.loop = true
    this.noiseFilter = biquad(ctx, 'lowpass', 20000, 0)
    this.noiseGain = ctx.createGain()
    this.noiseGain.gain.value = 0
    noise.connect(this.noiseFilter).connect(this.noiseGain).connect(this.output)
    noise.start()

    this.crushDrive = ctx.createGain()
    this.shaper = ctx.createWaveShaper()
    this.crushWet = ctx.createGain()
    this.crushWet.gain.value = 0
    this.input.connect(this.crushDrive).connect(this.shaper).connect(this.crushWet).connect(this.output)
  }

  apply(type: FxType, color: number, fx: FxSettings, bpm: number): void {
    const ctx = this.ctx
    const a = Math.min(1, Math.abs(color))
    const on = a >= FX_CENTRE
    // Whatever replaces the dry signal (filter, crush) fades in over the first
    // few percent of the knob, so leaving the centre never jumps.
    const take = on ? Math.min(1, a / 0.06) : 0
    const sweep = filterSweep(color)

    // Filter: the music itself, swept.
    const filtering = type === 'filter' && sweep != null
    if (filtering) {
      if (this.filter.type !== sweep.type) this.filter.type = sweep.type
      ramp(this.filter.frequency, sweep.freq, ctx)
      ramp(this.filter.Q, fx.filterResonance * 15, ctx)
    }
    ramp(this.filterWet.gain, filtering ? take : 0, ctx)

    // Echo: send + feedback grow with the knob; left darker, right thinner.
    const echoing = type === 'echo' && on
    ramp(this.echoSend.gain, echoing ? a * 0.8 : 0, ctx)
    ramp(this.feedback.gain, echoing ? 0.35 + 0.5 * a : 0.45, ctx, 0.05)
    if (type === 'echo') {
      ramp(this.delay.delayTime, echoSeconds(fx.echoBeats, bpm), ctx, 0.05)
      const tone = color < 0 ? 'lowpass' : 'highpass'
      if (this.echoTone.type !== tone) this.echoTone.type = tone
      ramp(this.echoTone.frequency, color < 0 ? 2500 : 600, ctx)
    }

    // Space: a reverb send; left darker, right thinner.
    const spacing = type === 'space' && on
    ramp(this.spaceSend.gain, spacing ? a : 0, ctx)
    if (type === 'space') {
      const size = Math.round(Math.max(0.3, Math.min(8, fx.spaceSize)) * 10) / 10
      if (size !== this.spaceSize) {
        this.spaceSize = size
        this.convolver.buffer = impulse(ctx, size)
      }
      const tone = color < 0 ? 'lowpass' : 'highpass'
      if (this.spaceTone.type !== tone) this.spaceTone.type = tone
      ramp(this.spaceTone.frequency, color < 0 ? 1500 : 700, ctx)
    }

    // Noise: white noise swept the way the filter is, the music untouched.
    const noising = type === 'noise' && sweep != null
    if (noising) {
      if (this.noiseFilter.type !== sweep.type) this.noiseFilter.type = sweep.type
      ramp(this.noiseFilter.frequency, sweep.freq, ctx)
      ramp(this.noiseFilter.Q, fx.noiseResonance * 15, ctx)
    }
    ramp(this.noiseGain.gain, noising ? a * 0.25 : 0, ctx)

    // Crush: fewer bits as the knob opens (16 → 3), driven by the setting.
    const crushing = type === 'crush' && on
    if (crushing) {
      const bits = Math.round(16 - a * 13)
      if (bits !== this.crushBits) {
        this.crushBits = bits
        this.shaper.curve = crushCurve(bits)
      }
      ramp(this.crushDrive.gain, fx.crushDrive, ctx)
    }
    ramp(this.crushWet.gain, crushing ? take / Math.max(1, fx.crushDrive) ** 0.5 : 0, ctx)

    ramp(this.dry.gain, filtering || crushing ? 1 - take : 1, ctx)
  }
}

class Strip {
  readonly input: GainNode
  readonly meter: AnalyserNode
  private readonly ctx: AudioContext
  private readonly trim: GainNode
  private readonly bands: [GainNode, GainNode, GainNode]
  private readonly fx: ColorFx
  private readonly fader: GainNode
  private readonly xfade: GainNode
  /** In circuit (performance mode) vs straight through (prep). */
  private readonly wetGate: GainNode
  private readonly dryGate: GainNode
  autoGainDb = 0
  bpm = 0

  constructor(ctx: AudioContext, bus: AudioNode) {
    this.ctx = ctx
    this.input = ctx.createGain()
    this.trim = ctx.createGain()
    this.input.connect(this.trim)

    // Isolator: low = LR4 low-pass @250 then an all-pass @2.5k (the phase the
    // other bands get from their LR4 pair there); mid = high-pass @250 then
    // low-pass @2.5k; high = high-pass @250 then high-pass @2.5k.
    const eqOut = ctx.createGain()
    const [lpA, lpB] = lr4(ctx, 'lowpass', EQ_LOW_HZ)
    const [hpA, hpB] = lr4(ctx, 'highpass', EQ_LOW_HZ)
    const ap = biquad(ctx, 'allpass', EQ_HIGH_HZ, Math.SQRT1_2)
    const [mlA, mlB] = lr4(ctx, 'lowpass', EQ_HIGH_HZ)
    const [mhA, mhB] = lr4(ctx, 'highpass', EQ_HIGH_HZ)
    this.bands = [ctx.createGain(), ctx.createGain(), ctx.createGain()]
    this.trim.connect(lpA)
    this.trim.connect(hpA)
    lpB.connect(ap).connect(this.bands[0]).connect(eqOut)
    hpB.connect(mlA)
    hpB.connect(mhA)
    mlB.connect(this.bands[1]).connect(eqOut)
    mhB.connect(this.bands[2]).connect(eqOut)

    this.fx = new ColorFx(ctx)
    eqOut.connect(this.fx.input)
    this.fader = ctx.createGain()
    this.fx.output.connect(this.fader)
    this.meter = ctx.createAnalyser()
    this.meter.fftSize = 512
    this.fader.connect(this.meter)
    this.xfade = ctx.createGain()
    this.wetGate = ctx.createGain()
    this.fader.connect(this.xfade).connect(this.wetGate).connect(bus)
    this.dryGate = ctx.createGain()
    this.input.connect(this.dryGate).connect(bus)
  }

  apply(s: ChannelState, xfade: number, eqKill: boolean, fx: FxSettings): void {
    const ctx = this.ctx
    const trimDb = this.autoGainDb + Math.max(-TRIM_RANGE_DB, Math.min(TRIM_RANGE_DB, s.trim))
    ramp(this.trim.gain, dbToGain(trimDb), ctx)
    ramp(this.bands[0].gain, eqGain(s.low, eqKill), ctx)
    ramp(this.bands[1].gain, eqGain(s.mid, eqKill), ctx)
    ramp(this.bands[2].gain, eqGain(s.high, eqKill), ctx)
    this.fx.apply(s.fx, s.color, fx, this.bpm)
    ramp(this.fader.gain, faderGain(s.fader), ctx)
    ramp(this.xfade.gain, xfade, ctx)
  }

  route(inCircuit: boolean, audible: boolean): void {
    ramp(this.wetGate.gain, audible && inCircuit ? 1 : 0, this.ctx, 0.01)
    ramp(this.dryGate.gain, audible && !inCircuit ? 1 : 0, this.ctx, 0.01)
  }
}

export interface MixerLevels {
  /** Peak dBFS: each channel after its fader, and the master after the limiter. */
  A: number
  B: number
  master: number
  /** The limiter's gain reduction now (dB, >= 0). */
  limiting: number
}

export class Mixer {
  private readonly ctx: AudioContext
  private readonly strips: Record<Channel, Strip>
  private readonly limiter: DynamicsCompressorNode
  private readonly limWet: GainNode
  private readonly limDry: GainNode
  private readonly masterMeter: AnalyserNode
  private readonly buf = new Float32Array(512)
  private active = false

  constructor(ctx: AudioContext) {
    this.ctx = ctx
    const bus = ctx.createGain()
    this.limiter = ctx.createDynamicsCompressor()
    this.limiter.threshold.value = LIMIT_DB
    this.limiter.knee.value = 0
    this.limiter.ratio.value = LIMIT_RATIO
    this.limiter.attack.value = 0.001
    this.limiter.release.value = 0.15
    const unMakeup = ctx.createGain()
    unMakeup.gain.value = dbToGain(-MAKEUP_DB)
    const out = ctx.createGain()
    this.limWet = ctx.createGain()
    this.limDry = ctx.createGain()
    bus.connect(this.limiter).connect(unMakeup).connect(this.limWet).connect(out)
    bus.connect(this.limDry).connect(out)
    this.masterMeter = ctx.createAnalyser()
    this.masterMeter.fftSize = 512
    out.connect(this.masterMeter)
    out.connect(ctx.destination)
    this.strips = { A: new Strip(ctx, bus), B: new Strip(ctx, bus) }
    this.setActive(false)
    subscribeMixer(() => this.apply())
    this.apply()
  }

  /** Where deck `ch` plays into. */
  input(ch: Channel): AudioNode {
    return this.strips[ch].input
  }

  /** Performance mode: both channels through the mixer and the limiter. Off:
   *  deck A straight to the speakers, deck B silent. */
  setActive(on: boolean): void {
    this.active = on
    this.strips.A.route(on, true)
    this.strips.B.route(on, on)
    ramp(this.limWet.gain, on ? 1 : 0, this.ctx, 0.01)
    ramp(this.limDry.gain, on ? 0 : 1, this.ctx, 0.01)
  }

  get isActive(): boolean {
    return this.active
  }

  /** The loaded track's own auto gain (dB; 0 when it has none). */
  setAutoGain(ch: Channel, db: number): void {
    this.strips[ch].autoGainDb = db
    this.apply()
  }

  /** The deck's tempo now — Echo's beat. */
  setTempo(ch: Channel, bpm: number): void {
    if (this.strips[ch].bpm === bpm) return
    this.strips[ch].bpm = bpm
    this.apply()
  }

  private apply(): void {
    const s = getMixerState()
    const [ga, gb] = xfadeGains(s.crossfader, s.sharpness)
    this.strips.A.apply(s.A, ga, s.eqKill, s.fx)
    this.strips.B.apply(s.B, gb, s.eqKill, s.fx)
  }

  private peak(meter: AnalyserNode): number {
    meter.getFloatTimeDomainData(this.buf)
    let p = 0
    for (let i = 0; i < this.buf.length; i++) {
      const v = Math.abs(this.buf[i])
      if (v > p) p = v
    }
    return p > 0 ? 20 * Math.log10(p) : -Infinity
  }

  /** Read the meters (call once per frame). */
  levels(): MixerLevels {
    return {
      A: this.peak(this.strips.A.meter),
      B: this.peak(this.strips.B.meter),
      master: this.peak(this.masterMeter),
      limiting: Math.max(0, -this.limiter.reduction),
    }
  }
}

let instance: Mixer | null = null

/** The app's mixer, built on its AudioContext the first time it is asked for. */
export function mixer(): Mixer {
  if (!instance) instance = new Mixer(audioContext())
  return instance
}

