// Frequency waveform analysis + painting.
//
// The audio is downmixed to mono and split into three frequency bands with
// native BiquadFilters (via OfflineAudioContext — fast, off the main thread).
// Per output column we keep each band's RMS energy, normalised to that band's
// loudest moment in the track.
//
// Painting draws the three bands as LAYERS rather than blending them into one
// colour per column: bass (orange) is drawn first and tallest, then mids
// (violet) and highs (cyan) on top with a screen blend, so where bands overlap
// they mix into light. A kick reads as an orange flare, a hat as a cyan tick,
// and the structure of a track — where the bass drops out, where the highs
// build — is visible at a glance. Still no green, matching Traktor's palette.

export interface WaveColumn {
  peak: number // 0..1 amplitude of the full mix
  low: number // 0..1 band energies, each normalised to its own maximum
  mid: number
  high: number
}

// Band crossover choices. Independent filters, so band energies don't strictly
// partition — that's fine, each band is normalised on its own.
const LOW_HZ = 250
const MID_HZ = 1200
const MID_Q = 0.6
const HIGH_HZ = 5000

// Each layer's colour and its height relative to the waveform's half-height.
// Bass is the envelope; mids and highs sit inside it so all three stay visible.
const LAYERS: { band: 'low' | 'mid' | 'high'; color: string; scale: number; blend: GlobalCompositeOperation }[] = [
  { band: 'low', color: 'rgba(255,138,61,0.95)', scale: 0.96, blend: 'source-over' },
  { band: 'mid', color: 'rgba(143,107,255,0.8)', scale: 0.66, blend: 'screen' },
  { band: 'high', color: 'rgba(94,231,255,0.85)', scale: 0.46, blend: 'screen' },
]

export interface PaintOptions {
  /** Bar width and the gap after it, in DEVICE pixels. A gap of 0 paints a
   *  solid waveform (the overview); a gap gives the main view its bar texture. */
  bar: number
  gap: number
}

/**
 * Paint a slice of the analysed columns across a canvas: source columns in the
 * fractional range [startFrac, endFrac) (of the whole track) are mapped across
 * the full canvas width. Fractions outside [0,1] paint blank — used by the
 * scrolling main view so the lead-in/lead-out past the track edges stays empty.
 *
 * Bars are binned in TRACK space, not screen space: bar k always covers the
 * same columns of audio, and only its x moves as the view scrolls. Binning by
 * screen pixel instead makes every bar's height flicker as the playhead
 * advances, because each frame regroups different columns under each bar.
 */
export function paintWave(
  ctx: CanvasRenderingContext2D,
  cols: WaveColumn[],
  w: number,
  h: number,
  startFrac: number,
  endFrac: number,
  opts: PaintOptions = { bar: 1, gap: 0 },
): void {
  const n = cols.length
  if (n === 0 || w === 0 || h === 0 || endFrac <= startFrac) return
  const mid = h / 2
  const colsPerPx = ((endFrac - startFrac) * n) / w
  const step = opts.bar + opts.gap
  const colsPerBar = step * colsPerPx
  const startCol = startFrac * n
  const k0 = Math.floor(Math.max(0, startCol) / colsPerBar)
  const k1 = Math.ceil(Math.min(n, endFrac * n) / colsPerBar)
  const prevOp = ctx.globalCompositeOperation
  for (const layer of LAYERS) {
    ctx.globalCompositeOperation = layer.blend
    ctx.fillStyle = layer.color
    const reach = (mid - 1) * layer.scale
    for (let k = k0; k < k1; k++) {
      const c0 = k * colsPerBar
      const i0 = Math.max(0, Math.floor(c0))
      const i1 = Math.min(n, Math.max(i0 + 1, Math.ceil(c0 + colsPerBar)))
      let v = 0
      for (let i = i0; i < i1; i++) {
        const e = cols[i][layer.band]
        if (e > v) v = e
      }
      if (v <= 0) continue
      const x = (c0 - startCol) / colsPerPx
      const half = Math.max(0.5, v * reach)
      ctx.fillRect(x, mid - half, opts.bar, half * 2)
    }
  }
  ctx.globalCompositeOperation = prevOp
}

export interface WaveformAnalysis {
  cols: WaveColumn[]
  /** The decoded audio, retained for the scratch engine (reused, not re-decoded). */
  buffer: AudioBuffer
}

export async function analyzeWaveform(url: string, buckets?: number): Promise<WaveformAnalysis> {
  const resp = await fetch(url)
  if (!resp.ok) throw new Error(`audio fetch failed: ${resp.status}`)
  const arr = await resp.arrayBuffer()

  const AC: typeof AudioContext =
    window.AudioContext ?? (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
  const ctx = new AC()
  let audio: AudioBuffer
  try {
    audio = await ctx.decodeAudioData(arr)
  } finally {
    void ctx.close()
  }

  const len = audio.length
  const sr = audio.sampleRate
  const chs = audio.numberOfChannels

  // Resolution scales with track length (~140 columns/sec) so the zoomed-in
  // main waveform stays crisp; clamped so short/long tracks stay reasonable.
  if (buckets == null) {
    const seconds = len / sr
    buckets = Math.min(48000, Math.max(1600, Math.round(seconds * 140)))
  }

  // Downmix to mono.
  const mono = new Float32Array(len)
  for (let c = 0; c < chs; c++) {
    const d = audio.getChannelData(c)
    for (let i = 0; i < len; i++) mono[i] += d[i] / chs
  }

  const scale = buckets / len
  const lowE = new Float64Array(buckets)
  const midE = new Float64Array(buckets)
  const highE = new Float64Array(buckets)
  const peak = new Float64Array(buckets)
  const cnt = new Float64Array(buckets)

  // Amplitude peak + sample count per column (from the mono mix).
  for (let i = 0; i < len; i++) {
    const b = (i * scale) | 0
    const a = Math.abs(mono[i])
    if (a > peak[b]) peak[b] = a
    cnt[b]++
  }

  // Render one band through a native filter, accumulate its energy, then let the
  // rendered buffer be GC'd before the next band (keeps peak memory to ~2 copies).
  const accumBand = async (
    target: Float64Array,
    type: BiquadFilterType,
    freq: number,
    q?: number,
  ) => {
    const off = new OfflineAudioContext(1, len, sr)
    const buf = off.createBuffer(1, len, sr)
    buf.copyToChannel(mono, 0)
    const src = off.createBufferSource()
    src.buffer = buf
    const filter = off.createBiquadFilter()
    filter.type = type
    filter.frequency.value = freq
    if (q != null) filter.Q.value = q
    src.connect(filter)
    filter.connect(off.destination)
    src.start()
    const rendered = await off.startRendering()
    const data = rendered.getChannelData(0)
    for (let i = 0; i < len; i++) {
      const b = (i * scale) | 0
      target[b] += data[i] * data[i]
    }
  }

  await accumBand(lowE, 'lowpass', LOW_HZ)
  await accumBand(midE, 'bandpass', MID_HZ, MID_Q)
  await accumBand(highE, 'highpass', HIGH_HZ)

  // Per-band normalisation, to the band's 99.5th-percentile column rather than
  // its single loudest (one clipped transient would otherwise shrink the whole
  // band). Each band then gets an EXPANDING curve: dance music keeps its bass
  // near full scale for whole sections, so a linear (or compressing) map draws
  // a solid wall where the eye wants to see the kick pulse. The bass curve is
  // steepest, since it carries the rhythm; highs stay near linear so hats and
  // risers keep their detail.
  const rms = (e: Float64Array) => {
    const out = new Float64Array(e.length)
    for (let b = 0; b < e.length; b++) out[b] = Math.sqrt(e[b] / (cnt[b] || 1))
    return out
  }
  const ceiling = (v: Float64Array) => {
    const sorted = Float64Array.from(v).sort()
    return Math.max(1e-9, sorted[Math.floor((sorted.length - 1) * 0.995)])
  }
  const lowR = rms(lowE)
  const midR = rms(midE)
  const highR = rms(highE)
  const maxL = ceiling(lowR)
  const maxM = ceiling(midR)
  const maxH = ceiling(highR)
  let maxPeak = 1e-6
  for (let b = 0; b < buckets; b++) if (peak[b] > maxPeak) maxPeak = peak[b]
  const curve = (v: number, k: number) => Math.pow(Math.min(1, v), k)

  const cols: WaveColumn[] = new Array(buckets)
  for (let b = 0; b < buckets; b++) {
    cols[b] = {
      peak: peak[b] / maxPeak,
      low: curve(lowR[b] / maxL, 1.8),
      mid: curve(midR[b] / maxM, 1.3),
      high: curve(highR[b] / maxH, 1.05),
    }
  }
  return { cols, buffer: audio }
}
