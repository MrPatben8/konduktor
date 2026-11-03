// How a control encodes what it does, and turning its raw values into
// something an action can use: a press, a position, or a number of ticks.
//
// Controllers disagree on everything here, so a binding stores its encoding
// (detected while learning — `detectEncoding` — and overridable in the editor):
//   button      a note or CC: any value > 0 = down, 0 = up
//   abs7        a knob / fader, 0-127
//   abs14       a 14-bit knob / fader: CC n (MSB) + CC n+32 (LSB), or pitch bend
//   relTwos     relative, two's complement: 1.. = +1.., 127.. = -1..
//   rel64       relative, centred on 64: 65.. = +1.., 63.. = -1..
//   relSignMag  relative, sign + magnitude: 1.. = +1.., 65.. = -1..
//   jogAbs7     a jog reporting its ABSOLUTE position, 7 bits, wrapping
//   jogAbs14    the same at 14 bits (MSB/LSB pair, or pitch bend)

import type { MidiMessage } from './messages'

export const ENCODINGS = ['button', 'abs7', 'abs14', 'relTwos', 'rel64', 'relSignMag', 'jogAbs7', 'jogAbs14'] as const
export type Encoding = (typeof ENCODINGS)[number]

export const ENCODING_LABELS: Record<Encoding, string> = {
  button: 'Button',
  abs7: 'Absolute',
  abs14: 'Absolute 14-bit',
  relTwos: 'Relative (01 / 7F)',
  rel64: 'Relative (41 / 3F)',
  relSignMag: 'Relative (01 / 41)',
  jogAbs7: 'Jog position',
  jogAbs14: 'Jog position 14-bit',
}

/** Whether a 14-bit encoding pairs this CC with CC number + 32. */
export const isFourteenBit = (e: Encoding) => e === 'abs14' || e === 'jogAbs14'

/** What one message means once decoded. */
export type Decoded =
  | { type: 'press'; down: boolean }
  /** `unit` 0..1; `bipolar` -1..1 with the control's centre detent exactly 0. */
  | { type: 'abs'; unit: number; bipolar: number }
  | { type: 'delta'; ticks: number }

/** Two's-complement / centred / sign-magnitude value -> signed ticks. */
export function relativeTicks(encoding: Encoding, v: number): number {
  switch (encoding) {
    case 'relTwos':
      return v < 64 ? v : v - 128
    case 'rel64':
      return v - 64
    case 'relSignMag':
      return v < 64 ? v : -(v - 64)
    default:
      return 0
  }
}

/** The shortest signed step from position `prev` to `next` on a wheel of
 *  `size` positions: 120 -> 3 on a 7-bit jog is +11, not -117. */
export function wrapDelta(prev: number, next: number, size: number): number {
  let d = (next - prev) % size
  if (d > size / 2) d -= size
  if (d < -size / 2) d += size
  return d
}

/** A position on 0..max -> unit and bipolar, the centre value (max+1)/2
 *  mapping to exactly 0 (64 of 0-127: a knob's detent reads flat). */
export function absValue(raw: number, max: number): { unit: number; bipolar: number } {
  const c = (max + 1) / 2
  return { unit: raw / max, bipolar: raw < c ? (raw - c) / c : (raw - c) / (max - c) }
}

/**
 * Decodes one control's messages. Keeps the state an encoding needs between
 * them: the other half of a 14-bit pair, and an absolute jog's last position.
 * One per bound source (the router keys them by port + source).
 */
export class Decoder {
  private msb = 0
  private lsb = 0
  private lastPos: number | null = null

  readonly encoding: Encoding
  readonly invert: boolean

  constructor(encoding: Encoding, invert = false) {
    this.encoding = encoding
    this.invert = invert
  }

  /** `isLsb`: the message is the CC n+32 half of a 14-bit pair. */
  feed(m: MidiMessage, isLsb = false): Decoded | null {
    const e = this.encoding
    const flip = this.invert ? -1 : 1
    if (e === 'button') return { type: 'press', down: m.value > 0 }

    if (e === 'abs7') {
      const v = absValue(m.value, 127)
      return this.invert ? { unit: 1 - v.unit, bipolar: -v.bipolar, type: 'abs' } : { type: 'abs', ...v }
    }
    if (e === 'abs14' || e === 'jogAbs14') {
      let pos: number
      if (m.kind === 'pb') pos = m.value
      else {
        if (isLsb) this.lsb = m.value
        else this.msb = m.value
        pos = (this.msb << 7) | this.lsb
      }
      if (e === 'abs14') {
        const v = absValue(pos, 16383)
        return this.invert ? { unit: 1 - v.unit, bipolar: -v.bipolar, type: 'abs' } : { type: 'abs', ...v }
      }
      return this.jog(pos, 16384, flip)
    }
    if (e === 'jogAbs7') return this.jog(m.value, 128, flip)
    const ticks = relativeTicks(e, m.value) * flip
    return ticks === 0 ? null : { type: 'delta', ticks }
  }

  private jog(pos: number, size: number, flip: number): Decoded | null {
    const prev = this.lastPos
    this.lastPos = pos
    if (prev == null) return null // the first position only sets where we are
    const ticks = wrapDelta(prev, pos, size) * flip
    return ticks === 0 ? null : { type: 'delta', ticks }
  }
}

/** What the action being learned expects, which narrows the guess. */
export type LearnKind = 'button' | 'continuous' | 'jog'

/**
 * Guess a control's encoding from the messages it sent while being learned
 * (the user was asked to move it both ways). The tells:
 *   - Pitch bend, or CC n with CC n+32 interleaved, is 14-bit.
 *   - A relative control REPEATS values (65 65 65 while turning steadily) and
 *     stays in its zones (near 0 / 127, or near 64); an absolute one changes on
 *     every message and wanders across its range.
 *   - Which relative scheme: 63s mean centred on 64; 127s mean two's
 *     complement; 1s with 65s mean sign-magnitude; one direction only is
 *     ambiguous between schemes and falls back to the common one.
 * Wrong guesses are rare and fixable in the editor.
 */
export function detectEncoding(samples: MidiMessage[], kind: LearnKind): Encoding {
  if (kind === 'button' || samples.length === 0) return 'button'
  const absolute = kind === 'jog' ? 'jogAbs' : 'abs'
  const first = samples[0]
  if (first.kind === 'pb') return `${absolute}14` as Encoding
  if (first.kind === 'cc' && first.number < 32 && samples.some((m) => m.kind === 'cc' && m.number === first.number + 32)) {
    return `${absolute}14` as Encoding
  }
  const vals = samples.filter((m) => m.kind === first.kind && m.number === first.number).map((m) => m.value)

  let repeats = 0
  for (let i = 1; i < vals.length; i++) if (vals[i] === vals[i - 1]) repeats++
  const inZones = vals.every((v) => v <= 16 || v >= 112 || (v >= 48 && v <= 80))
  const distinct = new Set(vals).size
  // An absolute control moved both ways turns back once or twice; a relative
  // one at varying speed jitters up and down all the time.
  let reversals = 0
  let lastSign = 0
  for (let i = 1; i < vals.length; i++) {
    const d = Math.sign(wrapDelta(vals[i - 1], vals[i], 128))
    if (d === 0) continue
    if (lastSign !== 0 && d !== lastSign) reversals++
    lastSign = d
  }
  const relative = repeats >= 2 || (inZones && (distinct <= 6 || reversals > 2))
  if (!relative) return `${absolute}7` as Encoding

  const has = (lo: number, hi: number) => vals.some((v) => v >= lo && v <= hi)
  if (has(48, 63)) return 'rel64'
  if (has(112, 127)) return 'relTwos'
  if (has(1, 16) && has(65, 80)) return 'relSignMag'
  if (has(65, 80)) return 'rel64'
  return 'relTwos'
}
