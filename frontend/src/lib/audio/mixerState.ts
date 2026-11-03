// The mixer's controls, in one small external store read through
// useSyncExternalStore (as lib/drag.tsx is): the on-screen mixer and the audio
// graph both follow it, and a MIDI mapping later will write the same setters
// (decided 2026-11-02: mouse now, MIDI later). Not persisted: a session's
// mixer starts flat.

import { useSyncExternalStore } from 'react'
import type { CrossfaderCurve } from './mixerMath'

export type Channel = 'A' | 'B'
export const CHANNELS: Channel[] = ['A', 'B']

export const FX_TYPES = ['filter', 'echo', 'space', 'noise', 'crush'] as const
export type FxType = (typeof FX_TYPES)[number]
export const FX_LABELS: Record<FxType, string> = {
  filter: 'Filter',
  echo: 'Echo',
  space: 'Space',
  noise: 'Noise',
  crush: 'Crush',
}
/** What each effect's parameter knob does. */
export const FX_PARAM: Record<FxType, string> = {
  filter: 'Resonance',
  echo: 'Echo length (beats)',
  space: 'Reverb size',
  noise: 'Noise resonance',
  crush: 'Drive',
}

export interface ChannelState {
  /** dB either side of the track's auto gain. */
  trim: number
  /** EQ bands, -1..1 (centre flat). */
  low: number
  mid: number
  high: number
  fx: FxType
  /** The colour knob, -1..1 (centre = effect off). */
  color: number
  /** The effect's parameter, 0..1. */
  param: number
  /** The channel fader, 0..1. */
  fader: number
}

export interface MixerState {
  A: ChannelState
  B: ChannelState
  /** -1 = all A, 1 = all B. */
  crossfader: number
  curve: CrossfaderCurve
  /** EQ cuts to silence at full left (else to -26 dB). */
  eqKill: boolean
}

export const DEFAULT_CHANNEL: ChannelState = {
  trim: 0,
  low: 0,
  mid: 0,
  high: 0,
  fx: 'filter',
  color: 0,
  param: 0.5,
  fader: 1,
}

let state: MixerState = {
  A: DEFAULT_CHANNEL,
  B: DEFAULT_CHANNEL,
  crossfader: 0,
  curve: 'smooth',
  eqKill: false,
}
const listeners = new Set<() => void>()

export function getMixerState(): MixerState {
  return state
}

export function subscribeMixer(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

function commit(next: MixerState): void {
  state = next
  for (const fn of listeners) fn()
}

export function setChannel(ch: Channel, patch: Partial<ChannelState>): void {
  commit({ ...state, [ch]: { ...state[ch], ...patch } })
}

export function setMixer(patch: Partial<Pick<MixerState, 'crossfader' | 'curve' | 'eqKill'>>): void {
  commit({ ...state, ...patch })
}

/** A slice of the mixer, re-rendering only when it changes. */
export function useMixer<T>(select: (s: MixerState) => T): T {
  return useSyncExternalStore(subscribeMixer, () => select(state))
}
