import { xfadeGains } from '../../lib/audio/mixerMath'
import { useMixer } from '../../lib/audio/mixerState'

const W = 200
const H = 72
const PAD = 6
const STEPS = 120

/** The crossfader's two gain curves at `sharpness`: A falling left to right,
 *  B rising, over the whole travel, with where the crossfader is now. */
export function CrossfaderCurveGraph({ sharpness }: { sharpness: number }) {
  const position = useMixer((m) => m.crossfader)
  const x = (t: number) => PAD + t * (W - 2 * PAD)
  const y = (g: number) => H - PAD - g * (H - 2 * PAD)
  const line = (ch: 0 | 1) =>
    Array.from({ length: STEPS + 1 }, (_, i) => {
      const t = i / STEPS
      const g = xfadeGains(t * 2 - 1, sharpness)[ch]
      return `${i ? 'L' : 'M'}${x(t).toFixed(1)} ${y(g).toFixed(1)}`
    }).join(' ')
  const at = (position + 1) / 2
  return (
    <svg
      width={W}
      height={H}
      viewBox={`0 0 ${W} ${H}`}
      className="well shrink-0 rounded-lg"
      role="img"
      aria-label={`Crossfader curve, ${Math.round(sharpness * 100)}% sharp`}
    >
      {/* full level, the -3 dB line, the centre */}
      <line x1={PAD} x2={W - PAD} y1={y(1)} y2={y(1)} stroke="rgb(255 255 255 / 0.08)" />
      <line x1={PAD} x2={W - PAD} y1={y(Math.SQRT1_2)} y2={y(Math.SQRT1_2)} stroke="rgb(255 255 255 / 0.06)" strokeDasharray="2 3" />
      <line x1={x(0.5)} x2={x(0.5)} y1={PAD} y2={H - PAD} stroke="rgb(255 255 255 / 0.08)" />
      <path d={line(0)} fill="none" stroke="var(--color-accent)" strokeWidth={2} strokeLinejoin="round" />
      <path d={line(1)} fill="none" stroke="var(--color-gold)" strokeWidth={2} strokeLinejoin="round" />
      <line x1={x(at)} x2={x(at)} y1={PAD} y2={H - PAD} stroke="var(--color-text)" strokeOpacity={0.5} />
      <text x={PAD + 2} y={H - PAD - 3} fontSize={9} fill="var(--color-accent)" fontWeight={600}>A</text>
      <text x={W - PAD - 8} y={H - PAD - 3} fontSize={9} fill="var(--color-gold)" fontWeight={600}>B</text>
    </svg>
  )
}
