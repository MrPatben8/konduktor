// Every MIDI message from every controller comes through here: Shift is
// tracked, Learn gets first look, then the message is decoded through its
// binding and the action is run on whatever is mounted to take it (`targets`).
//
// Rules from the discussion (2026-11-03):
//   - Shift is a learned control; each control has a free Shift binding, and
//     an unbound one falls back to the control's normal action.
//   - Prep mode has one deck: deck B and the mixer act only in performance mode.
//   - The controller always wins: a position is applied as it arrives (no soft
//     takeover), and moving pitch or nudging with the jog releases SYNC (the
//     deck target does that, as the on-screen fader and phase meter do).
//   - A held control's release reaches the action its press ran, even if
//     Shift was let go in between (as useDeckKeys' `held`).

import { actionDef, actionDeck, performanceOnly, type ActionKind, type DeckId } from './actions'
import { Decoder, detectEncoding, type Decoded, type Encoding, type LearnKind } from './encoding'
import { bind, bindShift, resolveBinding, type Binding, type Layer, type Mapping } from './mapping'
import { parseMessage, sourceKey, splitSource, turnSource, type MidiMessage, type SourceKey } from './messages'
import { armLearn, getMidiState, patchMidi, updateMapping, type LearnTarget } from './store'
import type { Track } from '../../api'
import type { DeckShortcuts } from '../deckKeys'
import { FX_TYPES, getMixerState, setChannel, setMixer, type ChannelState } from '../audio/mixerState'
import { TRIM_RANGE_DB } from '../audio/mixerMath'

/** What a deck offers a controller: its keys, plus what keys cannot do. */
export interface DeckMidi extends DeckShortcuts {
  toggleSizeLoop(): void
  loopIn(): void
  loopOut(): void
  toggleLoop(): void
  toggleSync(): void
  toggleKeylock(): void
  /** The pitch fader, -1..1. */
  pitch: number
  /** A hand move of the pitch fader (releases SYNC). */
  setPitch(v: number): void
  jogTouch(on: boolean, sensitivity: number): void
  jogTurn(ticks: number, sensitivity: number): void
  scrub(ticks: number, sensitivity: number): void
}

export interface MidiTargets {
  performance: boolean
  /** The deck loads go to. */
  focused: DeckId
  decks: Record<DeckId, DeckMidi | null>
  togglePerformance: (() => void) | null
  /** Load the track list's highlighted row into `deck`. */
  load: ((deck: DeckId) => void) | null
  /** Move the track list's highlight by `ticks` rows. */
  browse: ((ticks: number) => void) | null
  /** The track list's highlighted row (what `load` loads). */
  lead: (() => Track | null) | null
  /** The sidebar's playlist tree. */
  tree: { step(ticks: number): void; press(): void } | null
}

export const EMPTY_TARGETS: MidiTargets = {
  performance: false,
  focused: 'A',
  decks: { A: null, B: null },
  togglePerformance: null,
  load: null,
  browse: null,
  lead: null,
  tree: null,
}

/** Messages a learn capture waits for before guessing an encoding. */
const LEARN_SAMPLES = 12
/** …or this long after the first one. */
const LEARN_WINDOW_MS = 800
/** A CC learned for a button waits this long: for a note-on (a touch jog sends
 *  both), or for more messages showing it is an encoder being turned. */
const LEARN_CC_BUTTON_MS = 400
/** A fast encoder turn may report several ticks at once; never more repeats. */
const MAX_TURN_REPEAT = 8

const learnKind = (k: ActionKind | 'shift'): LearnKind =>
  k === 'continuous' ? 'continuous' : k === 'jog' ? 'jog' : 'button'

interface Capture {
  target: LearnTarget
  kind: LearnKind
  port: string | null
  source: SourceKey | null
  samples: MidiMessage[]
  layer: Layer
  timer: ReturnType<typeof setTimeout> | null
}

export class MidiRouter {
  private shiftHeld = new Map<string, boolean>()
  private decoders = new Map<string, Decoder>()
  private held = new Map<string, string>()
  private capture: Capture | null = null
  private lastActivity = 0

  private targets: () => MidiTargets

  constructor(targets: () => MidiTargets) {
    this.targets = targets
  }

  /** Raw bytes from `port`. */
  receive(port: string, bytes: ArrayLike<number>): void {
    const m = parseMessage(bytes)
    if (m) this.handle(port, m)
  }

  handle(port: string, m: MidiMessage): void {
    const key = sourceKey(m)
    const st = getMidiState()
    // The settings dialog's activity line: a jog sends hundreds a second.
    const now = performance.now()
    if (now - this.lastActivity > 50) {
      this.lastActivity = now
      patchMidi({ activity: { port, source: key, value: m.value, at: now } })
    }
    const mapping = st.mappings[port]

    // Shift: tracked always, and never an action of its own.
    if (mapping?.shift === key && !(st.armed?.action === 'shift')) {
      this.shiftHeld.set(port, m.value > 0)
      return
    }
    if (st.armed) {
      this.learn(port, m, key, st.armed)
      return
    }
    if (!mapping) return
    const shift = !!this.shiftHeld.get(port)
    const hit = resolveBinding(mapping, key)
    // An encoder whose turns are bound by direction. The plain binding wins
    // where it has an action for this layer; else the turns do.
    const plus = mapping.bindings[turnSource(key, '+')]
    const minus = mapping.bindings[turnSource(key, '-')]
    if (plus || minus) {
      const plainHere = hit && (shift ? hit.binding.shift : hit.binding.normal)
      const turnHere = shift ? (plus?.shift ?? minus?.shift) : (plus?.normal ?? minus?.normal)
      if (!plainHere && (turnHere || !hit)) return this.turn(port, m, key, plus ?? minus!, shift, mapping)
    }
    if (!hit) return
    const { binding, isLsb } = hit
    const decKey = `${port}|${hit.key}`
    let dec = this.decoders.get(decKey)
    if (!dec || dec.encoding !== binding.encoding || dec.invert !== !!binding.invert) {
      dec = new Decoder(binding.encoding, !!binding.invert)
      this.decoders.set(decKey, dec)
    }
    const d = dec.feed(m, isLsb)
    if (!d) return

    const heldKey = `${port}|${hit.key}`
    // A release goes where its press went.
    if (d.type === 'press' && !d.down) {
      const action = this.held.get(heldKey)
      if (action) {
        this.held.delete(heldKey)
        this.run(action, d, mapping.jogSensitivity, mapping.scrubSpeed)
      }
      return
    }
    const action = shift ? (binding.shift ?? binding.normal) : binding.normal
    if (!action) return
    if (d.type === 'press' && actionDef(action)?.kind === 'hold') this.held.set(heldKey, action)
    this.run(action, d, mapping.jogSensitivity, mapping.scrubSpeed)
  }

  /** One message from an encoder bound by direction: each tick fires that
   *  direction's action once (a hold is pressed and released). */
  private turn(port: string, m: MidiMessage, key: SourceKey, any: Binding, shift: boolean, mapping: Mapping): void {
    const decKey = `${port}|${key}~turn`
    let dec = this.decoders.get(decKey)
    if (!dec || dec.encoding !== any.encoding) {
      dec = new Decoder(any.encoding)
      this.decoders.set(decKey, dec)
    }
    const d = dec.feed(m)
    if (!d || d.type !== 'delta') return
    const b = mapping.bindings[turnSource(key, d.ticks > 0 ? '+' : '-')]
    const action = b && (shift ? (b.shift ?? b.normal) : b.normal)
    if (!action) return
    const hold = actionDef(action)?.kind === 'hold'
    for (let i = 0; i < Math.min(Math.abs(d.ticks), MAX_TURN_REPEAT); i++) {
      this.run(action, { type: 'press', down: true }, mapping.jogSensitivity, mapping.scrubSpeed)
      if (hold) this.run(action, { type: 'press', down: false }, mapping.jogSensitivity, mapping.scrubSpeed)
    }
  }

  /** Forget held controls and decoder state (a controller unplugged). */
  forget(port: string): void {
    this.shiftHeld.delete(port)
    for (const k of [...this.decoders.keys()]) if (k.startsWith(`${port}|`)) this.decoders.delete(k)
    for (const k of [...this.held.keys()]) if (k.startsWith(`${port}|`)) this.held.delete(k)
  }

  // ---- actions -----------------------------------------------------------------

  private run(action: string, d: Decoded, sensitivity: number, scrubSpeed: number): void {
    const t = this.targets()
    if (performanceOnly(action) && !t.performance) return
    const def = actionDef(action)
    if (!def) return
    // A press as the action's kind wants it; a position on a button = pressed past halfway.
    const down = d.type === 'press' ? d.down : d.type === 'abs' ? d.unit > 0.5 : null

    if (action.startsWith('mixer.')) return this.mixer(action, d, down)
    if (action === 'app.performance') return void (down && t.togglePerformance?.())
    if (action === 'library.browse') return void (d.type === 'delta' && t.browse?.(d.ticks))
    if (action === 'library.tree') return void (d.type === 'delta' && t.tree?.step(d.ticks))
    if (action === 'library.browsePress') return void (down && t.load?.(t.focused))
    if (action === 'library.treePress') return void (down && t.tree?.press())

    const deckId = actionDeck(action)
    const deck = deckId && t.decks[deckId]
    if (!deck || !deckId) return
    const op = action.slice('deck.A.'.length)

    if (def.kind === 'continuous') {
      if (op !== 'pitch') return
      if (d.type === 'abs') deck.setPitch(d.bipolar)
      else if (d.type === 'delta') deck.setPitch(Math.max(-1, Math.min(1, deck.pitch + d.ticks / 512)))
      return
    }
    if (def.kind === 'jog') {
      if (d.type !== 'delta') return
      if (op === 'jog') deck.jogTurn(d.ticks, sensitivity)
      else if (op === 'scrub') deck.scrub(d.ticks, sensitivity * scrubSpeed)
      return
    }
    if (down == null) return
    if (def.kind === 'hold') {
      const slot = op.match(/^hotcue\.(\d+)$/)
      if (slot) {
        const s = Number(slot[1]) - 1
        if (s >= deck.slotCount) return
        return void (down ? deck.onSlotPress(s) : deck.onSlotRelease(s))
      }
      if (op === 'cue') return down ? deck.onCuePress() : deck.onCueRelease()
      if (op === 'jogTouch') return deck.jogTouch(down, sensitivity)
      return
    }
    if (!down) return
    const n = op.match(/^(hotcueErase|stem|stemSolo)\.(\d+)$/)
    if (n) {
      const i = Number(n[2]) - 1
      if (n[1] === 'hotcueErase') return void (i < deck.slotCount && deck.deleteHotcueSlot(i))
      return deck.toggleStem(i, n[1] === 'stemSolo')
    }
    switch (op) {
      case 'play':
        return deck.toggle()
      case 'sync':
        return deck.toggleSync()
      case 'keylock':
        return deck.toggleKeylock()
      case 'jumpBack':
        return deck.moveOrJump(-1)
      case 'jumpFwd':
        return deck.moveOrJump(1)
      case 'markerPrev':
        return deck.prevMarker()
      case 'markerNext':
        return deck.nextMarker()
      case 'sizeDown':
        return deck.stepSize(-1)
      case 'sizeUp':
        return deck.stepSize(1)
      case 'sizeLoop':
        return deck.toggleSizeLoop()
      case 'loopIn':
        return deck.loopIn()
      case 'loopOut':
        return deck.loopOut()
      case 'loopToggle':
        return deck.toggleLoop()
      case 'load':
        return void t.load?.(deckId)
    }
  }

  private mixer(action: string, d: Decoded, down: boolean | null): void {
    const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))
    if (action === 'mixer.crossfader') {
      const x = getMixerState().crossfader
      if (d.type === 'abs') setMixer({ crossfader: d.bipolar })
      else if (d.type === 'delta') setMixer({ crossfader: clamp(x + d.ticks / 64, -1, 1) })
      return
    }
    const m = action.match(/^mixer\.([AB])\.(\w+)$/)
    if (!m) return
    const ch = m[1] as DeckId
    const op = m[2]
    const s = getMixerState()[ch]
    if (op === 'fxNext' || op === 'fxPrev') {
      if (!down) return
      const i = FX_TYPES.indexOf(s.fx)
      const n = FX_TYPES.length
      return setChannel(ch, { fx: FX_TYPES[(i + (op === 'fxNext' ? 1 : n - 1)) % n] })
    }
    const field: Record<string, keyof ChannelState> = { gain: 'trim', high: 'high', mid: 'mid', low: 'low', filter: 'color', fader: 'fader' }
    const f = field[op]
    if (!f) return
    // Gain is ±TRIM_RANGE_DB, the fader 0..1, the rest -1..1 (centre flat).
    const [lo, hi] = f === 'trim' ? [-TRIM_RANGE_DB, TRIM_RANGE_DB] : f === 'fader' ? [0, 1] : [-1, 1]
    let v: number
    if (d.type === 'abs') v = f === 'fader' ? d.unit : d.bipolar * hi
    else if (d.type === 'delta') v = clamp((s[f] as number) + (d.ticks * (hi - lo)) / 128, lo, hi)
    else return
    setChannel(ch, { [f]: v })
  }

  // ---- learn ---------------------------------------------------------------------

  private learn(port: string, m: MidiMessage, key: SourceKey, target: LearnTarget): void {
    const kind = learnKind(target.action === 'shift' ? 'shift' : (actionDef(target.action)?.kind ?? 'button'))
    let c = this.capture
    if (!c || c.target !== target) {
      if (c?.timer) clearTimeout(c.timer)
      const layer: Layer = 'layer' in target ? (this.shiftHeld.get(port) ? 'shift' : target.layer) : 'normal'
      c = this.capture = { target, kind, port: null, source: null, samples: [], layer, timer: null }
    }
    if (c.port && c.port !== port) return // one controller at a time

    if (kind === 'button') {
      if (m.kind === 'note') {
        if (m.value > 0) this.commit(c, port, key, 'button')
        return
      }
      if (m.kind !== 'cc') return
      // A CC is either a button or an ENCODER turned for this action's
      // direction: wait and see what else it sends (and whether a note-on
      // follows — a touch jog sends both).
      if (!c.source) {
        if (m.value === 0) return
        c.port = port
        c.source = key
        c.timer = setTimeout(() => this.finishButton(), LEARN_CC_BUTTON_MS)
      }
      if (key === c.source) c.samples.push(m)
      return
    }
    // A position or ticks: notes (a jog's touch sensor, a stray pad) do not count.
    if (m.kind === 'note') return
    if (!c.source) {
      c.port = port
      c.source = key
      c.timer = setTimeout(() => this.finish(), LEARN_WINDOW_MS)
    } else if (m.kind === 'cc' && c.source.startsWith('cc:')) {
      // The MSB of a 14-bit pair is the lower number, whichever came first.
      const first = Number(c.source.split(':')[2])
      if (m.channel === Number(c.source.split(':')[1]) && m.number === first - 32) c.source = key
    }
    c.samples.push(m)
    if (c.samples.length >= LEARN_SAMPLES) this.finish()
  }

  /**
   * A CC learned for a button action: a press (one message, or a press and a
   * release to 0) binds as a button; several non-zero messages are an encoder
   * TURNED, bound to the direction it went (decided 2026-11-03: controllers
   * put beat jump and loop size on encoders).
   */
  private finishButton(): void {
    const c = this.capture
    if (!c || !c.port || !c.source) return
    const vals = c.samples
    if (vals.length < 2 || vals.some((v) => v.value === 0)) return this.commit(c, c.port, c.source, 'button')
    const enc = detectEncoding(vals, 'jog')
    const dec = new Decoder(enc)
    let ticks = 0
    for (const v of vals) {
      const d = dec.feed(v)
      if (d?.type === 'delta') ticks += d.ticks
    }
    if (ticks === 0) return this.commit(c, c.port, c.source, 'button')
    this.commit(c, c.port, turnSource(c.source, ticks > 0 ? '+' : '-'), enc)
  }

  /** Commit a continuous / jog capture now (its window ran out). */
  finish(): void {
    const c = this.capture
    if (!c || !c.port || !c.source) return
    const src = c.source
    // The bound control's own message first: the guess reads its kind and number.
    const lead = c.samples.find((s) => sourceKey(s) === src)
    const ordered = lead ? [lead, ...c.samples.filter((s) => s !== lead)] : c.samples
    // A 14-bit pair binds on its MSB; anything else on the control that moved.
    this.commit(c, c.port, src, detectEncoding(ordered, c.kind))
  }

  private commit(c: Capture, port: string, source: SourceKey, encoding: Encoding): void {
    if (c.timer) clearTimeout(c.timer)
    if (this.capture !== c) return
    this.capture = null
    const t = c.target
    if (t.action === 'shift') updateMapping(port, (m) => bindShift(m, source))
    else {
      updateMapping(port, (m) => bind(m, source, encoding, c.layer, t.action))
      patchMidi({ learned: { port, action: t.action, layer: c.layer, at: performance.now() } })
    }
    this.decoders.delete(`${port}|${source}`)
    this.decoders.delete(`${port}|${splitSource(source).base}~turn`)
    armLearn(null)
  }
}
