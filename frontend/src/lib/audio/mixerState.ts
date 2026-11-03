// The mixer's controls, in one small external store read through
// useSyncExternalStore (as lib/drag.tsx is): the on-screen mixer and the audio
// graph both follow it, and a MIDI mapping later will write the same setters
// (decided 2026-11-02: mouse now, MIDI later). Not persisted: a session's
// mixer starts flat.

import { useSyncExternalStore } from 'react'
import type { FilterRange } from './mixerMath'

export type Channel = 'A' | 'B'
export const CHANNELS: Channel[] = ['A', 'B']

/**
 * FILTER FX (decided 2026-11-03, after Traktor's mixer): each channel's FILTER
 * knob always filters — low-pass left, high-pass right — and the effect chosen
 * here rides with it, its amount growing with the knob. `filter` is the filter
 * alone.
 */
export const FX_TYPES = ['filter', 'reverb', 'delay', 'crusher', 'phaser'] as const
export type FxType = (typeof FX_TYPES)[number]
export const FX_LABELS: Record<FxType, string> = {
  filter: 'Filter',
  reverb: 'Reverb',
  delay: 'Delay',
  crusher: 'Bit crusher',
  phaser: 'Phaser',
}
/** For the channel's narrow picker. */
export const FX_SHORT: Record<FxType, string> = {
  filter: 'Filter',
  reverb: 'Reverb',
  delay: 'Delay',
  crusher: 'Crush',
  phaser: 'Phaser',
}

/**
 * The filter and each effect's one setting — app settings, not knobs (the
 * per-channel parameter knob was removed, decided 2026-11-03), the same for
 * both channels.
 */
export interface FxSettings {
  /** The filter's range for the Filter alone … */
  filterRange: FilterRange
  /** … and with an effect riding along. */
  effectRange: FilterRange
  /** Filter resonance, 0..1. */
  filterResonance: number
  /** The SHORT reverb's decay (s) — near the centre; the knob blends into
   *  one REVERB_GROWTH times as long as it opens. */
  reverbSize: number
  /** The delay's SHORTEST length, in beats of the deck's tempo — near the
   *  centre; it doubles as the knob opens (`delayBeatsAt`). */
  delayBeats: number
  /** Bit crusher drive, 1..4. */
  crusherDrive: number
  /** The phaser's SLOWEST sweep, in beats of the deck's tempo — near the
   *  centre; it halves as the knob opens (`phaserBeatsAt`). */
  phaserBeats: number
}

export const DEFAULT_FX: FxSettings = {
  filterRange: 'full',
  effectRange: 'musical',
  filterResonance: 0.3,
  reverbSize: 2,
  delayBeats: 0.25,
  crusherDrive: 2,
  phaserBeats: 4,
}

/** The filter's range under `type`: one setting for the Filter alone (Full by
 *  default), another with an effect riding along (Musical by default — swept
 *  to near silence, the filter would starve the effect it feeds). */
export function rangeFor(type: FxType, fx: FxSettings): FilterRange {
  return type === 'filter' ? fx.filterRange : fx.effectRange
}

export interface ChannelState {
  /** dB either side of the track's auto gain. */
  trim: number
  /** EQ bands, -1..1 (centre flat). */
  low: number
  mid: number
  high: number
  fx: FxType
  /** The FILTER knob, -1..1 (centre = filter and effect off). Named `color`
   *  from before the rename to Filter FX. */
  color: number
  /** The channel fader, 0..1. */
  fader: number
}

export interface MixerState {
  A: ChannelState
  B: ChannelState
  /** -1 = all A, 1 = all B. */
  crossfader: number
  /** The crossfader curve, 0 (smooth) .. 1 (sharp): `mixerMath.xfadeGains`. */
  sharpness: number
  /** EQ cuts to silence at full left (else to -26 dB). */
  eqKill: boolean
  fx: FxSettings
}

export const DEFAULT_CHANNEL: ChannelState = {
  trim: 0,
  low: 0,
  mid: 0,
  high: 0,
  fx: 'filter',
  color: 0,
  fader: 1,
}

let state: MixerState = {
  A: DEFAULT_CHANNEL,
  B: DEFAULT_CHANNEL,
  crossfader: 0,
  sharpness: 0.5,
  eqKill: false,
  fx: DEFAULT_FX,
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

export function setMixer(patch: Partial<Pick<MixerState, 'crossfader' | 'sharpness' | 'eqKill' | 'fx'>>): void {
  commit({ ...state, ...patch })
}

/** A slice of the mixer, re-rendering only when it changes. */
export function useMixer<T>(select: (s: MixerState) => T): T {
  return useSyncExternalStore(subscribeMixer, () => select(state))
}
