// The RAW waveform: the main view's alternative to bars (the toggle under its
// zoom buttons, persisted as `mainWaveRaw`), after Traktor at full zoom — a thin
// signed line of the actual signal, its colour shifting with the frequency mix.
// The bars (`waveform.ts`) draw ~7 ms columns of unsigned band energy, which
// hides where a kick actually starts; this draws the samples.
//
// Decided (discussion 2026-10-15): the SHAPE is exact and the COLOUR is
// approximate — the tint comes from the bars' ~7 ms band columns, in the bars'
// palette blended in OKLab (`bandColour`), lerped between columns. The vertical scale is fixed by the track's
// loudest sample, so relative loudness is truthful and the height never pumps
// while scrolling.
//
// Two data paths, by how many samples one pixel covers:
//   * many (wide zoom): min/max per RAW_BLOCK samples, summarised once at load
//     (~4 MB for a 6-minute track) — a 64 s view reads ~90k block values;
//   * few (deep zoom): the visible window read straight from the decoded
//     AudioBuffer each frame. No full-length mono copy is kept; the playback
//     engine holds the buffer anyway.
// Each pixel is a vertical stroke from the lowest to the highest sample in it,
// stretched to touch its neighbour's — zoomed out it reads as a filled
// envelope, zoomed in it narrows to one continuous line, with no switch-over.

import type { WaveColumn } from './waveform'

/** Samples per entry of the min/max summary. */
export const RAW_BLOCK = 32
/** Below this many samples per pixel, read samples instead of blocks: a pixel
 *  then spans at least 4 blocks, so block edges smear it by at most a quarter. */
const SAMPLE_PATH_BELOW = 4 * RAW_BLOCK

export interface RawWave {
  sampleRate: number
  /** Samples in the track. */
  length: number
  /** Min / max of the mono downmix per RAW_BLOCK samples. */
  min: Float32Array
  max: Float32Array
  /** The loudest absolute sample — the fixed vertical scale. */
  peak: number
  /** Fill `dest` with the mono downmix from sample `start` (zeros outside). */
  readMono: (start: number, dest: Float32Array) => void
}

/** A reader of an AudioBuffer's mono downmix. copyFromChannel copies, so this
 *  never holds a view the playback engine could detach (the Firefox gotcha). */
export function bufferReader(buffer: AudioBuffer): RawWave['readMono'] {
  let tmp = new Float32Array(0)
  const chs = buffer.numberOfChannels
  return (start, dest) => {
    dest.fill(0)
    const from = Math.max(0, start)
    const skip = from - start
    const n = Math.min(dest.length - skip, buffer.length - from)
    if (n <= 0) return
    if (tmp.length < n) tmp = new Float32Array(n)
    const view = tmp.subarray(0, n)
    for (let c = 0; c < chs; c++) {
      buffer.copyFromChannel(view, c, from)
      for (let i = 0; i < n; i++) dest[skip + i] += view[i] / chs
    }
  }
}

/** Summarise a mono downmix. `readMono` defaults to reading `mono` itself
 *  (tests); the deck passes `bufferReader` so `mono` can be dropped. */
export function rawWaveFromMono(
  mono: Float32Array,
  sampleRate: number,
  readMono?: RawWave['readMono'],
): RawWave {
  const len = mono.length
  const nb = Math.max(1, Math.ceil(len / RAW_BLOCK))
  const min = new Float32Array(nb)
  const max = new Float32Array(nb)
  let peak = 0
  for (let b = 0; b < nb; b++) {
    let lo = Infinity
    let hi = -Infinity
    const end = Math.min(len, (b + 1) * RAW_BLOCK)
    for (let i = b * RAW_BLOCK; i < end; i++) {
      const v = mono[i]
      if (v < lo) lo = v
      if (v > hi) hi = v
    }
    if (lo === Infinity) lo = hi = 0
    min[b] = lo
    max[b] = hi
    peak = Math.max(peak, -lo, hi)
  }
  return {
    sampleRate,
    length: len,
    min,
    max,
    peak,
    readMono:
      readMono ??
      ((start, dest) => {
        dest.fill(0)
        for (let i = 0; i < dest.length; i++) {
          const j = start + i
          if (j >= 0 && j < len) dest[i] = mono[j]
        }
      }),
  }
}

/** Reusable per-pixel output of `rawSpans`. */
export interface RawSpans {
  lo: Float32Array
  hi: Float32Array
  /** Track time at each bin's centre (for the colour). */
  t: Float64Array
  /** Bins filled; bin k is drawn at device pixel `round(x0 + k)`. */
  n: number
  x0: number
  scratch: Float32Array
}

export function makeSpans(): RawSpans {
  return { lo: new Float32Array(0), hi: new Float32Array(0), t: new Float64Array(0), n: 0, x0: 0, scratch: new Float32Array(0) }
}

/**
 * Per-pixel min/max of `raw` across a `w`-pixel view of [startSec, endSec).
 * Bins sit on a TRACK-space grid (bin g covers [g, g+1) pixel widths of track
 * time), so a scrolling view moves bins rather than regrouping samples under
 * them — the same reason `paintWave` bins in track space: regrouping makes
 * every peak shimmer as the playhead advances. Bins outside the track are NaN.
 */
export function rawSpans(raw: RawWave, startSec: number, endSec: number, w: number, out: RawSpans): RawSpans {
  const secPerPx = (endSec - startSec) / w
  const spp = secPerPx * raw.sampleRate // samples per pixel
  const g0 = Math.floor(startSec / secPerPx)
  const n = w + 2
  if (out.lo.length < n) {
    out.lo = new Float32Array(n)
    out.hi = new Float32Array(n)
    out.t = new Float64Array(n)
  }
  out.n = n
  out.x0 = g0 - startSec / secPerPx
  const { lo, hi, t } = out
  for (let k = 0; k < n; k++) t[k] = (g0 + k + 0.5) * secPerPx

  if (spp >= SAMPLE_PATH_BELOW) {
    const nb = raw.min.length
    for (let k = 0; k < n; k++) {
      const s0 = (g0 + k) * spp
      const s1 = s0 + spp
      if (s1 <= 0 || s0 >= raw.length) {
        lo[k] = hi[k] = NaN
        continue
      }
      const b0 = Math.max(0, Math.floor(s0 / RAW_BLOCK))
      const b1 = Math.min(nb, Math.max(b0 + 1, Math.ceil(s1 / RAW_BLOCK)))
      let a = Infinity
      let z = -Infinity
      for (let b = b0; b < b1; b++) {
        if (raw.min[b] < a) a = raw.min[b]
        if (raw.max[b] > z) z = raw.max[b]
      }
      lo[k] = a
      hi[k] = z
    }
  } else {
    // One read covers the window, plus a sample each side for interpolation.
    const first = Math.floor(g0 * spp) - 1
    const count = Math.ceil(n * spp) + 3
    if (out.scratch.length < count) out.scratch = new Float32Array(count)
    const win = out.scratch.subarray(0, count)
    raw.readMono(first, win)
    for (let k = 0; k < n; k++) {
      const s0 = (g0 + k) * spp
      const s1 = s0 + spp
      if (s1 <= 0 || s0 >= raw.length) {
        lo[k] = hi[k] = NaN
        continue
      }
      const i0 = Math.ceil(s0)
      const i1 = Math.ceil(s1)
      let a = Infinity
      let z = -Infinity
      for (let i = i0; i < i1; i++) {
        const v = win[i - first]
        if (v < a) a = v
        if (v > z) z = v
      }
      if (a === Infinity) {
        // Narrower than a sample: the line's value at the bin's centre.
        const c = (s0 + s1) / 2
        const i = Math.floor(c)
        const f = c - i
        a = z = win[i - first] * (1 - f) + win[i + 1 - first] * f
      }
      lo[k] = a
      hi[k] = z
    }
  }

  // Join each stroke to the previous one (by its ORIGINAL extent), so a steep
  // edge stays a continuous line instead of a dotted one.
  let pLo = NaN
  let pHi = NaN
  for (let k = 0; k < n; k++) {
    const a = lo[k]
    const z = hi[k]
    if (a === a && pLo === pLo) {
      if (a > pHi) lo[k] = pHi
      if (z < pLo) hi[k] = pLo
    }
    pLo = a
    pHi = z
  }
  return out
}

/** Pixels per unit amplitude for each stem lane: each stem at its own peak,
 *  but never below 5% of the loudest stem's — the same floor as the bar lanes,
 *  so a near-silent stem draws as near-silence, not magnified noise. */
export function laneScales(peaks: number[], halfHeight: number): number[] {
  const loudest = Math.max(1e-6, ...peaks)
  return peaks.map((p) => halfHeight / Math.max(p, loudest * 0.05, 1e-6))
}

// The bars' band colours (waveform.ts LAYERS), as RGB.
const BANDS = [
  [255, 138, 61], // bass
  [143, 107, 255], // mids
  [94, 231, 255], // highs
]

// sRGB <-> OKLab (Björn Ottosson's matrices).
function toLab([r, g, b]: number[]): number[] {
  const lin = (v: number) => ((v /= 255) <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)
  const R = lin(r)
  const G = lin(g)
  const B = lin(b)
  const l = Math.cbrt(0.4122214708 * R + 0.5363325363 * G + 0.0514459929 * B)
  const m = Math.cbrt(0.2119034982 * R + 0.6806995451 * G + 0.1073969566 * B)
  const s = Math.cbrt(0.0883024619 * R + 0.2817188376 * G + 0.6299787005 * B)
  return [
    0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  ]
}

function fromLab([L, A, B]: number[]): number[] {
  const l = (L + 0.3963377774 * A + 0.2158037573 * B) ** 3
  const m = (L - 0.1055613458 * A - 0.0638541728 * B) ** 3
  const s = (L - 0.0894841775 * A - 1.291485548 * B) ** 3
  const enc = (v: number) => {
    v = Math.min(1, Math.max(0, v))
    return 255 * (v <= 0.0031308 ? 12.92 * v : 1.055 * v ** (1 / 2.4) - 0.055)
  }
  return [
    enc(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
    enc(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
    enc(-0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s),
  ]
}

const BAND_LAB = BANDS.map(toLab)
const BAND_CHROMA = BAND_LAB.map(([, a, b]) => Math.hypot(a, b))

/**
 * A column's colour from its band energies. Mixed in OKLab, then the CHROMA is
 * put back to the bands' weighted chroma: a plain weighted mean of orange,
 * violet and cyan lands near grey (they sit around the hue wheel), which read
 * as mud on busy passages. Chosen 2026-10-15 from five candidates rendered on
 * real tracks: orange + violet blends through pink (Traktor-like), highs stay
 * cyan, and nothing greys out. Lifted a touch so it reads on the dark well.
 */
function bandColour(low: number, mid: number, high: number): number[] {
  const w = [low, mid, high]
  const sum = low + mid + high
  if (sum < 1e-6) return BANDS[0]
  const mix = [0, 1, 2].map((c) => BAND_LAB.reduce((acc, lab, b) => acc + lab[c] * w[b], 0) / sum)
  const chroma = BAND_CHROMA.reduce((acc, ch, b) => acc + ch * w[b], 0) / sum
  const k = chroma / Math.max(1e-6, Math.hypot(mix[1], mix[2]))
  return fromLab([mix[0] + 0.04, mix[1] * k, mix[2] * k])
}

const columnRgb = new WeakMap<WaveColumn[], Float32Array>()

/** Each band column's colour (`bandColour`), computed once per analysis. */
function rgbOf(cols: WaveColumn[]): Float32Array {
  let rgb = columnRgb.get(cols)
  if (rgb) return rgb
  rgb = new Float32Array(cols.length * 3)
  for (let i = 0; i < cols.length; i++) rgb.set(bandColour(cols[i].low, cols[i].mid, cols[i].high), i * 3)
  columnRgb.set(cols, rgb)
  return rgb
}

const styles = new Map<number, string>()

/** The tint at track time `t`: lerped between the two nearest band columns,
 *  quantised to 32 levels a channel so a frame reuses a small set of strings. */
export function tintAt(cols: WaveColumn[], duration: number, t: number): string {
  const rgb = rgbOf(cols)
  const n = cols.length
  const c = Math.min(n - 1, Math.max(0, (t / duration) * n - 0.5))
  const i = Math.floor(c)
  const j = Math.min(n - 1, i + 1)
  const f = c - i
  let key = 0
  for (let ch = 0; ch < 3; ch++) {
    const v = rgb[i * 3 + ch] * (1 - f) + rgb[j * 3 + ch] * f
    key = (key << 5) | Math.min(31, Math.round(v / 8.226))
  }
  let s = styles.get(key)
  if (!s) {
    const r = ((key >> 10) & 31) * 8.226
    const g = ((key >> 5) & 31) * 8.226
    const b = (key & 31) * 8.226
    s = `rgb(${r | 0},${g | 0},${b | 0})`
    styles.set(key, s)
  }
  return s
}

/**
 * Paint spans as a line centred in [y0, y0 + h). `scale` is pixels per unit
 * amplitude; `thick` the minimum stroke (device px), so a flat stretch still
 * reads as a line. `colour` is a fixed style or a per-bin function (by time).
 */
export function paintRaw(
  ctx: CanvasRenderingContext2D,
  s: RawSpans,
  y0: number,
  h: number,
  scale: number,
  thick: number,
  colour: string | ((t: number) => string),
): void {
  const mid = y0 + h / 2
  const fixed = typeof colour === 'string'
  if (fixed) ctx.fillStyle = colour
  let current = ''
  const wpx = Math.max(1, Math.round(thick * 0.8))
  for (let k = 0; k < s.n; k++) {
    const a = s.lo[k]
    if (a !== a) continue
    let top = mid - s.hi[k] * scale
    let bot = mid - a * scale
    if (bot - top < thick) {
      const c = (top + bot) / 2
      top = c - thick / 2
      bot = c + thick / 2
    }
    if (!fixed) {
      const style = colour(s.t[k])
      if (style !== current) {
        ctx.fillStyle = style
        current = style
      }
    }
    ctx.fillRect(Math.round(s.x0 + k), top, wpx, bot - top)
  }
}
