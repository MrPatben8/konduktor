import type { CuePoint } from '../api'

// Marker colour by Traktor cue type. Type is authoritative here (the NML's
// stored per-cue colour is intentionally ignored) so each type reads consistently.
const TYPE_COLORS: Record<number, string> = {
  0: '#3b82f6', // cue — blue
  1: '#ff8c2f', // fade-in — orange
  2: '#ff8c2f', // fade-out — orange
  3: '#ffd60a', // load — yellow
  5: '#22c55e', // loop — green
}
const OTHER_COLOR = '#8b93a3' // gray

export function cueColor(cue: CuePoint): string {
  return TYPE_COLORS[cue.type] ?? OTHER_COLOR
}

function withAlpha(hex: string, a: number): string {
  const r = parseInt(hex.slice(1, 3), 16)
  const g = parseInt(hex.slice(3, 5), 16)
  const b = parseInt(hex.slice(5, 7), 16)
  return `rgba(${r},${g},${b},${a})`
}

// Black or white text, whichever reads better on the given marker colour.
export function contrastText(hex: string): string {
  const r = parseInt(hex.slice(1, 3), 16)
  const g = parseInt(hex.slice(3, 5), 16)
  const b = parseInt(hex.slice(5, 7), 16)
  return 0.299 * r + 0.587 * g + 0.114 * b > 140 ? '#0a0b0f' : '#ffffff'
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
  const outline = Math.max(1, Math.round(dpr))
  const flagW = Math.round(12 * dpr)
  const flagH = Math.round(14 * dpr)
  const half = Math.floor(lineW / 2)
  for (const cue of cues) {
    const color = cueColor(cue)
    const x = Math.round(timeToX(cue.start))

    // Loop region (len > 0): translucent band from start to end.
    if (cue.length > 0) {
      const x2 = Math.round(timeToX(cue.start + cue.length))
      if (x2 > 0 && x < w) {
        ctx.fillStyle = withAlpha(color, 0.2)
        ctx.fillRect(x, 0, Math.max(1, x2 - x), h)
      }
    }

    if (x < -flagW || x > w + flagW) continue

    // Dark outline behind the line so it reads on any waveform colour.
    ctx.fillStyle = 'rgba(0,0,0,0.7)'
    ctx.fillRect(x - half - outline, 0, lineW + outline * 2, h)
    ctx.fillStyle = color
    ctx.fillRect(x - half, 0, lineW, h)

    if (labels) {
      // A flag tab at the top for every cue (outlined), with the hotcue number.
      ctx.fillStyle = 'rgba(0,0,0,0.7)'
      ctx.fillRect(x - half - outline, 0, flagW + outline * 2, flagH + outline * 2)
      ctx.fillStyle = color
      ctx.fillRect(x - half, 0, flagW, flagH)
      if (cue.hotcue >= 0) {
        ctx.fillStyle = contrastText(color)
        ctx.font = `bold ${Math.round(10 * dpr)}px system-ui, sans-serif`
        ctx.textBaseline = 'top'
        ctx.fillText(String(cue.hotcue + 1), x - half + Math.round(2 * dpr), Math.round(2 * dpr))
      }
    }
  }
}

/**
 * Draw the floating "CUE" point: a gold vertical line with a dark outline and a
 * solid downward triangle tab at the top, distinct from numbered hotcue flags,
 * the white beatgrid, and the red playhead.
 */
export function drawCuePoint(
  ctx: CanvasRenderingContext2D,
  x: number,
  h: number,
  dpr: number,
): void {
  const lw = Math.max(2, Math.round(2 * dpr))
  const half = Math.floor(lw / 2)
  const outline = Math.max(1, Math.round(dpr))
  const tab = Math.round(8 * dpr)
  ctx.fillStyle = 'rgba(0,0,0,0.6)'
  ctx.fillRect(x - half - outline, 0, lw + outline * 2, h)
  ctx.fillStyle = '#ffb020'
  ctx.fillRect(x - half, 0, lw, h)
  ctx.beginPath()
  ctx.moveTo(x - tab, 0)
  ctx.lineTo(x + tab, 0)
  ctx.lineTo(x, tab)
  ctx.closePath()
  ctx.fill()
}

/** Draw the active loop region: a translucent green band with bright edges. */
export function drawLoop(
  ctx: CanvasRenderingContext2D,
  startX: number,
  endX: number,
  h: number,
  dpr: number,
): void {
  const edge = Math.max(1, Math.round(2 * dpr))
  ctx.fillStyle = 'rgba(34,197,94,0.18)'
  ctx.fillRect(startX, 0, Math.max(1, endX - startX), h)
  ctx.fillStyle = 'rgba(34,197,94,0.95)'
  ctx.fillRect(startX, 0, edge, h)
  ctx.fillRect(endX - edge, 0, edge, h)
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
  const outline = Math.max(1, Math.round(dpr))
  const beatW = Math.max(1, Math.round(dpr))
  const barW = Math.max(2, Math.round(2 * dpr))
  let k = Math.ceil((startSec - gridAnchor) / beat)
  for (;;) {
    const bt = gridAnchor + k * beat
    if (bt > endSec) break
    const downbeat = ((k % 4) + 4) % 4 === 0
    k++
    if (bt < startSec) continue
    if (!downbeat && !showBeats) continue
    const x = Math.round(((bt - startSec) / span) * w)
    const lw = downbeat ? barW : beatW
    // Dark outline so the line reads on any waveform colour, then a bright line.
    ctx.fillStyle = 'rgba(0,0,0,0.6)'
    ctx.fillRect(x - Math.floor(lw / 2) - outline, 0, lw + outline * 2, h)
    ctx.fillStyle = downbeat ? 'rgba(255,255,255,0.95)' : 'rgba(255,255,255,0.65)'
    ctx.fillRect(x - Math.floor(lw / 2), 0, lw, h)
  }
}
