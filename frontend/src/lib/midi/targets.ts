// What the router acts on, filled in by whoever is mounted to take it:
// PrepStrip (the decks, performance mode), App (loading), TrackTable (browse)
// and Sidebar (the playlist tree). Assigned on every render — as PrepStrip's
// `keys` ref is — so the router always sees the latest handlers.
//
// Unmounting clears only what THIS component last set: switching views
// renders the new table (which registers) before the old one's cleanup runs,
// and a blanket reset would wipe the newcomer's handlers.

import { useEffect, useRef } from 'react'
import { EMPTY_TARGETS, type MidiTargets } from './router'

export const midiTargets: MidiTargets = { ...EMPTY_TARGETS, decks: { A: null, B: null } }

export function useMidiTargets(patch: Partial<MidiTargets>): void {
  Object.assign(midiTargets, patch)
  const mine = useRef(patch)
  mine.current = patch
  useEffect(
    () => () => {
      const t = midiTargets as unknown as Record<string, unknown>
      for (const [k, v] of Object.entries(mine.current)) {
        if (t[k] !== v) continue
        t[k] = k === 'decks' ? { A: null, B: null } : EMPTY_TARGETS[k as keyof MidiTargets]
      }
    },
    [],
  )
}
