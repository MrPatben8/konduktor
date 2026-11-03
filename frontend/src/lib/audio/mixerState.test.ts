import { describe, expect, it } from 'vitest'
import { DEFAULT_FX, FX_TYPES, rangeFor } from './mixerState'

describe('rangeFor', () => {
  it('the Filter alone follows its setting, an effect the other', () => {
    const fx = { ...DEFAULT_FX, filterRange: 'full' as const, effectRange: 'gentle' as const }
    expect(rangeFor('filter', fx)).toBe('full')
    for (const t of FX_TYPES.filter((x) => x !== 'filter')) expect(rangeFor(t, fx)).toBe('gentle')
  })
  it('defaults: Full alone, Musical with an effect', () => {
    expect(rangeFor('filter', DEFAULT_FX)).toBe('full')
    expect(rangeFor('reverb', DEFAULT_FX)).toBe('musical')
  })
})
