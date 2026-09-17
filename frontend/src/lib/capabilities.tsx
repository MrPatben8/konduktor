import { createContext, useContext } from 'react'

import type { Capabilities } from '../api'

/**
 * What the loaded library can persist.
 *
 * Components read this instead of branching on the platform, so a control is
 * never offered for an edit that would silently vanish on save. There is no
 * default value on purpose: the app does not render until capabilities have
 * resolved, which removes any chance of a permissive first frame offering an
 * edit the adapter would reject.
 *
 * Containers call `useCaps()`; presentational leaves take plain values as props
 * so they stay trivially testable.
 */
export const CapabilitiesContext = createContext<Capabilities | null>(null)

export function useCaps(): Capabilities {
  const caps = useContext(CapabilitiesContext)
  if (!caps) {
    throw new Error('useCaps() used outside <CapabilitiesContext.Provider>')
  }
  return caps
}

/** Label for a bank slot: "1".."8" or "A".."H", per the platform's convention. */
export function slotLabeller(caps: Capabilities): (slot: number) => string {
  return caps.cues.slot_labels === 'letter'
    ? (slot) => String.fromCharCode(65 + slot)
    : (slot) => String(slot + 1)
}
