import { useEffect, useRef } from 'react'
import type { CuePoint } from '../api'
import type { BeatGrid } from '../lib/beatgrid'
import { drawBeatgrid, drawCuePoint, drawCues, drawLoop, drawLoopIn, drawPlayhead } from '../lib/cues'
import { paintLane, paintWave, type WaveColumn } from '../lib/waveform'

interface Props {
  cols: WaveColumn[]
  currentTime: number
  duration: number
  cues: CuePoint[]
  cuePoint: number | null
  grid: BeatGrid | null
  /** Marker governing the playhead — drawn gold, its segment tinted. */
  activeMarker: number
  /** Grid mode is on: draw the beat lines brighter. */
  emphasizeGrid?: boolean
  loop: { start: number; end: number } | null
  /** A loop that exists but is switched off — drawn as a dim outline. */
  idleLoop?: { start: number; end: number } | null
  /** An armed manual loop-in point, waiting for OUT. */
  loopIn?: number | null
  /** Seconds across the view — the zoom level, owned by the parent so it
      survives track switches and persists to prefs. */
  secPerView: number
  onZoomChange: (secPerView: number) => void
  onSeek: (t: number) => void
  onScratchStart: () => void
  onScratchMove: (t: number) => void
  onScratchEnd: (t: number) => void
  /** A stem track: one lane per stem instead of the three-band mix view. */
  lanes?: StemLane[] | null
  /** Click = mute toggle; Alt/Option-click = solo (mute all the others). */
  onStemToggle?: (index: number, solo: boolean) => void
}

export interface StemLane {
  data: Float32Array
  name: string
  color: string
  /** Not muted. */
  audible: boolean
  /** The only stem playing (drawn with a ring). */
  solo: boolean
  /** Its keyboard shortcut, for the tooltip ("Q"). */
  shortcut: string
}

const ALT = typeof navigator !== 'undefined' && /Mac/.test(navigator.platform) ? 'Option' : 'Alt'

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
  grid,
  activeMarker,
  emphasizeGrid = false,
  loop,
  idleLoop = null,
  loopIn = null,
  secPerView,
  onZoomChange,
  onSeek,
  onScratchStart,
  onScratchMove,
  onScratchEnd,
  lanes = null,
  onStemToggle,
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
      // 2 px bars with a 1 px gap: the texture that makes the scrolling view
      // read as discrete energy rather than a smear.
      const bars = { bar: Math.max(1, Math.round(2 * dpr)), gap: Math.max(1, Math.round(dpr)) }
      if (lanes && lanes.length) {
        // Stem lanes, Traktor-style: each stem its own strip in its own colour.
        const laneH = h / lanes.length
        lanes.forEach((lane, k) => {
          paintLane(ctx, lane.data, lane.color, w, k * laneH, laneH, startSec / duration, endSec / duration, bars,
            !lane.audible)
        })
        ctx.fillStyle = 'rgba(255,255,255,0.06)'
        for (let k = 1; k < lanes.length; k++) ctx.fillRect(0, Math.round(k * laneH), w, Math.max(1, Math.round(dpr)))
      } else {
        paintWave(ctx, cols, w, h, startSec / duration, endSec / duration, bars)
      }

      const timeToX = (t: number) => ((t - startSec) / secPerView) * w
      // Loop band (under the grid/cues), then beatgrid, then cue markers.
      if (loop) {
        drawLoop(ctx, timeToX(loop.start), timeToX(loop.end), h, dpr)
      } else if (idleLoop) {
        drawLoop(ctx, timeToX(idleLoop.start), timeToX(idleLoop.end), h, dpr, true)
      }
      // An armed IN, and the loop OUT would make right now: IN → playhead.
      if (loopIn != null) drawLoopIn(ctx, timeToX(loopIn), w / 2, h, dpr)
      if (grid) {
        drawBeatgrid(ctx, grid, startSec, endSec, w, h, dpr, activeMarker, emphasizeGrid)
      }
      if (cues.length) {
        drawCues(ctx, cues, w, h, dpr, timeToX, true)
      }
      if (cuePoint != null && cuePoint >= startSec && cuePoint <= endSec) {
        drawCuePoint(ctx, Math.round(timeToX(cuePoint)), h, dpr)
      }
    }

    drawPlayhead(ctx, Math.floor(w / 2), h, dpr, true)
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

  // Cmd/Ctrl + scroll zooms (a trackpad pinch arrives as ctrl+wheel too).
  // Attached by hand because React's onWheel is passive, so it could not stop
  // the page scrolling. Accumulated so a trackpad's many tiny deltas step the
  // zoom as evenly as a mouse wheel's notches do.
  const zoomRef = useRef({ secPerView, onZoomChange })
  zoomRef.current = { secPerView, onZoomChange }
  useEffect(() => {
    const wrap = wrapRef.current
    if (!wrap) return
    let acc = 0
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) return
      e.preventDefault()
      acc += e.deltaY
      if (Math.abs(acc) < 40) return
      const { secPerView: sec, onZoomChange: set } = zoomRef.current
      set(Math.min(MAX_SEC, Math.max(MIN_SEC, acc > 0 ? sec * 2 : sec / 2)))
      acc = 0
    }
    wrap.addEventListener('wheel', onWheel, { passive: false })
    return () => wrap.removeEventListener('wheel', onWheel)
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

      {/* One mute button per stem lane, at its left edge. stopPropagation on
          down, like the zoom buttons, so a click never starts a scratch. */}
      {lanes && lanes.length > 0 && (
        <div className="absolute inset-y-0 left-1.5 flex flex-col">
          {lanes.map((lane, k) => (
            <div key={k} className="flex flex-1 items-center">
              <button
                onPointerDown={(e) => e.stopPropagation()}
                onClick={(e) => onStemToggle?.(k, e.altKey)}
                title={`${lane.name} — click to mute, ${ALT}-click to mute all the others (${lane.shortcut})`}
                aria-pressed={lane.audible}
                className={`flex h-5 min-w-5 items-center justify-center rounded-md px-1 font-mono text-[10px] font-semibold backdrop-blur-md transition-opacity ${
                  lane.audible ? '' : 'opacity-40'
                } ${lane.solo ? 'ring-1 ring-white/80' : ''}`}
                style={{
                  color: lane.audible ? '#0b0b0f' : lane.color,
                  background: lane.audible ? lane.color : 'rgba(255,255,255,0.06)',
                }}
              >
                {lane.name.slice(0, 1).toUpperCase()}
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Zoom controls (Traktor-style +/-). stopPropagation on down so tapping a
          button doesn't begin a scratch drag. */}
      <div className="absolute right-2 top-2 flex flex-col gap-1">
        <button
          onPointerDown={(e) => e.stopPropagation()}
          onClick={zoomIn}
          disabled={secPerView <= MIN_SEC}
          className="btn-glass flex h-6 w-6 items-center justify-center rounded-lg text-sm text-text backdrop-blur-md disabled:opacity-30"
          title="Zoom in"
        >
          +
        </button>
        <button
          onPointerDown={(e) => e.stopPropagation()}
          onClick={zoomOut}
          disabled={secPerView >= MAX_SEC}
          className="btn-glass flex h-6 w-6 items-center justify-center rounded-lg text-sm text-text backdrop-blur-md disabled:opacity-30"
          title="Zoom out"
        >
          −
        </button>
      </div>
    </div>
  )
}
