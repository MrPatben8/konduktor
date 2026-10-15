// What every shortcut does, for the keyboard map in `ShortcutsDialog`.
// Written out BY HAND, not derived from the handlers: keep it in step with
// `PrepStrip` (deck), `TrackTable` (track list) and `SettingsMenu` (`?`).
import { cueTypeColor, GRID_MARKER_ACTIVE_COLOR } from './cues'

export const IS_MAC = /Mac|iPhone|iPad/.test(navigator.userAgent)

/** A modifier layer of the keyboard. `mod` is Cmd on macOS, Ctrl elsewhere —
 *  the handlers accept either, but the map names your key. */
export type Layer = 'base' | 'shift' | 'mod' | 'alt'
export const LAYERS: Layer[] = ['base', 'shift', 'mod', 'alt']

export const MODIFIER_KEY: Record<Exclude<Layer, 'base'>, string> = IS_MAC
  ? { shift: '⇧', mod: '⌘', alt: '⌥' }
  : { shift: 'Shift', mod: 'Ctrl', alt: 'Alt' }

export const LAYER_NAMES: Record<Layer, string> = IS_MAC
  ? { base: 'No modifier', shift: '⇧ Shift', mod: '⌘ Command', alt: '⌥ Option' }
  : { base: 'No modifier', shift: 'Shift', mod: 'Ctrl', alt: 'Alt' }

/** Where a shortcut acts. The colour lights its key on the map. */
export type Area = 'deck' | 'list' | 'any'
export const AREAS: Record<Area, string> = { deck: 'Deck', list: 'Track list', any: 'Anywhere' }
export const AREA_COLORS: Record<Area, string> = {
  deck: 'var(--color-accent)',
  list: '#6fd6e8',
  any: 'var(--color-text)',
}

export interface Binding {
  area: Area
  /** Printed on the key — a word or two; the key's own legend names it. */
  label: string
  /** The caption's full sentence. */
  text: string
  /** Overrides the area colour where the key has a colour of its own (pads, stems). */
  color?: string
}
export type Bindings = Record<Layer, Record<string, Binding[]>>

/** A stem file's default layout (NI's), since the map cannot know the loaded track's. */
const STEMS: [string, string][] = [
  ['drums', '#009E73'],
  ['bass', '#D55E00'],
  ['synths', '#CC79A7'],
  ['vox', '#56B4E9'],
]
const STEM_CODES = ['KeyQ', 'KeyW', 'KeyE', 'KeyR']

/** The digit keys of a bank of `slots` pads: 1–9, then 0 for the tenth, as
 *  `PrepStrip`'s `slotForDigit` reads them. */
export function padCodes(slots: number): string[] {
  return Array.from({ length: Math.min(slots, 10) }, (_, i) => (i === 9 ? 'Digit0' : `Digit${i + 1}`))
}

/** "1–8", or "1–9, 0" for a ten-pad bank. */
export function padRange(slots: number): string {
  const n = Math.min(slots, 10)
  if (n <= 1) return '1'
  if (n <= 9) return `1–${n}`
  return '1–9, 0'
}

export function bindings(slots: number): Bindings {
  const b: Bindings = { base: {}, shift: {}, mod: {}, alt: {} }
  const pad = cueTypeColor('cue')
  padCodes(slots).forEach((code, i) => {
    const n = i + 1
    b.base[code] = [
      { area: 'deck', label: 'Pad', color: pad, text: `Hotcue ${n}: set it if the pad is empty, otherwise jump to it. Hold to preview.` },
    ]
    b.shift[code] = [{ area: 'deck', label: 'Delete', color: 'var(--color-pink)', text: `Delete hotcue ${n}` }]
  })
  STEM_CODES.forEach((code, i) => {
    const [name, color] = STEMS[i]
    b.base[code] = [{ area: 'deck', label: 'Mute', color, text: `Stem tracks: mute or unmute stem ${i + 1} (usually ${name})` }]
    b.shift[code] = [
      { area: 'deck', label: 'Solo', color, text: `Solo stem ${i + 1}: mute every other stem. Press again to unmute them all.` },
    ]
  })
  Object.assign(b.base, {
    Space: [{ area: 'deck', label: 'Play / pause', text: 'Play / pause' }],
    KeyC: [
      { area: 'deck', label: 'CUE', color: 'var(--color-gold)', text: 'CUE: jump back to the cue point, set it while paused, or hold to preview' },
    ],
    ArrowLeft: [
      { area: 'deck', label: 'Jump −', text: 'Beat jump back by the loop size. While a loop plays, move the loop back by its length.' },
    ],
    ArrowRight: [
      { area: 'deck', label: 'Jump +', text: 'Beat jump forward by the loop size. While a loop plays, move the loop forward by its length.' },
    ],
    ArrowUp: [{ area: 'list', label: 'Up', text: 'Move the selection up one track' }],
    ArrowDown: [{ area: 'list', label: 'Down', text: 'Move the selection down one track' }],
    Enter: [
      { area: 'list', label: 'Load · OK', text: 'Load and play the highlighted track' },
      { area: 'any', label: 'OK', text: 'Save an edit, or confirm a dialog' },
    ],
    Backspace: [
      {
        area: 'list',
        label: 'Remove',
        text: `Remove the selected tracks from this playlist${IS_MAC ? '' : ' (Delete works too)'}`,
      },
    ],
    Slash: [{ area: 'any', label: 'Help', text: 'Open this list of shortcuts' }],
    Escape: [
      { area: 'list', label: 'Cancel', text: 'Clear the selection' },
      { area: 'any', label: 'Cancel', text: 'Cancel a drag or an edit, or close a dialog' },
    ],
  } satisfies Record<string, Binding[]>)
  Object.assign(b.shift, {
    ArrowLeft: [{ area: 'deck', label: '◂ Grid', color: GRID_MARKER_ACTIVE_COLOR, text: 'Go to the previous grid marker' }],
    ArrowRight: [{ area: 'deck', label: 'Grid ▸', color: GRID_MARKER_ACTIVE_COLOR, text: 'Go to the next grid marker' }],
    ArrowUp: [{ area: 'list', label: 'Extend', text: 'Extend the selection up' }],
    ArrowDown: [{ area: 'list', label: 'Extend', text: 'Extend the selection down' }],
  } satisfies Record<string, Binding[]>)
  Object.assign(b.mod, {
    ArrowDown: [{ area: 'deck', label: 'Size ÷2', text: 'Halve the loop and beat-jump size (down to 1/32 beat)' }],
    ArrowUp: [{ area: 'deck', label: 'Size ×2', text: 'Double the loop and beat-jump size (up to 32 beats)' }],
    KeyA: [{ area: 'list', label: 'All', text: 'Select every track in the list' }],
  } satisfies Record<string, Binding[]>)
  return b
}

/** Why a digit past the bank does nothing — the bank follows the library. */
export function unusedNote(code: string, slots: number): string | null {
  if (!/^Digit\d$/.test(code) || padCodes(slots).includes(code)) return null
  return `Not used: this library has ${slots} hotcue pad${slots === 1 ? '' : 's'}.`
}

export interface MouseGesture {
  mod?: Exclude<Layer, 'base'>
  gesture: string
  /** What is clicked or dragged, when it is not the column's whole area. */
  on?: string
  text: string
}
export const MOUSE: { title: string; rows: MouseGesture[] }[] = [
  {
    title: 'Deck',
    rows: [
      { gesture: 'Drag', on: 'waveform', text: 'Scratch' },
      { mod: 'mod', gesture: 'Scroll', on: 'waveform', text: 'Zoom in and out' },
      { gesture: 'Click', on: 'stem button', text: 'Mute or unmute that stem' },
      { mod: 'alt', gesture: 'Click', on: 'stem button', text: 'Solo that stem' },
      { gesture: 'Right-click', on: 'pad', text: 'Hotcue type, colour, rename, delete' },
    ],
  },
  {
    title: 'Track list',
    rows: [
      { gesture: 'Click', text: 'Select a track' },
      { mod: 'shift', gesture: 'Click', text: 'Select a range' },
      { mod: 'mod', gesture: 'Click', text: 'Add to or remove from the selection' },
      { gesture: 'Double-click', on: 'cell', text: 'Edit it in place' },
      { gesture: 'Right-click', text: 'Track menu. On the header: choose columns' },
      { gesture: 'Drag', on: 'rows', text: 'Onto a playlist or export to add, onto the deck to load, within a playlist to reorder' },
      { mod: 'alt', gesture: 'Drag', on: 'rows', text: 'Move them out of this playlist into another' },
    ],
  },
  {
    title: 'Sidebar',
    rows: [
      { gesture: 'Right-click', text: 'New playlist or folder, rename, delete' },
      { gesture: 'Drag', on: 'playlist', text: 'Rearrange playlists and folders, or drop onto an export to add it' },
    ],
  },
]

// ---- the keyboard drawn -----------------------------------------------------

export interface KeyDef {
  code: string
  legend: string
  /** The legend when keys are too small for `legend`. */
  short?: string
  /** Width in key units. */
  w?: number
  /** A modifier key: holding or clicking it shows its layer. */
  role?: Exclude<Layer, 'base'>
}

const key = (code: string, legend: string, w = 1, role?: KeyDef['role'], short?: string): KeyDef => ({
  code,
  legend,
  w,
  role,
  short,
})
const chars = (s: string, prefix: string) => [...s].map((c) => key(prefix + c, c))

/** The main block, a row per list. Every row is 14.5 units wide. */
export const KEY_ROWS: KeyDef[][] = (() => {
  const fn = [key('Escape', 'esc', 1.5)]
  for (let i = 1; i <= 12; i++) fn.push(key(`F${i}`, `F${i}`))
  fn.push(key('Power', ''))
  const shift = IS_MAC ? 'shift ⇧' : 'Shift'
  const shiftShort = IS_MAC ? '⇧' : 'Shift'
  const bottom = IS_MAC
    ? [
        key('Fn', 'fn'),
        key('ControlLeft', 'control', 1.25, undefined, 'ctrl'),
        key('AltLeft', 'option ⌥', 1.25, 'alt', '⌥'),
        key('MetaLeft', 'command ⌘', 1.5, 'mod', '⌘'),
        key('Space', 'space', 6.75),
        key('MetaRight', 'command ⌘', 1.5, 'mod', '⌘'),
        key('AltRight', 'option ⌥', 1.25, 'alt', '⌥'),
      ]
    : [
        key('ControlLeft', 'Ctrl', 1.25, 'mod'),
        key('MetaLeft', 'Win', 1.25),
        key('AltLeft', 'Alt', 1.25, 'alt'),
        key('Space', 'space', 6.75),
        key('AltRight', 'Alt', 1.25, 'alt'),
        key('MetaRight', 'Win', 1.25),
        key('ControlRight', 'Ctrl', 1.5, 'mod'),
      ]
  return [
    fn,
    [key('Backquote', '`'), ...chars('1234567890', 'Digit'), key('Minus', '-'), key('Equal', '='),
      key('Backspace', IS_MAC ? 'delete' : 'Backspace', 1.5, undefined, IS_MAC ? 'del' : 'Bksp')],
    [key('Tab', 'tab', 1.5), ...chars('QWERTYUIOP', 'Key'), key('BracketLeft', '['), key('BracketRight', ']'), key('Backslash', '\\')],
    [key('CapsLock', 'caps', 1.75), ...chars('ASDFGHJKL', 'Key'), key('Semicolon', ';'), key('Quote', "'"),
      key('Enter', IS_MAC ? 'return' : 'Enter', 1.75)],
    [key('ShiftLeft', shift, 2.25, 'shift', shiftShort), ...chars('ZXCVBNM', 'Key'), key('Comma', ','), key('Period', '.'),
      key('Slash', '/ ?'), key('ShiftRight', shift, 2.25, 'shift', shiftShort)],
    bottom,
  ]
})()

/** The inverted-T arrow cluster, 3 × 2, `null` for the gaps beside ↑. */
export const ARROW_KEYS: (KeyDef | null)[] = [
  null, key('ArrowUp', '↑'), null,
  key('ArrowLeft', '←'), key('ArrowDown', '↓'), key('ArrowRight', '→'),
]

/** The space between the main block and the arrows, in key units. */
export const ARROW_GAP = 0.36
/** The whole map's width in key units. */
export const KEYBOARD_UNITS = 14.5 + ARROW_GAP + 3

export const KEYS_BY_CODE: Record<string, KeyDef> = Object.fromEntries(
  [...KEY_ROWS.flat(), ...ARROW_KEYS.filter((k): k is KeyDef => !!k)].map((k) => [k.code, k]),
)

/** The modifier layer a physical key holds, if it is one. */
export function modifierLayer(code: string): Exclude<Layer, 'base'> | null {
  if (code === 'ShiftLeft' || code === 'ShiftRight') return 'shift'
  if (code === 'AltLeft' || code === 'AltRight') return 'alt'
  const mod = IS_MAC ? ['MetaLeft', 'MetaRight'] : ['ControlLeft', 'ControlRight']
  return mod.includes(code) ? 'mod' : null
}
