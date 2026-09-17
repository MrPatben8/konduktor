/**
 * Wording that names the loaded platform.
 *
 * The adapter supplies structured facts, never finished sentences, and these
 * compose them. That keeps the copy (and any future translation) in the UI while
 * staying genuinely specific — the pre-save warning is a real, per-platform
 * hazard and must not be watered down into something vague.
 */
import type { SaveCapabilities } from '../api'

export function saveLabel(save: SaveCapabilities): string {
  return `Save to ${save.app_name}`
}

/** Shown while there are unsaved changes. Null when the platform has no hazard. */
export function overwriteWarning(save: SaveCapabilities): string | null {
  switch (save.overwrite_risk) {
    case 'none':
      return null
    case 'on_exit':
      return `Close ${save.app_name} before saving — it overwrites ${save.library_label} on exit.`
    case 'while_running':
      return `${save.app_name} is open — it may overwrite or re-sync ${save.library_label} while running. Close it before saving.`
  }
}

export function restoreWarning(save: SaveCapabilities): string | null {
  switch (save.overwrite_risk) {
    case 'none':
      return null
    case 'on_exit':
      return `Close ${save.app_name} before restoring — it overwrites ${save.library_label} on exit.`
    case 'while_running':
      return `${save.app_name} is open — close it before restoring, or it may overwrite ${save.library_label}.`
  }
}

/** Trailing hint after an in-memory edit. */
export function writeHint(save: SaveCapabilities): string {
  return `Save to ${save.app_name} to write it to disk`
}
