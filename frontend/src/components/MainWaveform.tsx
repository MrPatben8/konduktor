import { useEffect, useRef, useState } from 'react'
import { paintWave, type WaveColumn } from '../lib/waveform'

interface Props {
  cols: WaveColumn[]
  currentTime: number
  duration: number
  onSeek: (t: number) => void
}

const MIN_SEC = 2 // most zoomed-in (seconds across the view)
const MAX_SEC = 64 // most zoomed-out
const DEFAULT_SEC = 16

/**
 * Traktor-style scrolling main waveform: the playhead is fixed at the centre and
 * the waveform scrolls beneath it. Shows a time window [t - sec/2, t + sec/2];
 * the zoom buttons change that window width. Repaints every frame (driven by the
 * parent's rAF-updated currentTime).
 */
export function MainWaveform({ cols, currentTime, duration, onSeek }: Props) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [secPerView, setSecPerView] = useState(DEFAULT_SEC)

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
      const startFrac = (currentTime - half) / duration
      const endFrac = (currentTime + half) / duration
      paintWave(ctx, cols, w, h, startFrac, endFrac)
    }

    // Fixed centre playhead.
    const cx = Math.floor(w / 2)
    ctx.fillStyle = '#e6e9ef'
    ctx.fillRect(cx, 0, Math.max(1, Math.round(dpr)), h)
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

  // Click to seek: map horizontal offset from centre to a time delta.
  const seek = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!duration) return
    const rect = e.currentTarget.getBoundingClientRect()
    const ratioFromCenter = (e.clientX - rect.left) / rect.width - 0.5
    const t = currentTime + ratioFromCenter * secPerView
    onSeek(Math.min(duration, Math.max(0, t)))
  }

  const zoomIn = () => setSecPerView((s) => Math.max(MIN_SEC, s / 2))
  const zoomOut = () => setSecPerView((s) => Math.min(MAX_SEC, s * 2))

  return (
    <div ref={wrapRef} onClick={seek} className="relative h-full w-full cursor-pointer">
      <canvas ref={canvasRef} className="block h-full w-full" />

      {/* Zoom controls (Traktor-style +/-) */}
      <div className="absolute right-2 top-2 flex flex-col gap-1">
        <button
          onClick={(e) => {
            e.stopPropagation()
            zoomIn()
          }}
          disabled={secPerView <= MIN_SEC}
          className="flex h-6 w-6 items-center justify-center rounded border border-line bg-ink-950/70 text-sm text-text hover:border-accent disabled:opacity-30"
          title="Zoom in"
        >
          +
        </button>
        <button
          onClick={(e) => {
            e.stopPropagation()
            zoomOut()
          }}
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
