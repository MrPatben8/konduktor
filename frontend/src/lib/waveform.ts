// Frequency-colored waveform analysis (Traktor "Spectrum" theme).
//
// The audio is downmixed to mono and split into three frequency bands with
// native BiquadFilters (via OfflineAudioContext — fast, off the main thread).
// Per output column we keep each band's RMS energy plus the amplitude peak.
// Colour = balance of the three bands (bass→red, mids→green, highs→blue);
// height = amplitude. This mirrors how Traktor's Spectrum waveform reads.

export interface WaveColumn {
  peak: number // 0..1 amplitude (column height)
  r: number // 0..1 colour channels (already contrast-boosted)
  g: number
  b: number
}

// Band crossover choices. Independent filters, so band energies don't strictly
// partition — that's fine, colour is normalised per column.
const LOW_HZ = 250
const MID_HZ = 1200
const MID_Q = 0.6
const HIGH_HZ = 5000

/**
 * Paint a slice of the analysed columns across a canvas: source columns in the
 * fractional range [startFrac, endFrac) (of the whole track) are mapped across
 * the full canvas width, aggregating multiple columns per pixel (max peak,
 * averaged colour). Fractions outside [0,1] paint blank — used by the scrolling
 * main view so the lead-in/lead-out past the track edges stays empty.
 */
export function paintWave(
  ctx: CanvasRenderingContext2D,
  cols: WaveColumn[],
  w: number,
  h: number,
  startFrac: number,
  endFrac: number,
): void {
  const n = cols.length
  if (n === 0 || w === 0 || h === 0) return
  const mid = h / 2
  const span = (endFrac - startFrac) * n
  for (let x = 0; x < w; x++) {
    const c0 = startFrac * n + (x / w) * span
    const c1 = startFrac * n + ((x + 1) / w) * span
    const i0 = Math.floor(c0)
    const i1 = Math.max(i0 + 1, Math.ceil(c1))
    let peak = 0
    let r = 0
    let g = 0
    let b = 0
    let count = 0
    for (let i = i0; i < i1; i++) {
      if (i < 0 || i >= n) continue
      const col = cols[i]
      if (col.peak > peak) peak = col.peak
      r += col.r
      g += col.g
      b += col.b
      count++
    }
    if (count === 0) continue // outside the track → blank pixel
    r /= count
    g /= count
    b /= count
    const half = Math.max(0.5, peak * (mid - 1))
    ctx.fillStyle = `rgb(${(r * 255) | 0},${(g * 255) | 0},${(b * 255) | 0})`
    ctx.fillRect(x, mid - half, 1, half * 2)
  }
}

export async function analyzeWaveform(url: string, buckets?: number): Promise<WaveColumn[]> {
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

  let maxPeak = 1e-6
  for (let b = 0; b < buckets; b++) if (peak[b] > maxPeak) maxPeak = peak[b]

  const cols: WaveColumn[] = new Array(buckets)
  for (let b = 0; b < buckets; b++) {
    const n = cnt[b] || 1
    const l = Math.sqrt(lowE[b] / n)
    const m = Math.sqrt(midE[b] / n)
    const h = Math.sqrt(highE[b] / n)
    // Normalise to the dominant band so colours stay vivid (brightness is not
    // tied to loudness — amplitude is shown by height, like Traktor). Gamma
    // sharpens the separation between bands.
    const mx = Math.max(l, m, h, 1e-9)
    cols[b] = {
      peak: peak[b] / maxPeak,
      r: Math.pow(l / mx, 1.3),
      g: Math.pow(m / mx, 1.3),
      b: Math.pow(h / mx, 1.3),
    }
  }
  return cols
}
