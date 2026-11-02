// The app's ONE AudioContext. Every deck plays into it, so the decks can be
// mixed (and limited) before the speakers; two contexts cannot be connected.
// Never closed: a deck that goes away disconnects its own nodes.

import deckWorkletUrl from './deckProcessor.worklet.ts?worker&url'
import rubberbandUrl from './rubberband.wasm?url'

let ctx: AudioContext | null = null
let master: GainNode | null = null
let worklet: Promise<ArrayBuffer | null> | null = null

export function audioContext(): AudioContext {
  if (!ctx) {
    const AC: typeof AudioContext =
      window.AudioContext ?? (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
    ctx = new AC({ latencyHint: 'interactive' })
    master = ctx.createGain()
    master.connect(ctx.destination)
  }
  return ctx
}

/** Resume on a user gesture (autoplay policy). */
export function resumeAudio(): void {
  void audioContext().resume()
}

/** Where decks connect: the master bus. */
export function masterInput(): AudioNode {
  audioContext()
  return master!
}

/**
 * Register the deck processor (once per context) and fetch Rubber Band's
 * module bytes for keylock. Resolves to null bytes when the module cannot be
 * fetched — the decks then play without keylock rather than not at all.
 */
export function loadDeckWorklet(): Promise<ArrayBuffer | null> {
  if (!worklet) {
    const c = audioContext()
    const bytes = fetch(rubberbandUrl)
      .then((r) => (r.ok ? r.arrayBuffer() : null))
      .catch(() => null)
    worklet = c.audioWorklet.addModule(deckWorkletUrl).then(() => bytes)
    worklet.catch(() => {
      worklet = null
    })
  }
  return worklet
}
