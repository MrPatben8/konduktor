import { describe, expect, it } from 'vitest'
import { formatLoudness } from './format'

describe('formatLoudness', () => {
  it('shows one decimal and the unit', () => {
    expect(formatLoudness(-8.24, 'LUFS')).toBe('-8.2 LUFS')
    expect(formatLoudness(5.06, 'LU')).toBe('5.1 LU')
  })
  it('signs a positive value only when asked (true peak)', () => {
    expect(formatLoudness(0.31, 'dBTP', true)).toBe('+0.3 dBTP')
    expect(formatLoudness(-1.0, 'dBTP', true)).toBe('-1.0 dBTP')
    expect(formatLoudness(8.5, 'dB')).toBe('8.5 dB')
  })
  it('never shows a negative or signed zero', () => {
    expect(formatLoudness(-0.04, 'dBTP', true)).toBe('0.0 dBTP')
    expect(formatLoudness(0.04, 'dBTP', true)).toBe('0.0 dBTP')
  })
  it('an unmeasured value is a dash', () => {
    expect(formatLoudness(undefined, 'LUFS')).toBe('—')
  })
})
