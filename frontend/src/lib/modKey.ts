// The platform's command modifier: Cmd on macOS, Ctrl elsewhere. For mouse
// gestures it is exactly that key — on macOS, Ctrl+click is a right-click, so
// accepting either (as the keyboard shortcuts do) would start a drag under a
// context menu.

export const IS_MAC = /Mac|iPhone|iPad/.test(navigator.userAgent)

/** Whether the command modifier is held. */
export const hasMod = (e: { metaKey: boolean; ctrlKey: boolean }) => (IS_MAC ? e.metaKey : e.ctrlKey)

/** Its `KeyboardEvent.key`, for following it being pressed mid-gesture. */
export const MOD_KEY = IS_MAC ? 'Meta' : 'Control'

/** Its name in a tooltip ("Cmd-click"). */
export const MOD_NAME = IS_MAC ? 'Cmd' : 'Ctrl'
