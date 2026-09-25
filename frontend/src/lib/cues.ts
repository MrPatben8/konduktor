import type { CuePoint, CueType } from '../api'
import { BEATS_PER_BAR, type BeatGrid } from './beatgrid'

// Fallback colour by cue type, used when the platform stored none.
const TYPE_COLORS: Record<CueType, string> = {
  cue: '#4d94ff', // blue
  fade_in: '#ff9a3d', // orange
  fade_out: '#ff9a3d', // orange
  load: '#ffd23d', // yellow
  loop: '#3ddc84', // green
}
const MEMORY_COLOR = '#9aa1b2' // gray

export const CUE_TYPE_LABELS: Record<CueType, string> = {
  cue: 'Cue',
  fade_in: 'Fade-In',
  fade_out: 'Fade-Out',
  load: 'Load',
  loop: 'Loop',
}

// A second, always-present channel for type. Colour used to be type-derived and
// therefore always carried it; now that a user's stored colour wins, the glyph
// is what keeps a fade-in distinguishable from a plain cue.
const TYPE_GLYPHS: Record<CueType, string> = {
  cue: '',
  fade_in: '▶',
  fade_out: '◀',
  load: '⏏',
  loop: '⟳',
}

/**
 * Colour for a cue marker or button.
 *
 * Stored colour wins where the platform has one: on Rekordbox and Serato it is
 * the attribute the user actually set, and overriding it with a type palette
 * would discard their own organisation. Traktor rarely stores one, so in a
 * Traktor library the type palette still drives virtually every cue and the
 * existing look is preserved.
 */
export function cueColor(cue: CuePoint): string {
  if (cue.color) return cue.color
  if (cue.role === 'memory') return MEMORY_COLOR
  return TYPE_COLORS[cue.type] ?? MEMORY_COLOR
}

export function cueTypeColor(type: CueType): string {
  return TYPE_COLORS[type] ?? MEMORY_COLOR
}

export function cueGlyph(cue: CuePoint): string {
  return TYPE_GLYPHS[cue.type] ?? ''
}

export function withAlpha(hex: string, a: number): string {
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
 * device-pixel x. With `labels`, a rounded flag is drawn at the top for each
 * cue carrying its slot label — and its name, where the cue has one (used on
 * the main waveform); the overview passes labels=false.
 *
 * Lines glow in their own colour rather than sitting on a black outline: on the
 * glass deck a dark outline reads as a gap in the waveform, where a glow reads
 * as the line being lit.
 */
export function drawCues(
  ctx: CanvasRenderingContext2D,
  cues: CuePoint[],
  w: number,
  h: number,
  dpr: number,
  timeToX: (t: number) => number,
  labels: boolean,
  /** How a bank slot is labelled — "1".."8" or "A".."H", per the platform. */
  slotLabel: (slot: number) => string = (slot) => String(slot + 1),
): void {
  const lineW = Math.max(2, Math.round(2 * dpr))
  const half = Math.floor(lineW / 2)
  const tabH = Math.round(17 * dpr)
  const pad = Math.round(6 * dpr)
  const radius = Math.round(8 * dpr)
  const inset = Math.round(5 * dpr)
  ctx.font = `600 ${Math.round(11 * dpr)}px 'Geist Variable', system-ui, sans-serif`
  ctx.textBaseline = 'middle'
  for (const cue of cues) {
    const color = cueColor(cue)
    const x = Math.round(timeToX(cue.start))

    // Loop region (len > 0): translucent band from start to end.
    if (cue.length > 0) {
      const x2 = Math.round(timeToX(cue.start + cue.length))
      if (x2 > 0 && x < w) {
        ctx.fillStyle = withAlpha(color, 0.16)
        ctx.fillRect(x, 0, Math.max(1, x2 - x), h)
      }
    }

    // A flag can extend well to the right of its line; keep drawing while any
    // of it could be on screen.
    if (x < -w || x > w + lineW) continue

    ctx.save()
    ctx.shadowColor = color
    ctx.shadowBlur = 10 * dpr
    ctx.fillStyle = color
    ctx.fillRect(x - half, 0, lineW, h)
    ctx.restore()

    if (labels) {
      // A banked cue's flag carries its slot label (plus its name when it has
      // a meaningful one); a cue without a slot (a memory cue) gets a short,
      // unlabelled flag, so it still reads as a real object rather than an
      // anonymous line.
      const banked = cue.slot != null
      const slotText = banked ? slotLabel(cue.slot!) : ''
      const name = cue.name && cue.name !== 'n.n.' ? cue.name : ''
      const text = slotText && name ? `${slotText}  ${name}` : slotText
      const tw = text ? Math.ceil(ctx.measureText(text).width) : 0
      const fw = text ? tw + pad * 2 : Math.round(8 * dpr)
      const fh = banked ? tabH : Math.round(tabH / 2)
      ctx.fillStyle = withAlpha(color, 0.9)
      ctx.beginPath()
      ctx.roundRect(x - half, inset, fw, fh, [0, radius, radius, 0])
      ctx.fill()
      if (text) {
        ctx.fillStyle = contrastText(color)
        ctx.fillText(text, x - half + pad, inset + fh / 2 + dpr * 0.5)
      }
    }
  }
}

/**
 * Draw the floating "CUE" point: a gold vertical line with a solid downward
 * triangle tab at the top, distinct from hotcue flags, the white beatgrid, and
 * the red playhead.
 */
export function drawCuePoint(
  ctx: CanvasRenderingContext2D,
  x: number,
  h: number,
  dpr: number,
): void {
  const lw = Math.max(2, Math.round(2 * dpr))
  const half = Math.floor(lw / 2)
  const tab = Math.round(7 * dpr)
  ctx.save()
  ctx.shadowColor = '#ffc861'
  ctx.shadowBlur = 10 * dpr
  ctx.fillStyle = '#ffc861'
  ctx.fillRect(x - half, 0, lw, h)
  ctx.beginPath()
  ctx.moveTo(x - tab, 0)
  ctx.lineTo(x + tab, 0)
  ctx.lineTo(x, tab)
  ctx.closePath()
  ctx.fill()
  ctx.restore()
}

/** Draw the active loop region: a green band, brightest at the top, with lit edges. */
export function drawLoop(
  ctx: CanvasRenderingContext2D,
  startX: number,
  endX: number,
  h: number,
  dpr: number,
): void {
  const edge = Math.max(1, Math.round(1.5 * dpr))
  const band = ctx.createLinearGradient(0, 0, 0, h)
  band.addColorStop(0, 'rgba(61,220,132,0.22)')
  band.addColorStop(1, 'rgba(61,220,132,0.08)')
  ctx.fillStyle = band
  ctx.fillRect(startX, 0, Math.max(1, endX - startX), h)
  ctx.save()
  ctx.shadowColor = '#3ddc84'
  ctx.shadowBlur = 8 * dpr
  ctx.fillStyle = 'rgba(61,220,132,0.9)'
  ctx.fillRect(startX, 0, edge, h)
  ctx.fillRect(endX - edge, 0, edge, h)
  ctx.restore()
}

/**
 * The main waveform's fixed centre playhead: a red line with a soft red glow
 * and inward-pointing caps top and bottom that anchor the eye in the quiet
 * margins. Shared with the overview so the two can never drift apart in look.
 */
export function drawPlayhead(
  ctx: CanvasRenderingContext2D,
  x: number,
  h: number,
  dpr: number,
  caps: boolean,
): void {
  const core = Math.max(2, Math.round(2 * dpr))
  const px = x - Math.floor(core / 2)
  ctx.save()
  ctx.shadowColor = '#ff4d5e'
  ctx.shadowBlur = 12 * dpr
  ctx.fillStyle = '#ff4d5e'
  ctx.fillRect(px, 0, core, h)
  if (caps) {
    const cap = Math.round(6 * dpr)
    ctx.beginPath()
    ctx.moveTo(x - cap, 0)
    ctx.lineTo(x + cap, 0)
    ctx.lineTo(x, cap)
    ctx.closePath()
    ctx.fill()
    ctx.beginPath()
    ctx.moveTo(x - cap, h)
    ctx.lineTo(x + cap, h)
    ctx.lineTo(x, h - cap)
    ctx.closePath()
    ctx.fill()
  }
  ctx.restore()
}

/** Grid marker line colours. Markers are structural, so they are never culled
 *  by the density check the way ordinary beat lines are. */
export const GRID_MARKER_COLOR = '#9fb0ff' // periwinkle — an inactive marker
export const GRID_MARKER_ACTIVE_COLOR = '#ffc861' // gold — governs the playhead

/**
 * Draw the beatgrid across the visible window [startSec, endSec].
 *
 * The grid is a list of tempo segments, so beats are drawn per segment at that
 * segment's own spacing, and downbeats restart at every marker. Marker lines go
 * on top; the segment governing the playhead is tinted and its marker drawn in
 * gold, so it is visible which marker the grid controls are acting on.
 */
export function drawBeatgrid(
  ctx: CanvasRenderingContext2D,
  grid: BeatGrid,
  startSec: number,
  endSec: number,
  w: number,
  h: number,
  dpr: number,
  activeMarker: number,
  /** Grid mode: the grid is what is being edited, so it is drawn brighter. */
  emphasize = false,
): void {
  if (endSec <= startSec) return
  const span = endSec - startSec
  const toX = (t: number) => Math.round(((t - startSec) / span) * w)
  const beatW = Math.max(1, Math.round(dpr))
  const barW = Math.max(2, Math.round(2 * dpr))

  // 1. Tint the active segment. Only meaningful on a flexible grid — on a
  // constant one it would just wash the whole canvas for no information.
  if (grid.count > 1) {
    const seg = grid.segments[activeMarker]
    if (seg && seg.start < endSec && seg.end > startSec) {
      const x1 = toX(Math.max(seg.start, startSec))
      const x2 = toX(Math.min(seg.end, endSec))
      ctx.fillStyle = 'rgba(255,200,97,0.07)'
      ctx.fillRect(x1, 0, Math.max(1, x2 - x1), h)
    }
  }

  // 2. Beat lines, per segment at its own spacing. Density culling is therefore
  // per-segment too: a fast segment can cull while its slower neighbour does not.
  for (const seg of grid.segmentsIn(startSec, endSec)) {
    const beatPx = (seg.beatDur / span) * w
    if (beatPx * BEATS_PER_BAR < 3 * dpr) continue // even bars are sub-pixel
    const showBeats = beatPx >= 6 * dpr
    let jFrom = Math.ceil((startSec - seg.start) / seg.beatDur)
    // Only segment 0 extrapolates backwards; later segments start at their marker.
    if (seg.index > 0) jFrom = Math.max(0, jFrom)
    const jLimit = Math.floor((Math.min(endSec, seg.end) - seg.start) / seg.beatDur)
    const jTo = Math.min(seg.beatCount - 1, jLimit)
    for (let j = jFrom; j <= jTo; j++) {
      const downbeat = ((j % BEATS_PER_BAR) + BEATS_PER_BAR) % BEATS_PER_BAR === 0
      if (!downbeat && !showBeats) continue
      const bt = seg.start + j * seg.beatDur
      if (bt < startSec || bt > endSec) continue
      const x = toX(bt)
      const lw = downbeat ? barW : beatW
      // Translucent white with no outline: the grid should read as a ruler laid
      // over the waveform, not bars cut through it. Downbeats are clearly
      // brighter, which is what grid editing needs to see.
      ctx.fillStyle = downbeat
        ? `rgba(255,255,255,${emphasize ? 0.9 : 0.62})`
        : `rgba(255,255,255,${emphasize ? 0.5 : 0.26})`
      ctx.fillRect(x - Math.floor(lw / 2), 0, lw, h)
    }
  }

  // 3. Marker lines on top, with a tab at the bottom edge (the top is already
  // occupied by drawCues' hotcue flags).
  for (const i of grid.markersIn(startSec, endSec)) {
    const active = i === activeMarker
    const color = active ? GRID_MARKER_ACTIVE_COLOR : GRID_MARKER_COLOR
    const lw = Math.max(2, Math.round((active ? 3 : 2) * dpr))
    const x = toX(grid.markers[i].start)
    ctx.save()
    ctx.shadowColor = color
    ctx.shadowBlur = 10 * dpr
    ctx.fillStyle = color
    ctx.fillRect(x - Math.floor(lw / 2), 0, lw, h)
    const tab = Math.round(7 * dpr)
    ctx.beginPath()
    ctx.moveTo(x, h - tab)
    ctx.lineTo(x + tab, h)
    ctx.lineTo(x - tab, h)
    ctx.closePath()
    ctx.fill()
    ctx.restore()
  }
}
