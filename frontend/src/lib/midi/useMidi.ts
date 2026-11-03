// MIDI for the whole app, mounted once (App): finds controllers as they come
// and go, feeds their messages to the router, and keeps the mappings in
// userprefs (`midiMappings`, through the shared debounced prefs writer).
//
// midir has no connect notifications (nor does every Web MIDI), so the port
// list is polled — cheap, and a newly plugged controller works within ~1.5 s.

import { useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api'
import { usePrefsWriter } from '../prefs'
import { sanitizeMappings } from './mapping'
import { MidiRouter } from './router'
import { getMidiState, patchMidi, replaceMappings, setPersist } from './store'
import { midiTargets } from './targets'
import { midiTransport } from './transport'

const POLL_MS = 1500

/** The router every message goes through (one per app). */
export const midiRouter = new MidiRouter(() => midiTargets)

export function useMidi(onNotify: (kind: 'success' | 'warning', msg: string) => void): void {
  const writePrefs = usePrefsWriter()
  const prefs = useQuery({ queryKey: ['prefs'], queryFn: api.getPrefs }).data
  const hydrated = useRef(false)
  const notify = useRef(onNotify)
  notify.current = onNotify

  // Mappings: hydrated once from prefs, then saved on every change.
  useEffect(() => {
    if (hydrated.current || !prefs) return
    hydrated.current = true
    replaceMappings(sanitizeMappings((prefs as { midiMappings?: unknown }).midiMappings), false)
    setPersist((m) => writePrefs({ midiMappings: m }))
  }, [prefs, writePrefs])
  useEffect(() => () => setPersist(null), [])

  useEffect(() => {
    const transport = midiTransport()
    if (!transport) return
    patchMidi({ available: true })
    const open = new Map<string, () => void>()
    const opening = new Set<string>()
    let first = true
    let stopped = false

    const poll = async () => {
      let ports: string[]
      try {
        ports = await transport.ports()
        if (getMidiState().blocked) patchMidi({ blocked: null })
      } catch (e) {
        // A browser refusing MIDI access (Chrome asks first): say so in the
        // MIDI dialog rather than looking like no controller is plugged in.
        const why = e instanceof Error ? e.message : String(e)
        if (getMidiState().blocked !== why) patchMidi({ blocked: why })
        return
      }
      if (stopped) return
      for (const [port, close] of open) {
        if (ports.includes(port)) continue
        close()
        open.delete(port)
        midiRouter.forget(port)
      }
      for (const port of ports) {
        if (open.has(port) || opening.has(port)) continue
        opening.add(port)
        // Ports present at launch connect quietly; later ones are news.
        const announce = !first
        transport
          .open(port, (batch) => {
            for (const bytes of batch) midiRouter.receive(port, bytes)
          })
          .then((close) => {
            if (stopped) return close()
            open.set(port, close)
            if (!announce) return
            const mapped = !!getMidiState().mappings[port]
            notify.current(
              mapped ? 'success' : 'warning',
              mapped ? `${port} connected` : `${port} connected — not mapped yet (Settings ▸ MIDI Controllers…)`,
            )
          })
          .catch(() => {})
          .finally(() => opening.delete(port))
      }
      patchMidi({ ports })
      first = false
    }
    void poll()
    const t = setInterval(() => void poll(), POLL_MS)
    return () => {
      stopped = true
      clearInterval(t)
      for (const close of open.values()) close()
      patchMidi({ ports: [] })
    }
  }, [])
}
