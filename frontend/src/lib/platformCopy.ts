/**
 * Wording that names the loaded platform.
 *
 * The adapter supplies structured facts, never finished sentences, and these
 * compose them. That keeps the copy (and any future translation) in the UI while
 * staying genuinely specific — the pre-save warning is a real, per-platform
 * hazard and must not be watered down into something vague.
 */
import type { Capabilities, PlatformOption, SaveCapabilities } from '../api'

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

/**
 * Why this library is read-only, or null when it is editable.
 *
 * The two causes need genuinely different wording: a missing feature is a gap
 * the user should expect Konduktor to close, whereas a cloud-synced library is
 * a deliberate, permanent refusal that protects their other machines — and a
 * user told only "read-only" would reasonably file the second as a bug.
 */
export function readOnlyNotice(caps: Capabilities): string | null {
  if (caps.writable) return null
  switch (caps.readonly_cause) {
    case 'cloud_synced':
      return `This ${caps.save.app_name} library is synced with ${caps.save.app_name} Cloud, so Konduktor will not write to it — an edit it did not make could break syncing on your other devices.`
    case 'platform_incomplete':
    default:
      return `Konduktor can read ${caps.save.app_name} libraries but cannot save changes to them yet.`
  }
}

/** Compact form for a badge or a disabled control's tooltip. */
export function readOnlyShort(caps: Capabilities): string | null {
  if (caps.writable) return null
  return caps.readonly_cause === 'cloud_synced' ? 'Read-only · cloud-synced' : 'Read-only'
}

// ---- picker: choosing a platform, before any library is open ----------------
//
// These take a PlatformOption rather than Capabilities: the picker runs before
// an adapter exists, so there are no capabilities to read yet.

/**
 * What this platform has to offer right now.
 *
 * "Nothing found" means two genuinely different things, and which one it is
 * decides what the user should do about it: a fixed-location platform is not
 * installed, or keeps its library somewhere unusual, and wants browsing —
 * whereas a removable one simply has nothing plugged in, and wants a USB port.
 * Someone told "not found" about OneLibrary would go hunting for a missing app.
 */
export function platformAvailability(p: PlatformOption): string {
  if (p.found > 1) return p.removable ? `${p.found} drives connected` : `${p.found} found`
  if (p.found === 1) return p.removable ? '1 drive connected' : `${p.library_label} found`
  return p.removable ? 'No drive connected' : `No ${p.library_label} in the usual place`
}

/** What the manual browser is asking the user to pick out. */
export function browsePrompt(p: PlatformOption): string {
  return p.selects === 'directory' ? 'Find the drive to open.' : `Choose a ${p.library_label} to open.`
}
