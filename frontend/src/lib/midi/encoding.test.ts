import { describe, expect, it } from 'vitest'
import { absValue, Decoder, detectEncoding, relativeTicks, wrapDelta } from './encoding'
import { describeSource, parseMessage, parseSource, sourceKey, type MidiMessage } from './messages'

const cc = (number: number, value: number, channel = 1): MidiMessage => ({ kind: 'cc', channel, number, value })
const note = (number: number, value: number, channel = 1): MidiMessage => ({ kind: 'note', channel, number, value })

describe('messages', () => {
  it('parses notes, CCs and pitch bend, and ignores the rest', () => {
    expect(parseMessage([0x91, 11, 127])).toEqual({ kind: 'note', channel: 2, number: 11, value: 127 })
    expect(parseMessage([0x81, 11, 64])).toEqual({ kind: 'note', channel: 2, number: 11, value: 0 })
    expect(parseMessage([0x90, 11, 0])?.value).toBe(0)
    expect(parseMessage([0xb0, 33, 65])).toEqual({ kind: 'cc', channel: 1, number: 33, value: 65 })
    expect(parseMessage([0xe3, 0x7f, 0x7f])).toEqual({ kind: 'pb', channel: 4, number: 0, value: 16383 })
    expect(parseMessage([0xc0, 5])).toBeNull()
    expect(parseMessage([0xf8])).toBeNull()
  })
  it('names sources and reads them back', () => {
    expect(sourceKey(cc(33, 1, 2))).toBe('cc:2:33')
    expect(sourceKey({ kind: 'pb', channel: 1, number: 0 })).toBe('pb:1')
    expect(parseSource('note:16:127')).toEqual({ kind: 'note', channel: 16, number: 127 })
    expect(parseSource('cc:17:1')).toBeNull()
    expect(describeSource('cc:2:33')).toBe('CC 33 · Ch 2')
    expect(describeSource('cc:1:20+')).toBe('CC 20 · Ch 1 ↻')
    expect(parseSource('cc:1:20-')).toEqual({ kind: 'cc', channel: 1, number: 20 })
    expect(parseSource('note:1:20+')).toBeNull() // only a control that turns has a direction
  })
})

describe('decoding', () => {
  it('reads the three relative schemes', () => {
    expect([1, 3, 127, 125].map((v) => relativeTicks('relTwos', v))).toEqual([1, 3, -1, -3])
    expect([65, 67, 63, 61].map((v) => relativeTicks('rel64', v))).toEqual([1, 3, -1, -3])
    expect([1, 3, 65, 67].map((v) => relativeTicks('relSignMag', v))).toEqual([1, 3, -1, -3])
  })
  it('maps a knob centre to exactly 0 and its ends to ±1', () => {
    expect(absValue(64, 127).bipolar).toBe(0)
    expect(absValue(0, 127).bipolar).toBe(-1)
    expect(absValue(127, 127).bipolar).toBe(1)
    expect(absValue(8192, 16383).bipolar).toBe(0)
    expect(absValue(127, 127).unit).toBe(1)
  })
  it('steps the short way round a wheel', () => {
    expect(wrapDelta(120, 3, 128)).toBe(11)
    expect(wrapDelta(3, 120, 128)).toBe(-11)
    expect(wrapDelta(16380, 4, 16384)).toBe(8)
  })
  it('turns an absolute jog position into ticks across the wrap, both ways', () => {
    const d = new Decoder('jogAbs7')
    expect(d.feed(cc(16, 125))).toBeNull() // the first position only sets where we are
    expect(d.feed(cc(16, 127))).toEqual({ type: 'delta', ticks: 2 })
    expect(d.feed(cc(16, 2))).toEqual({ type: 'delta', ticks: 3 })
    expect(d.feed(cc(16, 126))).toEqual({ type: 'delta', ticks: -4 })
  })
  it('combines a 14-bit MSB/LSB pair, and pitch bend', () => {
    const d = new Decoder('abs14')
    d.feed(cc(0, 64))
    const v = d.feed(cc(32, 0), true)
    expect(v).toEqual({ type: 'abs', unit: 8192 / 16383, bipolar: 0 })
    const j = new Decoder('jogAbs14')
    expect(j.feed({ kind: 'pb', channel: 1, number: 0, value: 16380 })).toBeNull()
    expect(j.feed({ kind: 'pb', channel: 1, number: 0, value: 5 })).toEqual({ type: 'delta', ticks: 9 })
  })
  it('inverts positions and ticks', () => {
    expect(new Decoder('abs7', true).feed(cc(1, 0))).toEqual({ type: 'abs', unit: 1, bipolar: 1 })
    expect(new Decoder('rel64', true).feed(cc(1, 66))).toEqual({ type: 'delta', ticks: -2 })
  })
  it('treats any value above 0 as a press', () => {
    const b = new Decoder('button')
    expect(b.feed(note(1, 1))).toEqual({ type: 'press', down: true })
    expect(b.feed(cc(1, 0))).toEqual({ type: 'press', down: false })
  })
})

describe('detecting an encoding while learning', () => {
  const seq = (vals: number[], n = 20) => vals.map((v) => cc(n, v))
  it('a fader swept up and back is absolute', () => {
    expect(detectEncoding(seq([10, 14, 19, 25, 33, 40, 48, 41, 30, 22, 15, 9]), 'continuous')).toBe('abs7')
  })
  it('a knob moved a little near its centre, changing every message, is absolute', () => {
    expect(detectEncoding(seq([60, 61, 62, 63, 64, 65, 66, 67, 68, 67, 66, 65]), 'continuous')).toBe('abs7')
  })
  it('a jog reporting its position (wrapping) is an absolute jog', () => {
    expect(detectEncoding(seq([100, 108, 116, 124, 4, 12, 20, 28, 36, 44, 52, 60]), 'jog')).toBe('jogAbs7')
  })
  it('centred-on-64 relative, both ways and one way', () => {
    expect(detectEncoding(seq([65, 65, 66, 65, 63, 62, 63, 63]), 'jog')).toBe('rel64')
    expect(detectEncoding(seq([65, 65, 65, 66, 65]), 'jog')).toBe('rel64')
  })
  it('a fast centred jog jittering with speed is still relative', () => {
    expect(detectEncoding(seq([66, 70, 68, 73, 69, 72, 67, 71, 75, 70, 66, 69]), 'jog')).toBe('rel64')
  })
  it("two's complement and sign-magnitude", () => {
    expect(detectEncoding(seq([1, 1, 2, 1, 127, 127, 126]), 'jog')).toBe('relTwos')
    expect(detectEncoding(seq([1, 1, 2, 1, 65, 65, 66]), 'jog')).toBe('relSignMag')
  })
  it('a 14-bit pair, whichever half came first', () => {
    const pair = [cc(39, 3), cc(7, 40), cc(39, 90), cc(7, 41), cc(39, 12), cc(7, 42)]
    expect(detectEncoding([cc(7, 40), ...pair], 'continuous')).toBe('abs14')
    expect(detectEncoding([{ kind: 'pb', channel: 1, number: 0, value: 9000 }], 'continuous')).toBe('abs14')
    expect(detectEncoding([{ kind: 'pb', channel: 1, number: 0, value: 9000 }], 'jog')).toBe('jogAbs14')
  })
  it('a button is a button', () => {
    expect(detectEncoding([note(1, 127)], 'button')).toBe('button')
  })
})
