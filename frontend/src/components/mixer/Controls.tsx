// The mixer's controls: a rotary knob and a fader. Mouse-driven — drag (Shift
// for fine), scroll, double-click to reset — and NOT focusable, so the deck's
// keys (←/→, Space) never land on them by accident.

import { useRef, type PointerEvent, type WheelEvent } from 'react'

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v))

/** Pixels of drag for the whole travel (Shift: five times finer). */
const DRAG_PX = 150

interface KnobProps {
  value: number
  min: number
  max: number
  /** Reset value (double-click); also where the arc starts on a bipolar knob. */
  defaultValue: number
  /** The arc grows from the centre (EQ, colour) rather than from the left. */
  bipolar?: boolean
  onChange: (v: number) => void
  label: string
  /** The tooltip, for the value it would show. */
  describe: (v: number) => string
  color?: string
  size?: number
  /** Its MIDI action, for on-screen learning (lib/midi). */
  midi?: string
}

/** A point on the knob's circle at angle `deg` (0 = straight up). */
function polar(r: number, deg: number, c: number): [number, number] {
  const rad = ((deg - 90) * Math.PI) / 180
  return [c + r * Math.cos(rad), c + r * Math.sin(rad)]
}

function arc(r: number, from: number, to: number, c: number): string {
  if (Math.abs(to - from) < 0.5) return ''
  const [a, b] = from < to ? [from, to] : [to, from]
  const [x1, y1] = polar(r, a, c)
  const [x2, y2] = polar(r, b, c)
  return `M ${x1} ${y1} A ${r} ${r} 0 ${b - a > 180 ? 1 : 0} 1 ${x2} ${y2}`
}

export function Knob({
  midi,
  value,
  min,
  max,
  defaultValue,
  bipolar = false,
  onChange,
  label,
  describe,
  color = 'var(--color-accent)',
  size = 26,
}: KnobProps) {
  const drag = useRef<{ y: number; v: number } | null>(null)
  const range = max - min
  const angle = -135 + ((clamp(value, min, max) - min) / range) * 270
  const start = bipolar ? -135 + ((defaultValue - min) / range) * 270 : -135
  const c = size / 2
  const stroke = size >= 36 ? 3.5 : 2.5
  const r = c - stroke
  const [px, py] = polar(r - stroke - 2, angle, c)

  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return
    e.preventDefault()
    e.currentTarget.setPointerCapture(e.pointerId)
    drag.current = { y: e.clientY, v: value }
  }
  const onPointerMove = (e: PointerEvent<HTMLDivElement>) => {
    const d = drag.current
    if (!d) return
    const px = e.shiftKey ? DRAG_PX * 5 : DRAG_PX
    onChange(clamp(d.v + ((d.y - e.clientY) / px) * range, min, max))
  }
  const onWheel = (e: WheelEvent<HTMLDivElement>) => {
    onChange(clamp(value - (e.deltaY / (e.shiftKey ? 2000 : 400)) * range, min, max))
  }
  return (
    <div className="flex flex-col items-center gap-0.5" title={describe(value)} data-midi-action={midi}>
      <div
        role="slider"
        aria-label={label}
        aria-valuemin={min}
        aria-valuemax={max}
        aria-valuenow={value}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={() => (drag.current = null)}
        onPointerCancel={() => (drag.current = null)}
        onDoubleClick={() => onChange(defaultValue)}
        onWheel={onWheel}
        className="cursor-ns-resize touch-none select-none"
        style={{ width: size, height: size }}
      >
        <svg width={size} height={size}>
          <circle cx={c} cy={c} r={r} fill="rgb(0 0 0 / 0.36)" stroke="rgb(255 255 255 / 0.08)" />
          <path d={arc(r, -135, 135, c)} fill="none" stroke="rgb(255 255 255 / 0.1)" strokeWidth={stroke} strokeLinecap="round" />
          <path
            d={arc(r, start, angle, c)}
            fill="none"
            stroke={color}
            strokeWidth={stroke}
            strokeLinecap="round"
            style={{ filter: `drop-shadow(0 0 3px ${color})` }}
          />
          <line x1={c} y1={c} x2={px} y2={py} stroke="var(--color-text)" strokeWidth={size >= 36 ? 2.5 : 2} strokeLinecap="round" />
        </svg>
      </div>
      <span
        className={`font-semibold uppercase leading-none tracking-[0.08em] text-faint ${size >= 36 ? 'text-[9.5px]' : 'text-[8.5px]'}`}
      >
        {label}
      </span>
    </div>
  )
}

interface FaderProps {
  value: number
  min: number
  max: number
  defaultValue: number
  onChange: (v: number) => void
  orientation: 'vertical' | 'horizontal'
  label: string
  describe: (v: number) => string
  className?: string
  /** Its MIDI action, for on-screen learning (lib/midi). */
  midi?: string
}

/** A fader: grab anywhere on it and the cap follows the pointer. */
export function Fader({ value, min, max, defaultValue, onChange, orientation, label, describe, className = '', midi }: FaderProps) {
  const vertical = orientation === 'vertical'
  const t = (clamp(value, min, max) - min) / (max - min)
  const set = (e: PointerEvent<HTMLDivElement>) => {
    const box = e.currentTarget.getBoundingClientRect()
    const f = vertical ? 1 - (e.clientY - box.top) / box.height : (e.clientX - box.left) / box.width
    onChange(min + clamp(f, 0, 1) * (max - min))
  }
  return (
    <div
      role="slider"
      data-midi-action={midi}
      aria-label={label}
      aria-orientation={orientation}
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuenow={value}
      title={describe(value)}
      onPointerDown={(e) => {
        if (e.button !== 0) return
        e.preventDefault()
        e.currentTarget.setPointerCapture(e.pointerId)
        set(e)
      }}
      onPointerMove={(e) => e.currentTarget.hasPointerCapture(e.pointerId) && set(e)}
      onDoubleClick={() => onChange(defaultValue)}
      className={`relative touch-none select-none ${vertical ? 'w-6 cursor-ns-resize' : 'h-6 cursor-ew-resize'} ${className}`}
    >
      <div
        className={`well absolute rounded-full ${vertical ? 'inset-y-1.5 left-1/2 w-1.5 -translate-x-1/2' : 'inset-x-1.5 top-1/2 h-1.5 -translate-y-1/2'}`}
      />
      <div
        className={`btn-glass pointer-events-none absolute rounded-md ${
          vertical ? 'left-0 h-3 w-6 -translate-y-1/2' : 'top-0 h-6 w-3 -translate-x-1/2'
        }`}
        style={vertical ? { top: `calc(0.375rem + ${(1 - t) * 100}% - ${(1 - t) * 0.75}rem)` } : { left: `calc(0.375rem + ${t * 100}% - ${t * 0.75}rem)` }}
      >
        <span className={`absolute bg-text/80 ${vertical ? 'inset-x-1 top-1/2 h-px' : 'inset-y-1 left-1/2 w-px'}`} />
      </div>
    </div>
  )
}
