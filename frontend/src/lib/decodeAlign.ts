// The deck decodes audio in the BROWSER (decodeAudioData), while every stored
// position — grids, cues — is in the backend's decoded time base (PyAV/FFmpeg,
// `core/waveform.load`). Measured 2026-10-15 with click files: the two agree to
// the sample on every format EXCEPT a header-less MP3 under WebKit (the macOS
// app's WKWebView), which decodes 529 samples (12 ms) EARLY — CoreAudio trims
// the MP3 decoder delay even when no Xing/Info header says to, FFmpeg trims
// nothing. Chromium (WebView2) matches FFmpeg. Invisible under 7 ms bars, but
// the raw waveform would draw every beat line 12 ms off its kick.
//
// Rather than hard-code WebKit's number, this engine's offset is MEASURED once,
// by decoding a tiny built-in header-less MP3 (`decodeProbe.ts`) whose click
// FFmpeg places at a known sample, and a header-less MP3's decoded buffer is
// shifted by it — so playback, scratch and both waveforms share the grid's
// clock. An MP3 WITH a header was measured to agree on both engines and is
// never touched.

import { PROBE_MP3_BASE64, PROBE_ONSET, PROBE_RATE } from './decodeProbe'

/** How far past the ID3 tag to look for the first frame (junk, padding). */
const SCAN = 64 * 1024

interface Frame {
  mpeg1: boolean
  mono: boolean
}

/** A Layer III frame header at `i`, or null. The backend's
 *  `core/mp3_gapless._frame_header`, kept in step with it. */
function frameHeader(b: Uint8Array, i: number): Frame | null {
  if (i + 4 > b.length || b[i] !== 0xff || (b[i + 1] & 0xe0) !== 0xe0) return null
  const version = (b[i + 1] >> 3) & 3
  const layer = (b[i + 1] >> 1) & 3
  const bitrate = b[i + 2] >> 4
  const rate = (b[i + 2] >> 2) & 3
  if (version === 1 || layer !== 1 || bitrate === 0 || bitrate === 15 || rate === 3) return null
  return { mpeg1: version === 3, mono: b[i + 3] >> 6 === 3 }
}

const ascii = (b: Uint8Array, i: number, n: number) => String.fromCharCode(...b.subarray(i, i + n))

/**
 * Whether `bytes` is an MP3 whose first frame is a Xing / Info / VBRI header
 * (true), an MP3 without one (false), or not an MP3 at all (null). The header
 * test is the backend's `core/mp3_gapless.parse`; the "is it an MP3" gate is
 * extra, because this sees every format the deck plays and a stray frame sync
 * inside an M4A or FLAC must not count. An MP3 starts with an ID3v2 tag or with
 * a frame — nothing else this app plays does (FLAC, RIFF, ftyp, FORM, OggS).
 */
export function mp3HeaderFrame(b: Uint8Array): boolean | null {
  let start = 0
  if (b.length >= 10 && b[0] === 0x49 && b[1] === 0x44 && b[2] === 0x33) {
    const size = ((b[6] & 127) << 21) | ((b[7] & 127) << 14) | ((b[8] & 127) << 7) | (b[9] & 127)
    start = 10 + size + (b[5] & 0x10 ? 10 : 0)
    if (ascii(b, start, 4) === 'fLaC') return null // a FLAC carrying a (non-standard) ID3 tag
  } else if (!frameHeader(b, 0)) {
    return null
  }
  const end = Math.min(b.length - 4, start + SCAN)
  for (let i = start; i < end; i++) {
    const h = frameHeader(b, i)
    if (!h) continue
    const side = h.mpeg1 ? (h.mono ? 17 : 32) : h.mono ? 9 : 17
    const tag = ascii(b, i + 4 + side, 4)
    if (tag === 'Xing' || tag === 'Info') return true
    return ascii(b, i + 36, 4) === 'VBRI'
  }
  return null
}

/** The first sample of the probe's click (a quarter of its peak), or -1. */
function clickOnset(buf: AudioBuffer): number {
  const d = new Float32Array(buf.length)
  buf.copyFromChannel(d, 0)
  let peak = 0
  for (let i = 0; i < d.length; i++) peak = Math.max(peak, Math.abs(d[i]))
  if (peak <= 0) return -1
  for (let i = 0; i < d.length; i++) if (Math.abs(d[i]) > peak * 0.25) return i
  return -1
}

async function measure(): Promise<number> {
  const bytes = Uint8Array.from(atob(PROBE_MP3_BASE64), (c) => c.charCodeAt(0))
  const buf = await new OfflineAudioContext(1, 1, PROBE_RATE).decodeAudioData(bytes.buffer)
  const onset = clickOnset(buf)
  // An onset nowhere near the click means the probe did not decode sensibly;
  // shifting by it would be worse than the 12 ms it exists to fix.
  if (onset < 0 || Math.abs(onset - PROBE_ONSET) > 4096) return 0
  return (onset - PROBE_ONSET) / PROBE_RATE
}

let offset: Promise<number> | null = null

/** Seconds by which this engine decodes a header-less MP3 AHEAD of FFmpeg
 *  (negative = its audio starts earlier). Measured once per session; 0 if the
 *  probe cannot be decoded. */
export function headerlessMp3Offset(): Promise<number> {
  return (offset ??= measure().catch(() => 0))
}

/**
 * Samples to PREPEND (negative: drop from the start) to a buffer this engine
 * decoded, to put it in the backend's time base. `header` is
 * `mp3HeaderFrame` of the file's bytes, read before decoding detaches them.
 */
export function alignShift(header: boolean | null, offsetSec: number, sampleRate: number): number {
  return header === false ? Math.round(-offsetSec * sampleRate) || 0 : 0
}

/** `buffer`, decoded by this engine from a file whose `mp3HeaderFrame` is
 *  `header`, moved into the backend's time base (the same buffer if no move). */
export async function alignDecoded(buffer: AudioBuffer, header: boolean | null): Promise<AudioBuffer> {
  if (header !== false) return buffer
  const shift = alignShift(header, await headerlessMp3Offset(), buffer.sampleRate)
  if (shift === 0) return buffer
  const out = new AudioBuffer({
    length: Math.max(1, buffer.length + shift),
    numberOfChannels: buffer.numberOfChannels,
    sampleRate: buffer.sampleRate,
  })
  // copyFromChannel, never getChannelData: see the detach gotcha in scratchEngine.
  const tmp = new Float32Array(buffer.length)
  for (let c = 0; c < buffer.numberOfChannels; c++) {
    buffer.copyFromChannel(tmp, c)
    if (shift > 0) out.copyToChannel(tmp, c, shift)
    else out.copyToChannel(tmp.subarray(-shift), c, 0)
  }
  return out
}
