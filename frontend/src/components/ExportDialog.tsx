import { createPortal } from 'react-dom'
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type ExportSet } from '../api'
import { localLibraryProblem } from '../lib/platformCopy'
import { FolderPicker } from './FolderPicker'

/**
 * Create or edit an export: its name, its target platforms and where it lands.
 *
 * Several platforms can be ticked. They share the destination and ONE copy of
 * the audio (under `Contents/`), each writing its own library beside it — so a
 * stick can carry a Traktor collection and a OneLibrary library at once.
 *
 * All three are settled up front, and the destination especially is NOT a
 * detail deferred to export time. A Traktor `<LOCATION>` is a volume name plus
 * a volume-relative path — both derived from where the export is written — so
 * the library file genuinely cannot be built until the destination is known.
 *
 * Or the set goes to THIS COMPUTER: pushed straight into the Traktor collection
 * and/or rekordbox library its DJ apps use (`local_push.py`), merging beside
 * the user's own tracks or replacing the library. No destination is asked —
 * which library each app uses, and the shared audio folder, are this
 * computer's settings. The kind is chosen once, at creation: a push leaves
 * records of what it put where, which a change of kind would orphan.
 */

interface Props {
  /** The set being edited, or null to create a new one. */
  editing: ExportSet | null
  onClose: () => void
  onSaved: (set: ExportSet, created: boolean) => void
  onError: (msg: string) => void
}

export function ExportDialog({ editing, onClose, onSaved, onError }: Props) {
  const [name, setName] = useState(editing?.name ?? '')
  const [chosen, setChosen] = useState<string[]>(editing?.targets ?? ['traktor'])
  const [destination, setDestination] = useState(editing?.destination ?? '')
  const [kind, setKind] = useState<ExportSet['kind']>(editing?.kind ?? 'folder')
  const [mode, setMode] = useState<ExportSet['mode']>(editing?.mode ?? 'merge')
  const computer = kind === 'computer'
  // A push's targets: this computer's DJ apps, each with why it cannot be used.
  const locals = useQuery({
    queryKey: ['local-libraries'],
    queryFn: api.localLibraries,
    enabled: computer,
  })
  const [browsing, setBrowsing] = useState(false)
  const [busy, setBusy] = useState(false)

  // Every platform, with `installed` standing for "can be an export target".
  // The unsupported ones are shown DISABLED with a reason rather than hidden:
  // an absent option reads as a missing feature, a disabled one as a roadmap.
  const targets = useQuery({ queryKey: ['export-targets'], queryFn: api.exportTargets })

  // Another set writing to the same folder would have its output wiped, because
  // an export clears its destination before writing. Caught while the folder is
  // being chosen — at run time a warning would already be too late.
  const conflict = useQuery({
    queryKey: ['export-conflict', editing?.id ?? null],
    queryFn: () => api.exportContents(editing!.id),
    enabled: !!editing,
  }).data?.destination_conflict

  // Some targets are only found at a drive's root (a CDJ looks for `PIONEER/`
  // there and nowhere else). Warned, not refused: the other targets are fine in
  // any folder. Checked against the same polled drive list the sidebar uses.
  const drives = useQuery({ queryKey: ['drives'], queryFn: api.drives })
  const trim = (p: string) => p.replace(/[\\/]+$/, '') || p
  const atDriveRoot =
    destination.trim() !== '' &&
    (drives.data ?? []).some((d) => trim(d.path) === trim(destination.trim()))
  const needsRoot = (targets.data ?? []).filter(
    (p) => p.drive_root && chosen.includes(p.platform),
  )
  // A computer-only library ticked beside a stick format: harmless, but it does
  // nothing on the stick, which is easy to mistake for a broken export.
  const computerOnly = (targets.data ?? []).filter(
    (p) => p.computer_only && chosen.includes(p.platform),
  )

  const toggle = (platform: string, on: boolean) =>
    setChosen((prev) =>
      on ? [...prev.filter((t) => t !== platform), platform] : prev.filter((t) => t !== platform),
    )

  const pushable = new Set(
    (locals.data?.libraries ?? []).filter((l) => !l.blocked).map((l) => l.platform),
  )
  const valid = computer
    ? name.trim() !== '' && chosen.length > 0 && chosen.every((t) => pushable.has(t))
    : name.trim() !== '' && destination.trim() !== '' && chosen.length > 0

  const chooseKind = (next: ExportSet['kind']) => {
    setKind(next)
    // The two kinds offer different platforms: start from the obvious one.
    setChosen(['traktor'])
  }

  const save = async () => {
    setBusy(true)
    try {
      const saved = editing
        ? await api.updateExport(editing.id, computer
            ? { name: name.trim(), targets: chosen, mode }
            : { name: name.trim(), targets: chosen, destination: destination.trim() })
        : await api.createExport(computer
            ? { name: name.trim(), targets: chosen, destination: '', kind, mode }
            : { name: name.trim(), targets: chosen, destination: destination.trim() })
      onSaved(saved, !editing)
    } catch (e) {
      onError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return createPortal(
    <>
      {browsing && (
        <FolderPicker
          value={destination}
          onChange={setDestination}
          onClose={() => setBrowsing(false)}
        />
      )}
      <div aria-modal="true" className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-[3px] p-4">
        <div className="w-full max-w-lg glass-overlay">
          <div className="border-b border-line px-5 py-3">
            <h2 className="text-sm font-semibold text-text">
              {editing ? `Edit “${editing.name}”` : computer ? 'New push to this computer' : 'New export'}
            </h2>
          </div>

          <div className="space-y-4 px-5 py-4 text-sm">
            <div>
              <span className="mb-1 block text-xs text-muted">Name</span>
              <input
                autoFocus
                value={name}
                placeholder="Ibiza — July"
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && valid && !busy) save()
                }}
                className="w-full rounded-md well px-2 py-1.5 text-sm text-text outline-none focus:ring-1 focus:ring-accent"
              />
            </div>

            {!editing && (
              <div>
                <span className="mb-1 block text-xs text-muted">Where</span>
                <div className="grid grid-cols-2 gap-2">
                  {([
                    ['folder', 'A folder or drive', 'Writes a library and a copy of the audio there — a USB stick for the gig.'],
                    ['computer', 'This computer', 'Pushes into the Traktor or rekordbox you DJ with here.'],
                  ] as const).map(([value, title, detail]) => (
                    <button
                      key={value}
                      onClick={() => chooseKind(value)}
                      aria-pressed={kind === value}
                      className={`rounded-lg px-3 py-2 text-left ${
                        kind === value ? 'is-selected well' : 'btn-glass'
                      }`}
                    >
                      <span className="block text-sm text-text">{title}</span>
                      <span className="mt-0.5 block text-[11px] leading-snug text-faint">{detail}</span>
                    </button>
                  ))}
                </div>
              </div>
            )}

            {computer ? (
              <>
                <div>
                  <span className="mb-1 block text-xs text-muted">Into</span>
                  <div className="space-y-1">
                    {(locals.data?.libraries ?? []).map((lib) => {
                      const problem = localLibraryProblem(lib)
                      return (
                        <label
                          key={lib.platform}
                          className={`flex items-start gap-2 rounded-md px-2 py-1.5 ${
                            problem ? 'cursor-not-allowed' : 'cursor-pointer hover:bg-ink-850'
                          }`}
                        >
                          <input
                            type="checkbox"
                            disabled={!!problem}
                            checked={chosen.includes(lib.platform)}
                            onChange={(e) => toggle(lib.platform, e.target.checked)}
                            className="mt-0.5 accent-accent"
                          />
                          <span className="min-w-0 flex-1">
                            <span className={problem ? 'text-faint' : 'text-text'}>{lib.name}</span>
                            <span
                              title={lib.path ?? undefined}
                              className="block truncate text-[11px] text-faint"
                            >
                              {problem ?? lib.path}
                            </span>
                          </span>
                        </label>
                      )
                    })}
                  </div>
                  <div className="mt-1 text-[11px] text-faint">
                    The audio is copied to{' '}
                    <span className="font-mono">{locals.data?.audio_root ?? '…'}</span>, shared by
                    every push. Change either in Settings → Local libraries.
                  </div>
                </div>

                <div>
                  <span className="mb-1 block text-xs text-muted">How</span>
                  <div className="space-y-1">
                    {([
                      ['merge', 'Merge', 'Adds and updates this set’s tracks, in a folder named after it. Your own tracks and playlists are never touched.'],
                      ['overwrite', 'Overwrite', 'Replaces the WHOLE library with this set. Everything else in it — your own tracks, playlists and other pushes — is removed.'],
                    ] as const).map(([value, title, detail]) => (
                      <label key={value} className="flex cursor-pointer items-start gap-2 rounded-md px-2 py-1.5 hover:bg-ink-850">
                        <input
                          type="radio"
                          name="push-mode"
                          checked={mode === value}
                          onChange={() => setMode(value)}
                          className="mt-0.5 accent-accent"
                        />
                        <span>
                          <span className={value === 'overwrite' ? 'text-pink' : 'text-text'}>{title}</span>
                          <span className="block text-[11px] leading-snug text-faint">{detail}</span>
                        </span>
                      </label>
                    ))}
                  </div>
                </div>
              </>
            ) : (
            <>
            <div>
              <span className="mb-1 block text-xs text-muted">For</span>
              <div className="space-y-1">
                {(targets.data ?? []).map((p) => (
                  <label
                    key={p.platform}
                    className={`flex items-center gap-2 rounded-md px-2 py-1.5 ${
                      p.installed ? 'cursor-pointer hover:bg-ink-850' : 'cursor-not-allowed'
                    }`}
                  >
                    <input
                      type="checkbox"
                      disabled={!p.installed}
                      checked={chosen.includes(p.platform)}
                      onChange={(e) => toggle(p.platform, e.target.checked)}
                      className="accent-accent"
                    />
                    <span className={p.installed ? 'text-text' : 'text-faint'}>{p.name}</span>
                    {!p.installed && (
                      <span className="ml-auto text-[11px] text-faint">
                        Konduktor cannot write a {p.name} library yet
                      </span>
                    )}
                  </label>
                ))}
              </div>
              {computerOnly.length > 0 && needsRoot.length > 0 && (
                <div className="mt-2 rounded well px-3 py-2 text-xs text-gold">
                  {computerOnly.map((p) => p.name).join(' and ')} is only read on a computer, never
                  from a USB drive, so it adds nothing to {needsRoot.map((p) => p.name).join(' and ')}.
                  It replaces a collection on this computer and only works on this machine.
                </div>
              )}
            </div>

            <div>
              <span className="mb-1 block text-xs text-muted">Export to</span>
              <div className="flex items-center gap-2">
                <span
                  title={destination}
                  dir="rtl"
                  className="min-w-0 flex-1 truncate rounded-md well px-2 py-1.5 font-mono text-xs text-text"
                >
                  {destination || 'Choose a folder…'}
                </span>
                <button
                  onClick={() => setBrowsing(true)}
                  className="shrink-0 btn-glass rounded-full px-3 py-1.5 text-sm text-muted hover:text-text"
                >
                  Browse…
                </button>
              </div>
              <div className="mt-1 text-[11px] text-faint">
                The folder gets a library for each platform, plus one copy of every track under{' '}
                <span className="font-mono">Contents/</span>, in your own folder structure.
              </div>
              {needsRoot.length > 0 && destination.trim() !== '' && !atDriveRoot && (
                <div className="mt-2 rounded well px-3 py-2 text-xs text-gold">
                  {needsRoot.map((p) => p.name).join(' and ')} is only found at the top of a
                  drive. Choose the drive itself rather than a folder on it, or players won’t see
                  this export.
                </div>
              )}
              {conflict && (
                <div className="mt-2 rounded well px-3 py-2 text-xs text-gold">
                  “{conflict}” already exports here. Exporting replaces whatever the other one
                  wrote.
                </div>
              )}
            </div>
            </>
            )}
          </div>

          <div className="flex justify-end gap-2 border-t border-line px-5 py-3">
            <button
              onClick={onClose}
              className="btn-glass rounded-full px-3 py-1.5 text-sm text-muted hover:text-text"
            >
              Cancel
            </button>
            <button
              disabled={!valid || busy}
              onClick={save}
              className="rounded-full btn-primary px-3 py-1.5 text-sm font-semibold disabled:opacity-40"
            >
              {busy ? 'Saving…' : editing ? 'Save' : 'Create'}
            </button>
          </div>
        </div>
      </div>
    </>,
    document.body,
  )
}
