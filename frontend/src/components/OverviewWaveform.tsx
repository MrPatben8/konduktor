import { useCallback, useEffect, useRef } from 'react'
import type { CuePoint } from '../api'
import { drawCues, drawLoop } from '../lib/cues'
import { paintWave, type WaveColumn } from '../lib/waveform'

interface Props {
  cols: WaveColumn[]
  currentTime: number
  duration: number
  cues: CuePoint[]
  loop: { start: number; end: number } | null
  onSeek: (t: number) => void
}

/**
 * Whole-track overview waveform: static coloured waveform (cached to an
 * offscreen canvas) with a moving playhead + dimmed played region. Click to
 * seek anywhere in the track.
 */
export function OverviewWaveform({ cols, currentTime, duration, cues, loop, onSeek }: Props) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const cacheRef = useRef<HTMLCanvasElement | null>(null)
  const timeRef = useRef(0)
  const durRef = useRef(0)
  const cuesRef = useRef<CuePoint[]>(cues)
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
      ctx.fillStyle = 'rgba(10,11,15,0.55)'
      ctx.fillRect(0, 0, px, h)
      const dpr = window.devicePixelRatio || 1
      const lp = loopRef.current
      if (lp) drawLoop(ctx, (lp.start / dur) * w, (lp.end / dur) * w, h, dpr)
      // Cue markers (no beatgrid/labels in the overview).
      drawCues(ctx, cuesRef.current, w, h, dpr, (t) => (t / dur) * w, false)
      const pw = Math.max(2, Math.round(2 * dpr))
      ctx.fillStyle = '#e6e9ef'
      ctx.fillRect(px - Math.floor(pw / 2), 0, pw, h)
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

  // Redraw when the cue set or loop changes.
  useEffect(() => {
    cuesRef.current = cues
    loopRef.current = loop
    draw()
  }, [cues, loop, draw])

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
