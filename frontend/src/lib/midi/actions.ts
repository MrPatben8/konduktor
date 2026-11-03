// Everything a controller can be mapped to. Buttons do what the deck keys do
// (lib/deckKeys.ts), so a key and a pad mean the same thing; the rest are the
// controls a keyboard has no equivalent for — pitch, jog, the mixer, browsing.
//
// Kinds: `button` acts on press; `hold` also on release (CUE and hotcue
// previews, jog touch); `continuous` takes a position (or relative ticks from
// an endless knob); `jog` takes ticks.

export type DeckId = 'A' | 'B'
export type ActionKind = 'button' | 'hold' | 'continuous' | 'jog'
export type ActionGroup = 'Deck A' | 'Deck B' | 'Mixer' | 'Library' | 'App'

export interface ActionDef {
  id: string
  label: string
  group: ActionGroup
  kind: ActionKind
}

/** Hotcue pads offered per deck (a platform's bank may be smaller: the deck ignores the rest). */
export const HOTCUE_ACTIONS = 8
export const STEM_ACTIONS = 4

function deckActions(d: DeckId): ActionDef[] {
  const group: ActionGroup = `Deck ${d}`
  const a = (id: string, label: string, kind: ActionKind = 'button'): ActionDef => ({
    id: `deck.${d}.${id}`,
    label,
    group,
    kind,
  })
  const range = (n: number) => Array.from({ length: n }, (_, i) => i + 1)
  return [
    a('play', 'Play / pause'),
    a('cue', 'CUE', 'hold'),
    a('sync', 'SYNC'),
    a('keylock', 'Keylock'),
    a('pitch', 'Pitch fader', 'continuous'),
    a('jog', 'Jog wheel', 'jog'),
    a('jogTouch', 'Jog touch (scratch)', 'hold'),
    a('scrub', 'Fast scrub', 'jog'),
    ...range(HOTCUE_ACTIONS).map((n) => a(`hotcue.${n}`, `Hotcue ${n}`, 'hold')),
    ...range(HOTCUE_ACTIONS).map((n) => a(`hotcueErase.${n}`, `Erase hotcue ${n}`)),
    a('jumpBack', 'Beat jump / move loop back'),
    a('jumpFwd', 'Beat jump / move loop forward'),
    a('markerPrev', 'Previous grid marker'),
    a('markerNext', 'Next grid marker'),
    a('sizeDown', 'Loop / jump size −'),
    a('sizeUp', 'Loop / jump size +'),
    a('sizeLoop', 'Loop (at size)'),
    a('loopIn', 'Loop in'),
    a('loopOut', 'Loop out'),
    a('loopToggle', 'Loop on / off'),
    ...range(STEM_ACTIONS).map((n) => a(`stem.${n}`, `Mute stem ${n}`)),
    ...range(STEM_ACTIONS).map((n) => a(`stemSolo.${n}`, `Solo stem ${n}`)),
    a('load', 'Load selected track'),
  ]
}

function channelActions(c: DeckId): ActionDef[] {
  const a = (id: string, label: string, kind: ActionKind = 'continuous'): ActionDef => ({
    id: `mixer.${c}.${id}`,
    label: `${c}: ${label}`,
    group: 'Mixer',
    kind,
  })
  return [
    a('gain', 'Gain'),
    a('high', 'EQ high'),
    a('mid', 'EQ mid'),
    a('low', 'EQ low'),
    a('filter', 'Filter'),
    a('fxNext', 'Next effect', 'button'),
    a('fxPrev', 'Previous effect', 'button'),
    a('fader', 'Channel fader'),
  ]
}

export const ACTIONS: ActionDef[] = [
  ...deckActions('A'),
  ...deckActions('B'),
  ...channelActions('A'),
  ...channelActions('B'),
  { id: 'mixer.crossfader', label: 'Crossfader', group: 'Mixer', kind: 'continuous' },
  { id: 'library.browse', label: 'Browse tracks', group: 'Library', kind: 'jog' },
  { id: 'library.browsePress', label: 'Load to focused deck', group: 'Library', kind: 'button' },
  { id: 'library.tree', label: 'Browse playlists', group: 'Library', kind: 'jog' },
  { id: 'library.treePress', label: 'Open / close folder', group: 'Library', kind: 'button' },
  { id: 'app.performance', label: 'Performance mode on / off', group: 'App', kind: 'button' },
]

export const ACTION_GROUPS: ActionGroup[] = ['Deck A', 'Deck B', 'Mixer', 'Library', 'App']

const BY_ID = new Map(ACTIONS.map((a) => [a.id, a]))
export const actionDef = (id: string): ActionDef | undefined => BY_ID.get(id)

/**
 * What a control does under Shift when nothing else is chosen: the keyboard's
 * Shift meaning (hotcue -> erase it, stem -> solo it, beat jump -> step
 * markers), plus Shift+jog = fast scrub and Shift+browse = the playlist tree
 * (decided 2026-11-03). Filled in when the control is learned; overridable.
 */
export function shiftDefault(id: string): string | null {
  const m = id.match(/^deck\.([AB])\.(.+)$/)
  if (m) {
    const [, d, rest] = m
    const swap: Record<string, string> = { jumpBack: 'markerPrev', jumpFwd: 'markerNext', jog: 'scrub' }
    if (swap[rest]) return `deck.${d}.${swap[rest]}`
    const n = rest.match(/^(hotcue|stem)\.(\d+)$/)
    if (n) return `deck.${d}.${n[1] === 'hotcue' ? 'hotcueErase' : 'stemSolo'}.${n[2]}`
    return null
  }
  if (id === 'library.browse') return 'library.tree'
  if (id === 'library.browsePress') return 'library.treePress'
  return null
}

/** The deck an action belongs to, if any (mixer channels count as their deck). */
export function actionDeck(id: string): DeckId | null {
  const m = id.match(/^(?:deck|mixer)\.([AB])\./)
  return m ? (m[1] as DeckId) : null
}

/** Acts only in performance mode: deck B and the whole mixer (prep mode has
 *  one deck and plays past the mixer). */
export function performanceOnly(id: string): boolean {
  return id.startsWith('mixer.') || actionDeck(id) === 'B'
}
