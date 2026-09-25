import { useCallback, useEffect, useRef } from 'react'
import type { CuePoint } from '../api'
import { drawCuePoint, drawCues, drawLoop, drawPlayhead } from '../lib/cues'
import { paintWave, type WaveColumn } from '../lib/waveform'

interface Props {
  cols: WaveColumn[]
  currentTime: number
  duration: number
  cues: CuePoint[]
  cuePoint: number | null
  loop: { start: number; end: number } | null
  /** Seconds the MAIN waveform shows — drawn here as a window around the
   *  playhead, so the overview says which part of the track is on screen. */
  secPerView: number
  onSeek: (t: number) => void
}

/**
 * Whole-track overview waveform: static coloured waveform (cached to an
 * offscreen canvas) with a moving playhead + dimmed played region. Click to
 * seek anywhere in the track.
 */
export function OverviewWaveform({
  cols,
  currentTime,
  duration,
  cues,
  cuePoint,
  loop,
  secPerView,
  onSeek,
}: Props) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const cacheRef = useRef<HTMLCanvasElement | null>(null)
  const timeRef = useRef(0)
  const durRef = useRef(0)
  const cuesRef = useRef<CuePoint[]>(cues)
  const viewRef = useRef(secPerView)
  const cuePointRef = useRef<number | null>(cuePoint)
  const loopRef = useRef(loop)

  const renderCache = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas || canvas.width === 0) return
    let cache = cacheRef.current
    if (!cache) {
      cache = document.createElement('canvas')
      cacheRef.current = cache
    }
    cache.width = canvas.width
    cache.height = canvas.height
    const ctx = cache.getContext('2d')!
    ctx.clearRect(0, 0, cache.width, cache.height)
    paintWave(ctx, cols, cache.width, cache.height, 0, 1)
  }, [cols])

  const draw = useCallback(() => {
    const canvas = canvasRef.current
    const cache = cacheRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')!
    const w = canvas.width
    const h = canvas.height
    ctx.clearRect(0, 0, w, h)
    if (cache) ctx.drawImage(cache, 0, 0)
    const dur = durRef.current
    if (dur > 0) {
      const px = Math.round((timeRef.current / dur) * w)
      ctx.fillStyle = 'rgba(5,6,10,0.5)'
      ctx.fillRect(0, 0, px, h)
      const dpr = window.devicePixelRatio || 1
      const lp = loopRef.current
      if (lp) drawLoop(ctx, (lp.start / dur) * w, (lp.end / dur) * w, h, dpr)
      // Cue markers (no beatgrid/labels in the overview).
      drawCues(ctx, cuesRef.current, w, h, dpr, (t) => (t / dur) * w, false)
      const cp = cuePointRef.current
      if (cp != null) drawCuePoint(ctx, Math.round((cp / dur) * w), h, dpr)
      drawViewWindow(ctx, timeRef.current, viewRef.current, dur, w, h, dpr)
      drawPlayhead(ctx, px, h, dpr, false)
    }
  }, [])

  const resize = useCallback(() => {
    const canvas = canvasRef.current
    const wrap = wrapRef.current
    if (!canvas || !wrap) return
    const dpr = window.devicePixelRatio || 1
    canvas.width = Math.max(1, Math.floor(wrap.clientWidth * dpr))
    canvas.height = Math.max(1, Math.floor(wrap.clientHeight * dpr))
    renderCache()
    draw()
  }, [renderCache, draw])

  // Rebuild the cache when the columns change (new track) or on resize.
  useEffect(() => {
    resize()
  }, [resize])

  useEffect(() => {
    const wrap = wrapRef.current
    if (!wrap) return
    const ro = new ResizeObserver(() => resize())
    ro.observe(wrap)
    return () => ro.disconnect()
  }, [resize])

  // Move the playhead as playback advances.
  useEffect(() => {
    timeRef.current = currentTime
    durRef.current = duration
    draw()
  }, [currentTime, duration, draw])

  // Redraw when the cue set, cue point, loop or main-view zoom changes.
  useEffect(() => {
    cuesRef.current = cues
    cuePointRef.current = cuePoint
    loopRef.current = loop
    viewRef.current = secPerView
    draw()
  }, [cues, cuePoint, loop, secPerView, draw])

  const seek = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!duration) return
    const rect = e.currentTarget.getBoundingClientRect()
    const ratio = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width))
    onSeek(ratio * duration)
  }

  return (
    <div ref={wrapRef} onClick={seek} className="h-full w-full cursor-pointer">
      <canvas ref={canvasRef} className="block h-full w-full" />
    </div>
  )
}

/**
 * The main waveform's visible range: a frosted, outlined window centred on the
 * playhead, `secPerView` wide. Clipped to the track at either end, as the main
 * view is (it shows blank past the edges), and never narrower than a few
 * pixels, so it stays findable when zoomed right in on a long track.
 */
function drawViewWindow(
  ctx: CanvasRenderingContext2D,
  t: number,
  secPerView: number,
  dur: number,
  w: number,
  h: number,
  dpr: number,
): void {
  if (!(secPerView > 0)) return
  const minW = 6 * dpr
  let x0 = ((t - secPerView / 2) / dur) * w
  let x1 = ((t + secPerView / 2) / dur) * w
  if (x1 - x0 < minW) {
    const c = (x0 + x1) / 2
    x0 = c - minW / 2
    x1 = c + minW / 2
  }
  x0 = Math.max(0, x0)
  x1 = Math.min(w, x1)
  if (x1 <= x0) return
  const inset = dpr
  const r = Math.min(4 * dpr, (x1 - x0) / 2)
  ctx.save()
  ctx.beginPath()
  ctx.roundRect(x0 + inset / 2, inset, x1 - x0 - inset, h - inset * 2, r)
  ctx.fillStyle = 'rgba(255,255,255,0.10)'
  ctx.fill()
  ctx.shadowColor = 'rgba(255,255,255,0.35)'
  ctx.shadowBlur = 8 * dpr
  ctx.lineWidth = Math.max(1, dpr)
  ctx.strokeStyle = 'rgba(255,255,255,0.75)'
  ctx.stroke()
  ctx.restore()
}
