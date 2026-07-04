import type { CuePoint } from '../api'

// Default marker colours by Traktor cue type, used when a cue has no explicit
// colour of its own.
const TYPE_COLORS: Record<number, string> = {
  0: '#4f8cff', // cue — accent blue
  1: '#34d399', // fade-in — mint
  2: '#34d399', // fade-out — mint
  3: '#ffb020', // load — gold
  5: '#f472b6', // loop — pink
}

export function cueColor(cue: CuePoint): string {
  if (cue.color && /^#[0-9a-f]{6}$/i.test(cue.color)) return cue.color
  return TYPE_COLORS[cue.type] ?? '#8b93a3'
}

function withAlpha(hex: string, a: number): string {
  const r = parseInt(hex.slice(1, 3), 16)
  const g = parseInt(hex.slice(3, 5), 16)
  const b = parseInt(hex.slice(5, 7), 16)
  return `rgba(${r},${g},${b},${a})`
}

/**
 * Draw cue/loop markers onto a canvas. `timeToX` maps a time (seconds) to a
 * device-pixel x. With `labels`, a small numbered flag is drawn at the top for
 * hotcues (used on the main waveform); the overview passes labels=false.
 */
export function drawCues(
  ctx: CanvasRenderingContext2D,
  cues: CuePoint[],
  w: number,
  h: number,
  dpr: number,
  timeToX: (t: number) => number,
  labels: boolean,
): void {
  const lineW = Math.max(2, Math.round(2 * dpr))
  const flag = Math.round(13 * dpr)
  for (const cue of cues) {
    const color = cueColor(cue)
    const x = Math.round(timeToX(cue.start))

    // Loop region (len > 0): translucent band from start to end.
    if (cue.length > 0) {
      const x2 = Math.round(timeToX(cue.start + cue.length))
      if (x2 > 0 && x < w) {
        ctx.fillStyle = withAlpha(color, 0.18)
        ctx.fillRect(x, 0, Math.max(1, x2 - x), h)
      }
    }

    if (x < -flag || x > w + flag) continue
    ctx.fillStyle = color
    ctx.fillRect(x - Math.floor(lineW / 2), 0, lineW, h)

    if (labels && cue.hotcue >= 0) {
      ctx.fillStyle = color
      ctx.fillRect(x, 0, flag, flag)
      ctx.fillStyle = '#0a0b0f'
      ctx.font = `${Math.round(9 * dpr)}px system-ui, sans-serif`
      ctx.textBaseline = 'top'
      ctx.fillText(String(cue.hotcue + 1), x + Math.round(3 * dpr), Math.round(2 * dpr))
    }
  }
}

/**
 * Draw the beatgrid: beat lines extrapolated from the grid anchor at 60/bpm
 * spacing, across the visible window [startSec, endSec]. Downbeats (every 4th
 * beat from the anchor) are brighter; regular beats are hidden when too dense.
 */
export function drawBeatgrid(
  ctx: CanvasRenderingContext2D,
  bpm: number,
  gridAnchor: number,
  startSec: number,
  endSec: number,
  w: number,
  h: number,
  dpr: number,
): void {
  if (bpm <= 0 || endSec <= startSec) return
  const beat = 60 / bpm
  const span = endSec - startSec
  const beatPx = (beat / span) * w
  const showBeats = beatPx >= 6 * dpr
  let k = Math.ceil((startSec - gridAnchor) / beat)
  for (;;) {
    const bt = gridAnchor + k * beat
    if (bt > endSec) break
    const downbeat = ((k % 4) + 4) % 4 === 0
    k++
    if (bt < startSec) continue
    if (!downbeat && !showBeats) continue
    const x = Math.round(((bt - startSec) / span) * w)
    ctx.fillStyle = downbeat ? 'rgba(255,255,255,0.5)' : 'rgba(255,255,255,0.2)'
    ctx.fillRect(x, 0, downbeat ? Math.max(1, Math.round(dpr)) : 1, h)
  }
}
