// The app's ambient colour: the backdrop's four washes and the accent hue,
// sampled from the loaded track's cover art.
//
// Everything is written as CSS custom properties on <html> (registered with
// @property in index.css, so a change GLIDES rather than snaps). Nothing in
// React state depends on it, which is what lets a new track recolour the whole
// app without a single re-render.

/** Used when there is no track, no art, or art with no real colour in it. */
const FALLBACK = {
  washes: ['#6d4aff', '#e0409a', '#12b5c9', '#2f3fe0'],
  hue: 275,
}

// Below this share of saturated pixels an image is treated as greyscale: an
// accent pulled from a black-and-white sleeve would be an accident of JPEG noise.
const MIN_COLOURFUL = 0.06

function apply(washes: string[], hue: number): void {
  const root = document.documentElement.style
  washes.forEach((c, i) => root.setProperty(`--amb-${i + 1}`, c))
  root.setProperty('--accent-hue', hue.toFixed(1))
}

export function resetAmbient(): void {
  apply(FALLBACK.washes, FALLBACK.hue)
}

function rgbToHsl(r: number, g: number, b: number): [number, number, number] {
  const mx = Math.max(r, g, b)
  const mn = Math.min(r, g, b)
  const l = (mx + mn) / 2
  if (mx === mn) return [0, 0, l]
  const d = mx - mn
  const s = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn)
  let h: number
  if (mx === r) h = (g - b) / d + (g < b ? 6 : 0)
  else if (mx === g) h = (b - r) / d + 2
  else h = (r - g) / d + 4
  return [h * 60, s, l]
}

/**
 * The image's dominant colours, as backdrop washes plus an accent hue.
 *
 * Pixels are binned by hue (12 bins) and weighted by how colourful they are, so
 * a large dull background loses to a smaller vivid subject — which is what the
 * eye picks out of a sleeve. Each wash is then pushed to a fixed saturation and
 * a mid lightness: the backdrop should carry the art's HUES, never its
 * brightness, or a white sleeve would wash the whole app out.
 */
function palette(data: Uint8ClampedArray): { washes: string[]; hue: number } | null {
  const BINS = 12
  const weight = new Float64Array(BINS)
  const sumX = new Float64Array(BINS) // hue averaged as a vector, so 350° + 10° = 0°
  const sumY = new Float64Array(BINS)
  let colourful = 0
  let total = 0
  for (let i = 0; i < data.length; i += 4) {
    if (data[i + 3] < 128) continue
    total++
    const [h, s, l] = rgbToHsl(data[i] / 255, data[i + 1] / 255, data[i + 2] / 255)
    const vivid = s * (1 - Math.abs(2 * l - 1))
    if (vivid < 0.12) continue
    colourful++
    const bin = Math.floor(h / (360 / BINS)) % BINS
    const w = vivid * vivid
    weight[bin] += w
    sumX[bin] += Math.cos((h * Math.PI) / 180) * w
    sumY[bin] += Math.sin((h * Math.PI) / 180) * w
  }
  if (!total || colourful / total < MIN_COLOURFUL) return null

  const ranked = Array.from({ length: BINS }, (_, i) => i)
    .filter((i) => weight[i] > 0)
    .sort((a, b) => weight[b] - weight[a])
  const hues = ranked.map((i) => {
    const deg = (Math.atan2(sumY[i], sumX[i]) * 180) / Math.PI
    return (deg + 360) % 360
  })
  // Fewer than four distinct hues (a duotone sleeve): rotate the strongest ones
  // slightly so the washes still move against each other instead of merging.
  while (hues.length < 4) hues.push((hues[0] + 28 * hues.length) % 360)

  const lightness = [52, 50, 46, 40]
  const washes = hues.slice(0, 4).map((h, i) => `hsl(${h.toFixed(0)} 78% ${lightness[i]}%)`)
  return { washes, hue: hues[0] }
}

let generation = 0

/**
 * Recolour the app from a cover-art URL (or reset it with null). Failures are
 * silent — a track without art, or art that cannot be decoded, keeps the
 * default look rather than surfacing an error for decoration.
 *
 * The image is fetched as a blob and decoded with createImageBitmap rather than
 * drawn from an <img>: the API is cross-origin under Tauri, and a cross-origin
 * <img> would taint the canvas and make its pixels unreadable.
 */
export async function setAmbientFromArt(url: string | null): Promise<void> {
  const mine = ++generation
  if (!url) {
    resetAmbient()
    return
  }
  try {
    const resp = await fetch(url)
    if (!resp.ok) throw new Error(String(resp.status))
    const bitmap = await createImageBitmap(await resp.blob(), {
      resizeWidth: 40,
      resizeHeight: 40,
      resizeQuality: 'low',
    })
    const canvas = document.createElement('canvas')
    canvas.width = 40
    canvas.height = 40
    const ctx = canvas.getContext('2d', { willReadFrequently: true })!
    ctx.drawImage(bitmap, 0, 0)
    bitmap.close()
    const found = palette(ctx.getImageData(0, 0, 40, 40).data)
    if (mine !== generation) return // a newer track was loaded meanwhile
    if (found) apply(found.washes, found.hue)
    else resetAmbient()
  } catch {
    if (mine === generation) resetAmbient()
  }
}
