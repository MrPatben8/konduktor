import { describe, expect, it } from 'vitest'
import { keyAction, slotForDigit, type KeyLike } from './deckKeys'

const key = (code: string, mods: Partial<KeyLike> = {}): KeyLike => ({
  code,
  shiftKey: false,
  altKey: false,
  metaKey: false,
  ctrlKey: false,
  repeat: false,
  ...mods,
})
const eight = () => 8

describe('keyAction', () => {
  it('maps the deck keys', () => {
    expect(keyAction(key('Space'), eight, false)).toEqual({ action: { kind: 'toggle' }, toOther: false })
    expect(keyAction(key('KeyC'), eight, false)?.action).toEqual({ kind: 'cue' })
    expect(keyAction(key('KeyE', { shiftKey: true }), eight, false)?.action).toEqual({ kind: 'stem', index: 2, solo: true })
    expect(keyAction(key('ArrowLeft'), eight, false)?.action).toEqual({ kind: 'move', dir: -1 })
    expect(keyAction(key('ArrowRight', { shiftKey: true }), eight, false)?.action).toEqual({ kind: 'marker', dir: 1 })
    expect(keyAction(key('Digit3'), eight, false)?.action).toEqual({ kind: 'slot', slot: 2, remove: false })
    expect(keyAction(key('Numpad1', { shiftKey: true }), eight, false)?.action).toEqual({ kind: 'slot', slot: 0, remove: true })
    expect(keyAction(key('ArrowUp', { metaKey: true }), eight, false)?.action).toEqual({ kind: 'stepSize', dir: 1 })
    expect(keyAction(key('ArrowDown', { ctrlKey: true }), eight, false)?.action).toEqual({ kind: 'stepSize', dir: -1 })
  })

  it('leaves other keys and modified keys alone', () => {
    expect(keyAction(key('KeyZ'), eight, false)).toBeNull()
    expect(keyAction(key('Space', { metaKey: true }), eight, false)).toBeNull()
    expect(keyAction(key('ArrowUp', { metaKey: true, shiftKey: true }), eight, false)).toBeNull()
    expect(keyAction(key('Digit9'), eight, false)).toBeNull() // beyond an 8-pad bank
  })

  it('treats an auto-repeat as one press, except the size sweep', () => {
    expect(keyAction(key('Space', { repeat: true }), eight, false)).toBeNull()
    expect(keyAction(key('ArrowUp', { metaKey: true, repeat: true }), eight, false)?.action.kind).toBe('stepSize')
  })

  it('Alt sends the key to the other deck — and does nothing with one deck', () => {
    expect(keyAction(key('Space', { altKey: true }), eight, true)).toEqual({ action: { kind: 'toggle' }, toOther: true })
    expect(keyAction(key('ArrowUp', { altKey: true, metaKey: true }), eight, true)?.toOther).toBe(true)
    expect(keyAction(key('Space', { altKey: true }), eight, false)).toBeNull()
  })

  it("reads the TARGET deck's bank size for digits", () => {
    const banks = (toOther: boolean) => (toOther ? 4 : 8)
    expect(keyAction(key('Digit6'), banks, true)?.action).toEqual({ kind: 'slot', slot: 5, remove: false })
    expect(keyAction(key('Digit6', { altKey: true }), banks, true)).toBeNull()
  })
})

describe('slotForDigit', () => {
  it('maps 1..9 to 0..8 and 0 to 9', () => {
    expect(slotForDigit('Digit1', 16)).toBe(0)
    expect(slotForDigit('Digit9', 16)).toBe(8)
    expect(slotForDigit('Digit0', 16)).toBe(9)
    expect(slotForDigit('KeyA', 16)).toBeNull()
  })
})
