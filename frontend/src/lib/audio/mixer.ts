// The mixer's audio graph (performance mode, decided 2026-11-02). Both decks
// play into it; it follows the controls in `mixerState.ts`.
//
//   deck → gain → 3-band isolator → filter FX → fader ─┬→ crossfader → bus
//                                                       └→ meter
//   bus → limiter → master meter → speakers
//
// GAIN (`trim` in the state) starts at the track's own auto gain (the DJ app's, `Track.auto_gain_db`;
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
  beatSeconds,
  dbToGain,
  eqGain,
  EQ_HIGH_HZ,
  EQ_LOW_HZ,
  delayBeatsAt,
  faderGain,
  filterSweep,
  sweepBeatsAt,
  REVERB_GROWTH,
  FX_CENTRE,
  TRIM_RANGE_DB,
  xfadeGains,
} from './mixerMath'
import { getMixerState, rangeFor, subscribeMixer, type Channel, type ChannelState, type FxSettings, type FxType } from './mixerState'

/** Butterworth Q in Web Audio's lowpass/highpass units (dB). */
const BUTTERWORTH_DB = 20 * Math.log10(Math.SQRT1_2)
const LIMIT_DB = -1.5
const LIMIT_RATIO = 20
/** The compressor's automatic make-up gain (Web Audio spec: the inverse of its
 *  gain at 0 dBFS, to the power 0.6), in dB. */
const MAKEUP_DB = -0.6 * (LIMIT_DB + (0 - LIMIT_DB) / LIMIT_RATIO)

/** The flanger's delay sweeps between these (s). */
const FLANGE_MIN = 0.0007
const FLANGE_MAX = 0.007

/** The longest delay (s): 8 × a 1-beat setting at 120 BPM. */
const MAX_DELAY_S = 4

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
 * One channel's FILTER FX (decided 2026-11-03, after Traktor's mixer): ONE
 * knob, -1..1, out of the path at the centre. It always FILTERS — low-pass
 * left, high-pass right; over the `filterRange` setting for the Filter alone,
 * the musical range under an effect (`rangeFor`) — and the channel's
 * chosen effect rides with it, fed the FILTERED signal, its amount growing
 * with the knob — and the more filter, the longer the reverb and delay and
 * the faster the flanger: Reverb and Delay are sends whose tails ring out
 * after the knob returns (echo- and wash-outs); Bit crusher replaces the
 * filtered signal with a crushed copy; Flanger mixes it with a few-ms delay
 * swept by a beat-synced LFO, with feedback.
 * `filter` is the filter alone. Everything is built
 * once and switched by gains, so a change never clicks. Each effect's one
 * parameter is an app setting (`FxSettings`).
 */
class FilterFx {
  readonly input: GainNode
  readonly output: GainNode
  private readonly ctx: AudioContext
  private readonly dry: GainNode
  /** Two stages, 24 dB/octave: the first Butterworth, the second carrying the
   *  resonance. One 12 dB stage left a 100 Hz bass only 28 dB down at the end
   *  of a full-range low-pass (measured) — not the near-silence asked for. */
  private readonly filterIn: BiquadFilterNode
  private readonly filter: BiquadFilterNode
  /** The filtered music itself (every effect but Bit crusher keeps it). */
  private readonly filtered: GainNode
  // Reverb: sends into a SHORT and a LONG convolver; the knob blends from one
  // into the other (re-generating one impulse instead would cut its tail).
  private readonly reverbShort: GainNode
  private readonly reverbLong: GainNode
  private readonly convShort: ConvolverNode
  private readonly convLong: ConvolverNode
  private reverbSize = -1
  // Delay: a send into a delay with filtered feedback.
  private readonly delaySend: GainNode
  private readonly delay: DelayNode
  private readonly delayTone: BiquadFilterNode
  private readonly feedback: GainNode
  // Bit crusher.
  private readonly crushDrive: GainNode
  private readonly shaper: WaveShaperNode
  private readonly crushWet: GainNode
  private crushBits = -1
  // Flanger: a few-ms delay swept by one LFO, with feedback.
  private readonly lfo: OscillatorNode
  private readonly flange: DelayNode
  private readonly flangeFeedback: GainNode
  private readonly flangerWet: GainNode

  constructor(ctx: AudioContext) {
    this.ctx = ctx
    this.input = ctx.createGain()
    this.output = ctx.createGain()
    this.dry = ctx.createGain()
    this.input.connect(this.dry).connect(this.output)

    this.filterIn = biquad(ctx, 'lowpass', 20000, BUTTERWORTH_DB)
    this.filter = biquad(ctx, 'lowpass', 20000, 0)
    this.input.connect(this.filterIn).connect(this.filter)
    this.filtered = ctx.createGain()
    this.filtered.gain.value = 0
    this.filter.connect(this.filtered).connect(this.output)

    this.reverbShort = ctx.createGain()
    this.reverbShort.gain.value = 0
    this.reverbLong = ctx.createGain()
    this.reverbLong.gain.value = 0
    this.convShort = ctx.createConvolver()
    this.convLong = ctx.createConvolver()
    this.filter.connect(this.reverbShort).connect(this.convShort).connect(this.output)
    this.filter.connect(this.reverbLong).connect(this.convLong).connect(this.output)

    this.delaySend = ctx.createGain()
    this.delaySend.gain.value = 0
    this.delay = ctx.createDelay(MAX_DELAY_S + 0.1)
    this.delayTone = biquad(ctx, 'lowpass', 2500, 0)
    this.feedback = ctx.createGain()
    this.feedback.gain.value = 0.45
    this.filter.connect(this.delaySend).connect(this.delay)
    this.delay.connect(this.delayTone).connect(this.feedback).connect(this.delay)
    this.delay.connect(this.output)

    this.crushDrive = ctx.createGain()
    this.shaper = ctx.createWaveShaper()
    this.crushWet = ctx.createGain()
    this.crushWet.gain.value = 0
    this.filter.connect(this.crushDrive).connect(this.shaper).connect(this.crushWet).connect(this.output)

    // A delay swept between FLANGE_MIN and FLANGE_MAX by the LFO, fed back
    // into itself; mixed with the filtered music its comb notches sweep.
    this.lfo = ctx.createOscillator()
    this.lfo.frequency.value = 0.5
    this.flange = ctx.createDelay(0.05)
    this.flange.delayTime.value = (FLANGE_MIN + FLANGE_MAX) / 2
    const depth = ctx.createGain()
    depth.gain.value = (FLANGE_MAX - FLANGE_MIN) / 2
    this.lfo.connect(depth).connect(this.flange.delayTime)
    this.flangeFeedback = ctx.createGain()
    this.flangeFeedback.gain.value = 0
    this.flangerWet = ctx.createGain()
    this.flangerWet.gain.value = 0
    this.filter.connect(this.flange)
    this.flange.connect(this.flangeFeedback).connect(this.flange)
    this.flange.connect(this.flangerWet).connect(this.output)
    this.lfo.start()
  }

  apply(type: FxType, knob: number, fx: FxSettings, bpm: number): void {
    const ctx = this.ctx
    const a = Math.min(1, Math.abs(knob))
    const on = a >= FX_CENTRE
    // The filtered path replaces the dry one over the first few percent of
    // the knob, so leaving the centre never jumps.
    const take = on ? Math.min(1, a / 0.06) : 0
    const sweep = filterSweep(knob, rangeFor(type, fx))
    if (sweep) {
      if (this.filter.type !== sweep.type) {
        this.filterIn.type = sweep.type
        this.filter.type = sweep.type
      }
      ramp(this.filterIn.frequency, sweep.freq, ctx)
      ramp(this.filter.frequency, sweep.freq, ctx)
      // Resonance fades over the last stretch: a resonant peak at 20 Hz (or
      // 20 kHz) at the end of a full sweep would be a boom (or a whistle).
      ramp(this.filter.Q, fx.filterResonance * 15 * (1 - a ** 4), ctx)
    }
    ramp(this.dry.gain, 1 - take, ctx)

    // How much of the filtered music stays: all of it, but half under the
    // flanger (whose notches come from mixing the two) and none under the
    // crusher (which replaces it).
    const flanging = type === 'flanger' && on
    const mix = flanging ? Math.min(1, a / 0.25) : 0
    const crushing = type === 'crusher' && on
    ramp(this.filtered.gain, crushing ? 0 : take * (1 - 0.5 * mix), ctx)
    ramp(this.flangerWet.gain, take * 0.5 * mix, ctx)
    // The sweep speeds up and the feedback (the "jet") deepens as it opens.
    ramp(this.flangeFeedback.gain, flanging ? 0.3 + 0.5 * a : 0, ctx, 0.05)
    if (type === 'flanger') ramp(this.lfo.frequency, 1 / beatSeconds(sweepBeatsAt(fx.flangerBeats, a), bpm), ctx, 0.05)

    // Reverb: the send grows with the knob, and moves from the short reverb
    // into the long one — the more filter, the longer the tail.
    const reverbing = type === 'reverb' && on
    ramp(this.reverbShort.gain, reverbing ? a * (1 - a) : 0, ctx)
    ramp(this.reverbLong.gain, reverbing ? a * a : 0, ctx)
    if (type === 'reverb') {
      const size = Math.round(Math.max(0.3, Math.min(6, fx.reverbSize)) * 10) / 10
      if (size !== this.reverbSize) {
        this.reverbSize = size
        this.convShort.buffer = impulse(ctx, size)
        this.convLong.buffer = impulse(ctx, Math.min(12, size * REVERB_GROWTH))
      }
    }

    // Delay: send, feedback AND length grow with the knob (the length in
    // doubling steps, `delayBeatsAt`); the repeats darken (left) or thin
    // (right) as they go round.
    const delaying = type === 'delay' && on
    ramp(this.delaySend.gain, delaying ? a * 0.8 : 0, ctx)
    ramp(this.feedback.gain, delaying ? 0.35 + 0.5 * a : 0.45, ctx, 0.05)
    if (type === 'delay') {
      ramp(this.delay.delayTime, Math.min(MAX_DELAY_S, beatSeconds(delayBeatsAt(fx.delayBeats, a), bpm)), ctx, 0.05)
      const tone = knob < 0 ? 'lowpass' : 'highpass'
      if (this.delayTone.type !== tone) this.delayTone.type = tone
      ramp(this.delayTone.frequency, knob < 0 ? 2500 : 600, ctx)
    }

    // Bit crusher: fewer bits as the knob opens (16 → 3), driven by the setting.
    if (crushing) {
      const bits = Math.round(16 - a * 13)
      if (bits !== this.crushBits) {
        this.crushBits = bits
        this.shaper.curve = crushCurve(bits)
      }
      ramp(this.crushDrive.gain, fx.crusherDrive, ctx)
    }
    ramp(this.crushWet.gain, crushing ? take / Math.max(1, fx.crusherDrive) ** 0.5 : 0, ctx)
  }
}

class Strip {
  readonly input: GainNode
  readonly meter: AnalyserNode
  private readonly ctx: AudioContext
  private readonly trim: GainNode
  private readonly bands: [GainNode, GainNode, GainNode]
  private readonly fx: FilterFx
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

    this.fx = new FilterFx(ctx)
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

