import { beforeEach, describe, expect, it, vi } from 'vitest'
import { bind, bindShift, emptyMapping, type Mapping } from './mapping'
import { EMPTY_TARGETS, MidiRouter, type DeckMidi, type MidiTargets } from './router'
import { armLearn, getMidiState, replaceMappings } from './store'
import { getMixerState, setChannel, setMixer } from '../audio/mixerState'

const PORT = 'DDJ'

function fakeDeck(): DeckMidi & { calls: string[] } {
  const calls: string[] = []
  const log = (name: string) => (...args: unknown[]) => void calls.push([name, ...args].join(' '))
  return {
    calls,
    slotCount: 8,
    pitch: 0,
    toggle: log('toggle'),
    onCuePress: log('cuePress'),
    onCueRelease: log('cueRelease'),
    onSlotPress: log('slotPress'),
    onSlotRelease: log('slotRelease'),
    deleteHotcueSlot: log('deleteSlot'),
    toggleStem: log('stem'),
    moveOrJump: log('move'),
    prevMarker: log('prevMarker'),
    nextMarker: log('nextMarker'),
    stepSize: log('stepSize'),
    toggleSizeLoop: log('sizeLoop'),
    loopIn: log('loopIn'),
    loopOut: log('loopOut'),
    toggleLoop: log('toggleLoop'),
    toggleSync: log('sync'),
    toggleKeylock: log('keylock'),
    setPitch: log('pitch'),
    jogTouch: log('jogTouch'),
    jogTurn: log('jogTurn'),
    scrub: log('scrub'),
  }
}

let A: ReturnType<typeof fakeDeck>
let B: ReturnType<typeof fakeDeck>
let targets: MidiTargets
let router: MidiRouter
const note = (n: number, v: number) => router.receive(PORT, [0x90, n, v])
const cc = (n: number, v: number) => router.receive(PORT, [0xb0, n, v])

function map(fn: (m: Mapping) => Mapping) {
  replaceMappings({ [PORT]: fn(emptyMapping()) }, false)
}

beforeEach(() => {
  A = fakeDeck()
  B = fakeDeck()
  targets = { ...EMPTY_TARGETS, decks: { A, B }, load: vi.fn(), browse: vi.fn() }
  router = new MidiRouter(() => targets)
  armLearn(null)
  setMixer({ crossfader: 0 })
  setChannel('A', { fader: 1, trim: 0, color: 0, fx: 'filter' })
  map((m) => {
    m = bindShift(m, 'note:1:63')
    m = bind(m, 'note:1:11', 'button', 'normal', 'deck.A.play')
    m = bind(m, 'note:1:0', 'button', 'normal', 'deck.A.hotcue.1')
    m = bind(m, 'note:2:11', 'button', 'normal', 'deck.B.play')
    m = bind(m, 'cc:1:33', 'rel64', 'normal', 'deck.A.jog')
    m = bind(m, 'note:1:54', 'button', 'normal', 'deck.A.jogTouch')
    m = bind(m, 'cc:1:0', 'abs14', 'normal', 'deck.A.pitch')
    m = bind(m, 'cc:7:19', 'abs7', 'normal', 'mixer.A.fader')
    m = bind(m, 'cc:7:31', 'abs7', 'normal', 'mixer.crossfader')
    m = bind(m, 'cc:7:64', 'rel64', 'normal', 'library.browse')
    return m
  })
})

describe('router', () => {
  it('runs a button on press only', () => {
    note(11, 127)
    note(11, 0)
    expect(A.calls).toEqual(['toggle'])
  })
  it('Shift + pad erases the hotcue; the pad alone presses and releases it', () => {
    note(0, 127)
    note(0, 0)
    note(63, 127)
    note(0, 127)
    note(63, 0)
    expect(A.calls).toEqual(['slotPress 0', 'slotRelease 0', 'deleteSlot 0'])
  })
  it('a held pad releases where it was pressed, even if Shift changes between', () => {
    note(0, 127)
    note(63, 127)
    note(0, 0)
    expect(A.calls).toEqual(['slotPress 0', 'slotRelease 0'])
  })
  it('an unbound Shift layer falls back to the normal action', () => {
    note(63, 127)
    note(11, 127)
    expect(A.calls).toEqual(['toggle'])
  })
  it('prep mode: deck B and the mixer do nothing', () => {
    router.receive(PORT, [0x91, 11, 127]) // deck B's play, channel 2
    router.receive(PORT, [0xb6, 19, 0]) // channel A's fader, channel 7
    expect(B.calls).toEqual([])
    expect(getMixerState().A.fader).toBe(1)
    targets.performance = true
    router.receive(PORT, [0x91, 11, 127])
    router.receive(PORT, [0xb6, 19, 0])
    expect(B.calls).toEqual(['toggle'])
    expect(getMixerState().A.fader).toBe(0)
  })
  it('positions jump straight to the control (no takeover)', () => {
    targets.performance = true
    router.receive(PORT, [0xb6, 31, 127])
    expect(getMixerState().crossfader).toBe(1)
    router.receive(PORT, [0xb6, 31, 64])
    expect(getMixerState().crossfader).toBe(0)
  })
  it('a 14-bit pitch fader: MSB then LSB', () => {
    cc(0, 0)
    cc(32, 0)
    expect(A.calls.at(-1)).toBe('pitch -1')
    cc(0, 64)
    expect(A.calls.at(-1)).toBe('pitch 0')
  })
  it('jog ticks carry the mapping sensitivity; Shift + jog = fast scrub', () => {
    cc(33, 66)
    note(63, 127)
    cc(33, 62)
    expect(A.calls).toEqual(['jogTurn 2 1', 'scrub -2 8'])
  })
  it('jog touch is a hold', () => {
    note(54, 127)
    note(54, 0)
    expect(A.calls).toEqual(['jogTouch true 1', 'jogTouch false 1'])
  })
  it('browse moves the track list; Shift + browse would move the tree', () => {
    router.receive(PORT, [0xb6, 64, 63])
    expect(targets.browse).toHaveBeenCalledWith(-1)
  })
})

describe('learning', () => {
  it('a button learns from its first press, into the Shift layer if Shift is held', () => {
    armLearn({ action: 'deck.A.cue', layer: 'normal' })
    note(12, 127)
    expect(getMidiState().mappings[PORT].bindings['note:1:12']?.normal).toBe('deck.A.cue')
    expect(getMidiState().armed).toBeNull()
    armLearn({ action: 'deck.A.sync', layer: 'normal' })
    note(63, 127)
    note(12, 127)
    expect(getMidiState().mappings[PORT].bindings['note:1:12']?.shift).toBe('deck.A.sync')
    expect(A.calls).toEqual([]) // learning never acts
  })
  it('a continuous control learns its encoding from what it sent', () => {
    armLearn({ action: 'mixer.B.fader', layer: 'normal' })
    for (const v of [10, 20, 30, 40, 50, 60, 70, 80, 90, 80, 70, 60]) router.receive(PORT, [0xb1, 19, v])
    expect(getMidiState().mappings[PORT].bindings['cc:2:19']).toEqual({ encoding: 'abs7', normal: 'mixer.B.fader' })
  })
  it('a jog ignores its touch note while learning, and finds it is relative', () => {
    armLearn({ action: 'deck.B.jog', layer: 'normal' })
    router.receive(PORT, [0x91, 54, 127])
    for (const v of [65, 65, 66, 65, 65, 63, 63, 62, 63, 63, 65, 65]) router.receive(PORT, [0xb1, 34, v])
    expect(getMidiState().mappings[PORT].bindings['cc:2:34']).toMatchObject({ encoding: 'rel64', normal: 'deck.B.jog', shift: 'deck.B.scrub' })
  })
  it('a 14-bit pitch fader learns on its MSB, whichever half arrives first', () => {
    armLearn({ action: 'deck.B.pitch', layer: 'normal' })
    for (let i = 0; i < 6; i++) {
      router.receive(PORT, [0xb1, 32 + 5, (i * 37) % 128]) // LSB (CC 37)
      router.receive(PORT, [0xb1, 5, 60 + i]) // MSB (CC 5)
    }
    const b = getMidiState().mappings[PORT].bindings
    expect(b['cc:2:5']).toEqual({ encoding: 'abs14', normal: 'deck.B.pitch' })
    expect(b['cc:2:37']).toBeUndefined()
    // …and then drives the deck from both halves.
    targets.performance = true
    router.receive(PORT, [0xb1, 5, 127])
    router.receive(PORT, [0xb1, 37, 127])
    expect(B.calls.at(-1)).toBe('pitch 1')
  })
  it('a jog reporting its absolute position learns as one, and turns across the wrap', () => {
    armLearn({ action: 'deck.A.jog', layer: 'normal' })
    for (const v of [100, 108, 116, 124, 4, 12, 20, 28, 36, 44, 52, 60]) cc(40, v)
    expect(getMidiState().mappings[PORT].bindings['cc:1:40']?.encoding).toBe('jogAbs7')
    cc(40, 126)
    cc(40, 3)
    expect(A.calls).toEqual(['jogTurn 5 1'])
  })
  it('the Shift control itself', () => {
    armLearn({ action: 'shift' })
    note(70, 127)
    expect(getMidiState().mappings[PORT].shift).toBe('note:1:70')
  })
})
