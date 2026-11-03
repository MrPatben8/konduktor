import { describe, expect, it } from 'vitest'
import { ACTIONS, shiftDefault } from './actions'
import { bind, bindShift, emptyMapping, resolveBinding, sanitizeMapping, sourcesFor, unbind } from './mapping'

describe('actions', () => {
  it('have unique ids', () => {
    expect(new Set(ACTIONS.map((a) => a.id)).size).toBe(ACTIONS.length)
  })
  it("default Shift layer = the keyboard's Shift meaning", () => {
    expect(shiftDefault('deck.A.hotcue.3')).toBe('deck.A.hotcueErase.3')
    expect(shiftDefault('deck.B.stem.2')).toBe('deck.B.stemSolo.2')
    expect(shiftDefault('deck.A.jumpFwd')).toBe('deck.A.markerNext')
    expect(shiftDefault('deck.B.jog')).toBe('deck.B.scrub')
    expect(shiftDefault('library.browse')).toBe('library.tree')
    expect(shiftDefault('deck.A.play')).toBeNull()
    for (const a of ACTIONS) {
      const d = shiftDefault(a.id)
      if (d) expect(ACTIONS.some((x) => x.id === d)).toBe(true)
    }
  })
})

describe('mapping', () => {
  it('learning fills the Shift layer with the default, and replaces the old control', () => {
    let m = bind(emptyMapping(), 'note:1:0', 'button', 'normal', 'deck.A.hotcue.1')
    expect(m.bindings['note:1:0']).toEqual({ encoding: 'button', normal: 'deck.A.hotcue.1', shift: 'deck.A.hotcueErase.1' })
    m = bind(m, 'note:1:8', 'button', 'normal', 'deck.A.hotcue.1')
    expect(sourcesFor(m, 'deck.A.hotcue.1', 'normal')).toEqual(['note:1:8'])
    // The old pad keeps its Shift action, and the new one does not duplicate it.
    expect(m.bindings['note:1:0']).toEqual({ encoding: 'button', shift: 'deck.A.hotcueErase.1' })
    expect(m.bindings['note:1:8'].shift).toBeUndefined()
  })
  it('a free Shift layer: any action under Shift', () => {
    let m = bind(emptyMapping(), 'note:1:11', 'button', 'normal', 'deck.A.play')
    m = bind(m, 'note:1:11', 'button', 'shift', 'deck.A.sync')
    expect(m.bindings['note:1:11']).toEqual({ encoding: 'button', normal: 'deck.A.play', shift: 'deck.A.sync' })
    m = unbind(m, 'deck.A.play', 'normal')
    expect(m.bindings['note:1:11']).toEqual({ encoding: 'button', shift: 'deck.A.sync' })
    m = unbind(m, 'deck.A.sync', 'shift')
    expect(m.bindings['note:1:11']).toBeUndefined()
  })
  it('the Shift control does nothing else', () => {
    let m = bind(emptyMapping(), 'note:1:63', 'button', 'normal', 'deck.A.play')
    m = bindShift(m, 'note:1:63')
    expect(m.shift).toBe('note:1:63')
    expect(m.bindings['note:1:63']).toBeUndefined()
    m = bind(m, 'note:1:63', 'button', 'normal', 'deck.A.cue')
    expect(m.shift).toBeNull()
  })
  it('the LSB half of a 14-bit pair reaches its MSB', () => {
    const m = bind(emptyMapping(), 'cc:1:0', 'abs14', 'normal', 'deck.A.pitch')
    expect(resolveBinding(m, 'cc:1:32')).toMatchObject({ key: 'cc:1:0', isLsb: true })
    const seven = bind(emptyMapping(), 'cc:1:0', 'abs7', 'normal', 'deck.A.pitch')
    expect(resolveBinding(seven, 'cc:1:32')).toBeNull()
  })
  it('sanitises what prefs or a file hold', () => {
    const m = sanitizeMapping({
      version: 1,
      shift: 'note:1:63',
      jogSensitivity: 99,
      bindings: {
        'note:1:0': { encoding: 'button', normal: 'deck.A.play' },
        'note:1:1': { encoding: 'bogus', normal: 'deck.A.play' },
        'note:1:2': { encoding: 'button', normal: 'no.such.action' },
        'nonsense': { encoding: 'button', normal: 'deck.A.play' },
      },
    })
    expect(m?.shift).toBe('note:1:63')
    expect(m?.jogSensitivity).toBe(10)
    expect(Object.keys(m!.bindings)).toEqual(['note:1:0'])
    expect(sanitizeMapping({ version: 2 })).toBeNull()
  })
})
