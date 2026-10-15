import { describe, expect, it } from 'vitest'

import { alignShift, mp3HeaderFrame } from './decodeAlign'
import { PROBE_MP3_BASE64 } from './decodeProbe'

/** A stereo MPEG-1 Layer III frame header (128 kbps, 44.1 kHz) followed by
 *  `tag` where a Xing/Info tag would sit, padded to 64 bytes. */
function frame(tag = '') {
  const b = new Uint8Array(64)
  b.set([0xff, 0xfb, 0x90, 0x00])
  for (let i = 0; i < tag.length; i++) b[4 + 32 + i] = tag.charCodeAt(i)
  return b
}

function withId3(body: Uint8Array, size = 1000) {
  const out = new Uint8Array(10 + size + body.length)
  out.set([0x49, 0x44, 0x33, 4, 0, 0])
  out.set([(size >> 21) & 127, (size >> 14) & 127, (size >> 7) & 127, size & 127], 6)
  out.set(body, 10 + size)
  return out
}

describe('mp3HeaderFrame', () => {
  it('sees a Xing / Info header, after an ID3 tag too', () => {
    expect(mp3HeaderFrame(frame('Xing'))).toBe(true)
    expect(mp3HeaderFrame(withId3(frame('Info'), 200_000))).toBe(true)
  })
  it('sees an MP3 without one', () => {
    expect(mp3HeaderFrame(frame())).toBe(false)
    expect(mp3HeaderFrame(withId3(frame()))).toBe(false)
  })
  it('reads the built-in probe as header-less', () => {
    const bytes = Uint8Array.from(atob(PROBE_MP3_BASE64), (c) => c.charCodeAt(0))
    expect(mp3HeaderFrame(bytes)).toBe(false)
  })
  it('is not fooled by a frame sync inside another format', () => {
    const m4a = new Uint8Array(96)
    m4a.set([0, 0, 0, 0x20, 0x66, 0x74, 0x79, 0x70]) // ....ftyp
    m4a.set(frame(), 16)
    expect(mp3HeaderFrame(m4a)).toBeNull()
    const flac = withId3(new Uint8Array([0x66, 0x4c, 0x61, 0x43, ...frame()])) // ID3 + fLaC
    expect(mp3HeaderFrame(flac)).toBeNull()
  })
})

describe('alignShift', () => {
  it("prepends what this engine trimmed, only on a header-less MP3", () => {
    // WebKit measured 529 samples early at 44.1 kHz.
    const early = -529 / 44100
    expect(alignShift(false, early, 44100)).toBe(529)
    expect(alignShift(false, early, 48000)).toBe(Math.round(529 * 48000 / 44100))
    expect(alignShift(true, early, 44100)).toBe(0)
    expect(alignShift(null, early, 44100)).toBe(0)
    expect(alignShift(false, 0, 44100)).toBe(0)
  })
})
