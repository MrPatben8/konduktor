import { useEffect } from 'react'
import { createPortal } from 'react-dom'
import { actionDef } from '../lib/midi/actions'
import { armLearn, getMidiState, setOnScreenLearn, useMidiState } from '../lib/midi/store'

/**
 * On-screen MIDI learning (Settings ▸ MIDI Controllers… ▸ Learn on screen):
 * every control that has a MIDI action (`data-midi-action`) is ringed;
 * clicking one ARMS it instead of using it, and the next control moved on a
 * controller is bound to it — into the Shift layer if the controller's Shift
 * is held. Mapped controls show solid, the armed one glows, and Done or Esc
 * leaves. Mounted once (main.tsx); renders nothing until switched on.
 */
export function MidiLearnOverlay() {
  const onScreen = useMidiState((s) => s.onScreen)
  const armed = useMidiState((s) => s.armed)
  const mappings = useMidiState((s) => s.mappings)
  const ports = useMidiState((s) => s.ports)
  const learned = useMidiState((s) => s.learned)

  // Clicks on a learnable control arm it; nothing else changes. Pointer and
  // mouse events are stopped in the capture phase so the control never acts
  // (knobs and faders start their drags on pointerdown).
  useEffect(() => {
    if (!onScreen) return
    const target = (e: Event) => (e.target as Element | null)?.closest?.('[data-midi-action]') as HTMLElement | null
    const stop = (e: Event) => {
      const el = target(e)
      if (!el) return
      e.preventDefault()
      e.stopPropagation()
      if (e.type !== 'pointerdown') return
      const action = el.dataset.midiAction!
      const a = getMidiState().armed
      armLearn(a && 'layer' in a && a.action === action ? null : { action, layer: 'normal' })
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      e.preventDefault()
      e.stopPropagation()
      if (getMidiState().armed) armLearn(null)
      else setOnScreenLearn(false)
    }
    const kinds = ['pointerdown', 'pointerup', 'mousedown', 'mouseup', 'click', 'dblclick', 'contextmenu']
    for (const k of kinds) window.addEventListener(k, stop, true)
    window.addEventListener('keydown', onKey, true)
    document.body.classList.add('midi-learn')
    return () => {
      for (const k of kinds) window.removeEventListener(k, stop, true)
      window.removeEventListener('keydown', onKey, true)
      document.body.classList.remove('midi-learn')
    }
  }, [onScreen])

  // Mark the controls: mapped (on a connected controller), armed, just learned.
  useEffect(() => {
    if (!onScreen) return
    const mapped = new Set<string>()
    for (const p of ports) {
      for (const b of Object.values(mappings[p]?.bindings ?? {})) {
        if (b.normal) mapped.add(b.normal)
        if (b.shift) mapped.add(b.shift)
      }
    }
    const armedId = armed && 'layer' in armed ? armed.action : null
    const flash = learned && performance.now() - learned.at < 1500 ? learned.action : null
    for (const el of document.querySelectorAll<HTMLElement>('[data-midi-action]')) {
      const id = el.dataset.midiAction!
      el.toggleAttribute('data-midi-mapped', mapped.has(id))
      el.toggleAttribute('data-midi-armed', id === armedId)
      el.toggleAttribute('data-midi-learned', id === flash)
    }
    // The "just learned" flash fades on its own.
    const fade = flash ? setTimeout(() => document.querySelector('[data-midi-learned]')?.removeAttribute('data-midi-learned'), 1500) : 0
    return () => {
      clearTimeout(fade)
      for (const el of document.querySelectorAll('[data-midi-action]')) {
        el.removeAttribute('data-midi-mapped')
        el.removeAttribute('data-midi-armed')
        el.removeAttribute('data-midi-learned')
      }
    }
  }, [onScreen, armed, mappings, ports, learned])

  if (!onScreen) return null
  const armedLabel = armed && 'layer' in armed ? actionDef(armed.action)?.label : null
  return createPortal(
    <div className="pointer-events-none fixed inset-x-0 top-3 z-[80] flex justify-center">
      <div className="pointer-events-auto flex items-center gap-3 glass-overlay rounded-full px-4 py-2 text-sm text-text">
        <span className="h-2 w-2 animate-pulse rounded-full bg-accent" />
        {ports.length === 0 ? (
          <span>MIDI learn — plug in a controller</span>
        ) : armedLabel ? (
          <span>
            <span className="font-semibold">{armedLabel}</span>: move a control on your controller — an encoder a few clicks in
            this action's direction (hold its Shift for the Shift layer)
          </span>
        ) : (
          <span>MIDI learn — click a control, then move it on your controller</span>
        )}
        <button onClick={() => setOnScreenLearn(false)} className="rounded-full btn-primary px-3 py-1 text-xs font-semibold">
          Done
        </button>
      </div>
    </div>,
    document.body,
  )
}
