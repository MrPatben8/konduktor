import { useEffect, useRef } from 'react'
import { bendFor, PITCH_RANGES } from '../../lib/sync'
import { Fader } from '../mixer/Controls'
import type { Deck } from './useDeck'

interface Props {
  deck: Deck
  /** This deck is the tempo master (the one SYNC follows). */
  master: boolean
  synced: boolean
  /** Why SYNC cannot engage (no grid on a deck), else null. */
  syncBlocked: string | null
  onSync: () => void
  onMaster: () => void
  /** Moving the pitch fader — or bending — by hand takes the deck out of SYNC. */
  onManualPitch: () => void
  /** This deck's beats against the other deck's, in beats (-0.5..0.5); null
   *  when either has no grid or is not playing. Read every frame. */
  phase: () => number | null
}

const lit = 'is-selected text-text'
const unlit = 'btn-glass text-muted'

/**
 * A deck's tempo row in performance mode (decided 2026-11-02): keylock, SYNC,
 * MASTER, the phase meter, the pitch fader with its range, and the tempo as
 * heard. The pitch fader is the deck's SPEED — the grid's BPM is still edited
 * in the header and Grid mode.
 */
export function DeckTempo({ deck, master, synced, syncBlocked, onSync, onMaster, onManualPitch, phase }: Props) {
  const { pitch, setPitch, pitchRange, changeRange, rate, setBend, keylock, setKeylock, keylockAvailable, activeMarker, track } = deck
  const bpm = activeMarker ? activeMarker.bpm * rate : null
  const pct = pitch * pitchRange * 100
  const nextRange = PITCH_RANGES[(PITCH_RANGES.indexOf(pitchRange) + 1) % PITCH_RANGES.length]
  return (
    <div className="flex h-9 shrink-0 items-center gap-2">
      <button
        data-midi-action={`deck.${deck.channel}.keylock`}
        onClick={() => setKeylock(!keylock)}
        disabled={!keylockAvailable}
        aria-pressed={keylock}
        title={
          keylockAvailable
            ? keylock
              ? 'Keylock on: a tempo change keeps the pitch'
              : 'Keylock off: the pitch moves with the tempo, like a turntable'
            : 'Keylock is unavailable (the time-stretcher did not load)'
        }
        className={`h-8 shrink-0 rounded-full px-3 text-[11px] font-semibold tracking-[0.08em] disabled:opacity-40 ${keylock ? lit : unlit}`}
      >
        KEY
      </button>
      <button
        data-midi-action={`deck.${deck.channel}.sync`}
        onClick={onSync}
        disabled={!track || (!synced && syncBlocked != null)}
        aria-pressed={synced}
        title={syncBlocked ?? (synced ? 'Synced: following the master’s tempo and beats — click to release' : 'Match the master’s tempo and line the beats up')}
        className={`h-8 shrink-0 rounded-full px-3 text-[11px] font-semibold tracking-[0.08em] disabled:opacity-40 ${synced ? lit : unlit}`}
      >
        SYNC
      </button>
      <button
        data-midi-action={`deck.${deck.channel}.master`}
        onClick={onMaster}
        disabled={!track}
        aria-pressed={master}
        title={master ? 'Tempo master: a synced deck follows this one' : 'Make this deck the tempo master'}
        className={`h-8 shrink-0 rounded-full px-3 text-[11px] font-semibold tracking-[0.08em] disabled:opacity-40 ${
          master ? 'bg-gold/20 text-gold shadow-[inset_0_0_0_1px_rgb(255_200_97/0.6)]' : unlit
        }`}
      >
        MASTER
      </button>
      <PhaseMeter
        phase={phase}
        disabled={!track}
        onBend={(b) => {
          if (b !== 1) onManualPitch()
          setBend(b)
        }}
      />
      <Fader
        midi={`deck.${deck.channel}.pitch`}
        orientation="horizontal"
        value={pitch}
        min={-1}
        max={1}
        defaultValue={0}
        onChange={(v) => {
          setPitch(Math.abs(v) < 0.01 ? 0 : v)
          onManualPitch()
        }}
        label="Pitch"
        describe={() => `Pitch ${fmtPct(pct)} (range ±${Math.round(pitchRange * 100)}%) — double-click to reset`}
        className="min-w-16 flex-1"
      />
      <button
        onClick={() => changeRange(nextRange)}
        title={`Pitch range ±${Math.round(pitchRange * 100)}% — click for ±${Math.round(nextRange * 100)}%`}
        className="btn-glass h-8 w-11 shrink-0 rounded-full font-mono text-[11px] text-muted"
      >
        ±{Math.round(pitchRange * 100)}
      </button>
      <div className="well flex h-8 w-[6.5rem] shrink-0 flex-col items-end justify-center rounded-lg px-2 leading-none">
        <span className="font-mono text-[13px] font-medium text-text">{bpm ? bpm.toFixed(2) : '—'}</span>
        <span className={`font-mono text-[10px] ${pct === 0 ? 'text-faint' : 'text-muted'}`}>{fmtPct(pct)}</span>
      </div>
    </div>
  )
}

const fmtPct = (p: number) => `${p > 0 ? '+' : p < 0 ? '−' : '±'}${Math.abs(p).toFixed(2)}%`

/** Where this deck's beats fall against the other deck's: centre = in phase,
 *  right = ahead. Drawn every frame straight to the DOM. DRAG it to bend the
 *  deck's speed — right faster, left slower — and let go to spring back, the
 *  way a DJ nudges a platter to bring the beats together. */
function PhaseMeter({
  phase,
  onBend,
  disabled,
}: {
  phase: () => number | null
  onBend: (bend: number) => void
  disabled: boolean
}) {
  const markRef = useRef<HTMLSpanElement>(null)
  const dragX = useRef<number | null>(null)
  const release = () => {
    if (dragX.current == null) return
    dragX.current = null
    onBend(1)
  }
  const read = useRef(phase)
  read.current = phase
  useEffect(() => {
    let raf = 0
    const tick = () => {
      const el = markRef.current
      const p = read.current()
      if (el) {
        el.style.opacity = p == null ? '0' : '1'
        if (p != null) {
          el.style.left = `${(p + 0.5) * 100}%`
          el.style.background = Math.abs(p) < 0.03 ? 'var(--color-mint)' : 'var(--color-gold)'
        }
      }
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [])
  return (
    <div
      onPointerDown={(e) => {
        if (disabled || e.button !== 0) return
        e.preventDefault()
        e.currentTarget.setPointerCapture(e.pointerId)
        dragX.current = e.clientX
      }}
      onPointerMove={(e) => dragX.current != null && onBend(bendFor(e.clientX - dragX.current, e.shiftKey))}
      onPointerUp={release}
      onPointerCancel={release}
      onLostPointerCapture={release}
      className={`well relative h-3 w-20 shrink-0 touch-none overflow-hidden rounded-full ${disabled ? '' : 'cursor-ew-resize'}`}
      title="Phase: where this deck's beats fall against the other deck's (centre = together). Drag to bend the speed — right faster, left slower (Shift: finer)"
    >
      <span aria-hidden className="absolute inset-y-0.5 left-1/2 w-px bg-white/25" />
      <span ref={markRef} className="absolute inset-y-0 w-1.5 -translate-x-1/2 rounded-full opacity-0" />
    </div>
  )
}
