// MIDI state the UI shows and the router reads, in one small external store
// read through useSyncExternalStore (as lib/audio/mixerState.ts is): the
// connected controllers, every controller's mapping, what is being learned and
// the last message seen (the settings dialog's activity line).
//
// Mappings are persisted by whoever installs `setPersist` (useMidi, through
// the shared prefs writer); everything else lives for the session.

import { useSyncExternalStore } from 'react'
import type { Layer, Mapping, Mappings } from './mapping'
import { emptyMapping } from './mapping'
import type { SourceKey } from './messages'

/** What Learn is waiting for: an action in a layer, or the Shift control itself. */
export type LearnTarget = { action: string; layer: Layer } | { action: 'shift' }

export interface MidiState {
  /** Whether this build can read MIDI at all (desktop app, or a Web MIDI browser). */
  available: boolean
  /** Why MIDI could not be read (a browser refused access), else null. */
  blocked: string | null
  /** Connected controllers, by port name. */
  ports: string[]
  mappings: Mappings
  /** Armed: the next control moved is bound to this. */
  armed: LearnTarget | null
  /** On-screen learn mode: clicking a control arms it. */
  onScreen: boolean
  /** The last binding learned (the on-screen mode flashes it). */
  learned: { port: string; action: string; layer: Layer; at: number } | null
  activity: { port: string; source: SourceKey; value: number; at: number } | null
}

let state: MidiState = {
  available: false,
  blocked: null,
  ports: [],
  mappings: {},
  armed: null,
  onScreen: false,
  learned: null,
  activity: null,
}
const listeners = new Set<() => void>()
let persist: ((m: Mappings) => void) | null = null

export function getMidiState(): MidiState {
  return state
}

export function subscribeMidi(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export function patchMidi(patch: Partial<MidiState>): void {
  state = { ...state, ...patch }
  for (const fn of listeners) fn()
}

export function setPersist(fn: ((m: Mappings) => void) | null): void {
  persist = fn
}

/** Change one controller's mapping (created empty on first use) and save. */
export function updateMapping(port: string, fn: (m: Mapping) => Mapping): void {
  const mappings = { ...state.mappings, [port]: fn(state.mappings[port] ?? emptyMapping()) }
  patchMidi({ mappings })
  persist?.(mappings)
}

export function replaceMappings(mappings: Mappings, save = true): void {
  patchMidi({ mappings })
  if (save) persist?.(mappings)
}

export function armLearn(target: LearnTarget | null): void {
  patchMidi({ armed: target })
}

export function setOnScreenLearn(on: boolean): void {
  patchMidi({ onScreen: on, armed: null })
}

/** A slice of the MIDI state, re-rendering only when it changes. */
export function useMidiState<T>(select: (s: MidiState) => T): T {
  return useSyncExternalStore(subscribeMidi, () => select(state))
}
