import { describe, expect, it } from 'vitest'

import { maxPosition, moveToPosition } from './playlistOrder'

const order = ['A', 'B', 'C', 'D', 'E', 'F', 'G']
const move = (ids: string[], position: number) => moveToPosition(order, new Set(ids), position).join('')

describe('moveToPosition', () => {
  it('puts the track AT the position, moving down or up', () => {
    expect(move(['B'], 5)).toBe('ACDEBFG')
    expect(move(['F'], 2)).toBe('AFBCDEG')
    expect(move(['A'], 1)).toBe('ABCDEFG')
  })

  it('moves a selection as one block in playlist order', () => {
    expect(move(['F', 'B'], 2)).toBe('ABFCDEG')
    expect(move(['G', 'A', 'D'], 3)).toBe('BCADGEF')
  })

  it('clamps past either end', () => {
    expect(move(['B', 'C'], 99)).toBe('ADEFGBC')
    expect(move(['C'], 0)).toBe('CABDEFG')
  })

  it('moves every entry of a track listed twice', () => {
    expect(moveToPosition(['A', 'B', 'A', 'C'], new Set(['A']), 2).join('')).toBe('BAAC')
  })
})

describe('moveToPosition with a view order', () => {
  const sorted = (ids: string[], position: number, view: string[]) =>
    moveToPosition(order, new Set(ids), position, view).join('')

  it('moves the block in the order the view shows it', () => {
    expect(sorted(['B', 'E', 'F'], 1, ['F', 'B', 'E'])).toBe('FBEACDG')
    expect(sorted(['B', 'E', 'F'], 3, ['E', 'F', 'B'])).toBe('ACEFBDG')
  })

  it('ignores view ids that are not moving', () => {
    expect(sorted(['B', 'D'], 1, ['G', 'D', 'A', 'B'])).toBe('DBACEFG')
  })

  it('keeps a twice-listed track together, and appends a track the view lacks', () => {
    expect(moveToPosition(['A', 'B', 'A', 'C'], new Set(['A', 'C']), 1, ['C', 'A']).join('')).toBe('CAAB')
    expect(sorted(['B', 'C'], 1, ['C'])).toBe('CBADEFG')
  })
})

describe('maxPosition', () => {
  it('leaves room for the block behind its first track', () => {
    expect(maxPosition(order, new Set(['B']))).toBe(7)
    expect(maxPosition(order, new Set(['B', 'C', 'D']))).toBe(5)
    expect(move(['B', 'C', 'D'], 5)).toBe('AEFGBCD')
  })
})
