import { api } from '../api'
import { askConfirm } from './confirm'

/**
 * Ask before an action that throws away unsaved edits — opening a different
 * library replaces the adapter that holds them. Resolves true when there is
 * nothing to lose or the user agreed.
 *
 * Reads the state fresh rather than from a cached query: the two places that
 * switch library (the sidebar header and the status bar) must not disagree, and
 * the status bar's button used to skip the question entirely.
 */
export async function confirmDiscardUnsaved(): Promise<boolean> {
  const dirty = await api
    .state()
    .then((s) => s.dirty)
    .catch(() => false) // no library loaded: nothing to lose
  if (!dirty) return true
  return askConfirm({
    title: 'Discard unsaved changes?',
    body: 'You have unsaved changes. Opening a different library will discard them.',
    confirmLabel: 'Discard and switch',
  })
}
