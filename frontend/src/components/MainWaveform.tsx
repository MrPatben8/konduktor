import { useEffect, useRef } from 'react'
import type { CuePoint } from '../api'
import { drawBeatgrid, drawCuePoint, drawCues, drawLoop } from '../lib/cues'
import { paintWave, type WaveColumn } from '../lib/waveform'

interface Props {
  cols: WaveColumn[]
  currentTime: number
  duration: number
  cues: CuePoint[]
  cuePoint: number | null
  bpm: number | null
  gridAnchor: number | null
  loop: { start: number; end: number } | null
  /** Seconds across the view — the zoom level, owned by the parent so it
      survives track switches and persists to prefs. */
  secPerView: number
  onZoomChange: (secPerView: number) => void
  onSeek: (t: number) => void
  onScratchStart: () => void
  onScratchMove: (t: number) => void
  onScratchEnd: (t: number) => void
}

const DRAG_THRESHOLD = 3 // px before a press becomes a scratch (vs a click-seek)

export const MIN_SEC = 2 // most zoomed-in (seconds across the view)
export const MAX_SEC = 64 // most zoomed-out
export const DEFAULT_SEC = 16

/**
 * Traktor-style scrolling main waveform: the playhead is fixed at the centre and
 * the waveform scrolls beneath it. Shows a time window [t - sec/2, t + sec/2];
 * the zoom buttons change that window width. Repaints every frame (driven by the
 * parent's rAF-updated currentTime).
 */
export function MainWaveform({
  cols,
  currentTime,
  duration,
  cues,
  cuePoint,
  bpm,
  gridAnchor,
  loop,
  secPerView,
  onZoomChange,
  onSeek,
  onScratchStart,
  onScratchMove,
  onScratchEnd,
}: Props) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  // Drag state for scratching. startTime is the playhead position at press;
  // dragging maps horizontal movement to a time offset (right = earlier, so the
  // waveform follows the cursor like grabbing a record).
  const dragRef = useRef<{ startX: number; startTime: number; lastT: number; moved: boolean } | null>(
    null,
  )

  // Draw closes over the latest props/state; a ref lets the ResizeObserver call
  // the current version without re-subscribing.
  const draw = () => {
    const canvas = canvasRef.current
    const wrap = wrapRef.current
    if (!canvas || !wrap) return
    const dpr = window.devicePixelRatio || 1
    const w = Math.max(1, Math.floor(wrap.clientWidth * dpr))
    const h = Math.max(1, Math.floor(wrap.clientHeight * dpr))
    if (canvas.width !== w) canvas.width = w
    if (canvas.height !== h) canvas.height = h
    const ctx = canvas.getContext('2d')!
    ctx.clearRect(0, 0, w, h)

    if (cols.length && duration > 0) {
      const half = secPerView / 2
      const startSec = currentTime - half
      const endSec = currentTime + half
      paintWave(ctx, cols, w, h, startSec / duration, endSec / duration)

      const timeToX = (t: number) => ((t - startSec) / secPerView) * w
      // Loop band (under the grid/cues), then beatgrid, then cue markers.
      if (loop) {
        drawLoop(ctx, timeToX(loop.start), timeToX(loop.end), h, dpr)
      }
      if (bpm && gridAnchor != null) {
        drawBeatgrid(ctx, bpm, gridAnchor, startSec, endSec, w, h, dpr)
      }
      if (cues.length) {
        drawCues(ctx, cues, w, h, dpr, timeToX, true)
      }
      if (cuePoint != null && cuePoint >= startSec && cuePoint <= endSec) {
        drawCuePoint(ctx, Math.round(timeToX(cuePoint)), h, dpr)
      }
    }

    // Fixed centre playhead — red core with a translucent black outline so it
    // separates from the waveform, plus inward-pointing triangle caps top and
    // bottom that anchor the eye in the quiet margins.
    const core = Math.max(2, Math.round(2 * dpr))
    const edge = Math.max(1, Math.round(dpr))
    const cx = Math.floor(w / 2)
    const px = cx - Math.floor(core / 2)
    ctx.fillStyle = 'rgba(0,0,0,0.5)'
    ctx.fillRect(px - edge, 0, core + edge * 2, h)
    ctx.fillStyle = '#ff3b30'
    ctx.fillRect(px, 0, core, h)

    const cap = Math.round(6 * dpr)
    ctx.fillStyle = '#ff3b30'
    ctx.beginPath()
    ctx.moveTo(cx - cap, 0)
    ctx.lineTo(cx + cap, 0)
    ctx.lineTo(cx, cap)
    ctx.closePath()
    ctx.fill()
    ctx.beginPath()
    ctx.moveTo(cx - cap, h)
    ctx.lineTo(cx + cap, h)
    ctx.lineTo(cx, h - cap)
    ctx.closePath()
    ctx.fill()
  }

  const drawRef = useRef(draw)
  drawRef.current = draw

  // Redraw after every render (covers currentTime advancing, zoom, new track).
  useEffect(() => {
    draw()
  })

  useEffect(() => {
    const wrap = wrapRef.current
    if (!wrap) return
    const ro = new ResizeObserver(() => drawRef.current())
    ro.observe(wrap)
    return () => ro.disconnect()
  }, [])

  // A press that doesn't move is a seek (map offset from centre to a time);
  // a press that moves is a scratch (map movement to a target time).
  const timeAtClientX = (clientX: number, rect: DOMRect) => {
    const ratioFromCenter = (clientX - rect.left) / rect.width - 0.5
    return Math.min(duration, Math.max(0, currentTime + ratioFromCenter * secPerView))
  }

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!duration) return
    e.currentTarget.setPointerCapture(e.pointerId)
    dragRef.current = { startX: e.clientX, startTime: currentTime, lastT: currentTime, moved: false }
  }

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const d = dragRef.current
    if (!d) return
    const rect = e.currentTarget.getBoundingClientRect()
    const dx = e.clientX - d.startX
    if (!d.moved && Math.abs(dx) > DRAG_THRESHOLD) {
      d.moved = true
      onScratchStart()
    }
    if (d.moved) {
      const t = Math.min(duration, Math.max(0, d.startTime - (dx / rect.width) * secPerView))
      d.lastT = t
      onScratchMove(t)
    }
  }

  const onPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    const d = dragRef.current
    dragRef.current = null
    if (!d) return
    e.currentTarget.releasePointerCapture?.(e.pointerId)
    if (d.moved) onScratchEnd(d.lastT)
    else onSeek(timeAtClientX(e.clientX, e.currentTarget.getBoundingClientRect()))
  }

  const zoomIn = () => onZoomChange(Math.max(MIN_SEC, secPerView / 2))
  const zoomOut = () => onZoomChange(Math.min(MAX_SEC, secPerView * 2))

  return (
    <div
      ref={wrapRef}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      className="relative h-full w-full cursor-ew-resize select-none touch-none"
    >
      <canvas ref={canvasRef} className="block h-full w-full" />

      {/* Zoom controls (Traktor-style +/-). stopPropagation on down so tapping a
          button doesn't begin a scratch drag. */}
      <div className="absolute right-2 top-2 flex flex-col gap-1">
        <button
          onPointerDown={(e) => e.stopPropagation()}
          onClick={zoomIn}
          disabled={secPerView <= MIN_SEC}
          className="flex h-6 w-6 items-center justify-center rounded border border-line bg-ink-950/70 text-sm text-text hover:border-accent disabled:opacity-30"
          title="Zoom in"
        >
          +
        </button>
        <button
          onPointerDown={(e) => e.stopPropagation()}
          onClick={zoomOut}
          disabled={secPerView >= MAX_SEC}
          className="flex h-6 w-6 items-center justify-center rounded border border-line bg-ink-950/70 text-sm text-text hover:border-accent disabled:opacity-30"
          title="Zoom out"
        >
          −
        </button>
      </div>
    </div>
  )
}
