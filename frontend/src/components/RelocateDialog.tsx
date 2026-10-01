import { createPortal } from 'react-dom'
import { useEffect, useState } from 'react'
import { api, type PathMapping, type RelocationVolume } from '../api'

interface Props {
  volumes: RelocationVolume[]
  /** Called once the backend has the answer (Apply or Not now). */
  onAnswered: (applied: { mappings: number; tracks: number } | null) => void
  /** "Not now", then the manual Path Remapping dialog. */
  onOpenManual: () => void
  onError: (msg: string) => void
}

const n = (v: number) => v.toLocaleString()

/**
 * The open-time missing-files check: shown when a stored volume resolves NONE
 * of its tracks, after the backend has searched for where they went. It always
 * asks — nothing is applied until Apply — and what it applies lasts for this
 * session only; the library and prefs are never touched, and a reopen asks
 * again. A volume with one clear location is ticked; two likely locations (a
 * drive and its backup clone) are a choice with nothing preselected, because a
 * wrong guess would play and tag files on the wrong drive.
 */
export function RelocateDialog({ volumes, onAnswered, onOpenManual, onError }: Props) {
  // Per volume root: the chosen candidate's `to`, or null to leave it missing.
  const [choice, setChoice] = useState<Record<string, string | null>>(() =>
    Object.fromEntries(
      volumes.map((v) => [v.root, v.status === 'found' ? v.candidates[0].to : null]),
    ),
  )
  const [busy, setBusy] = useState(false)

  const chosen: PathMapping[] = volumes.flatMap((v) => {
    const to = choice[v.root]
    const c = v.candidates.find((c) => c.to === to)
    return c ? [{ from: c.from, to: c.to }] : []
  })
  const anyFound = volumes.some((v) => v.candidates.length > 0)
  const anyMissing = volumes.some((v) => v.status === 'not_found')

  const answer = async (mappings: PathMapping[], then?: () => void) => {
    setBusy(true)
    try {
      const res = await api.answerRelocation(mappings)
      onAnswered(mappings.length ? res : null)
      then?.()
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Failed to apply the remapping')
      setBusy(false)
    }
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && !busy && answer([])
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  const set = (root: string, to: string | null) => setChoice((c) => ({ ...c, [root]: to }))

  return createPortal(
    // No close on a backdrop click: dismissing lasts until the library is
    // reopened, which is too much for a stray click to decide.
    <div
      aria-modal="true"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-[3px] p-6"
    >
      <div className="flex max-h-[85vh] w-full max-w-xl flex-col overflow-hidden glass-overlay">
        <div className="border-b border-line px-5 py-4">
          <div className="text-[15px] font-semibold tracking-tight">Tracks not found</div>
          <div className="text-xs text-muted">
            None of the tracks on {volumes.length === 1 ? 'this drive are' : 'these drives are'} where
            the collection says.
            {anyFound ? ' Konduktor looked for them on this computer:' : ' They were not found on this computer.'}
          </div>
        </div>

        <div className="min-h-0 flex-1 space-y-2 overflow-y-auto px-5 py-4">
          {volumes.map((v) => (
            <VolumeRow key={v.root} volume={v} choice={choice[v.root]} onChoose={(to) => set(v.root, to)} />
          ))}
          <p className="pt-1 text-[11px] leading-relaxed text-faint">
            Remapping applies for this session only. Nothing is written to your collection, and
            you'll be asked again the next time you open it.
          </p>
        </div>

        <div className="flex items-center gap-2 border-t border-line px-5 py-3">
          {anyMissing && (
            <button
              onClick={() => answer([], onOpenManual)}
              disabled={busy}
              className="mr-auto rounded-md px-2 py-1.5 text-xs text-muted hover:bg-ink-800 hover:text-text disabled:opacity-50"
            >
              Map by hand…
            </button>
          )}
          <div className="ml-auto flex items-center gap-2">
            <button
              onClick={() => answer([])}
              disabled={busy}
              className="rounded-md px-3 py-1.5 text-sm text-muted hover:bg-ink-800 hover:text-text disabled:opacity-50"
            >
              Not now
            </button>
            {anyFound && (
              <button
                onClick={() => answer(chosen)}
                disabled={busy || chosen.length === 0}
                className="rounded-full btn-primary px-4 py-1.5 text-sm font-semibold disabled:opacity-50"
              >
                {busy ? 'Applying…' : 'Apply'}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}

function VolumeRow({
  volume: v,
  choice,
  onChoose,
}: {
  volume: RelocationVolume
  choice: string | null | undefined
  onChoose: (to: string | null) => void
}) {
  const heading = (
    <span className="min-w-0 flex-1">
      <span className="block truncate text-sm text-text">
        {v.label || v.root}
        {v.root !== v.label && <span className="ml-1.5 text-xs text-faint">{v.root}</span>}
      </span>
      <span className="block text-xs text-faint">
        {n(v.total)} track{v.total === 1 ? '' : 's'}
      </span>
    </span>
  )

  if (v.status === 'not_found') {
    return (
      <div className="flex items-start gap-3 rounded-md border border-line px-3 py-2.5">
        {heading}
        <span className="shrink-0 pt-0.5 text-xs text-gold">Not connected</span>
      </div>
    )
  }

  if (v.status === 'found') {
    const c = v.candidates[0]
    return (
      <label
        className={`flex cursor-pointer items-start gap-3 rounded-md border border-line px-3 py-2.5 ${
          choice ? 'is-selected' : 'hover:bg-ink-850'
        }`}
      >
        <input
          type="checkbox"
          checked={!!choice}
          onChange={(e) => onChoose(e.target.checked ? c.to : null)}
          className="mt-1 accent-accent"
        />
        {heading}
        <Target to={c.to} found={c.found} total={v.total} />
      </label>
    )
  }

  // Ambiguous: a choice, with nothing preselected.
  return (
    <div className="rounded-md border border-line px-3 py-2.5">
      <div className="flex items-start gap-3">
        {heading}
        <span className="shrink-0 pt-0.5 text-xs text-gold">Found in {v.candidates.length} places</span>
      </div>
      <div className="mt-2 space-y-1">
        {v.candidates.map((c) => (
          <label
            key={c.to}
            className={`flex cursor-pointer items-center gap-3 rounded-md px-2 py-1.5 ${
              choice === c.to ? 'is-selected' : 'hover:bg-ink-850'
            }`}
          >
            <input
              type="radio"
              name={`relocate-${v.root}`}
              checked={choice === c.to}
              onChange={() => onChoose(c.to)}
              className="accent-accent"
            />
            <Target to={c.to} found={c.found} total={v.total} wide />
          </label>
        ))}
        <label
          className={`flex cursor-pointer items-center gap-3 rounded-md px-2 py-1.5 ${
            !choice ? 'is-selected' : 'hover:bg-ink-850'
          }`}
        >
          <input
            type="radio"
            name={`relocate-${v.root}`}
            checked={!choice}
            onChange={() => onChoose(null)}
            className="accent-accent"
          />
          <span className="text-sm text-muted">Leave missing</span>
        </label>
      </div>
    </div>
  )
}

function Target({ to, found, total, wide }: { to: string; found: number; total: number; wide?: boolean }) {
  return (
    <span className={`min-w-0 text-right ${wide ? 'flex flex-1 items-baseline justify-between gap-3' : 'max-w-[55%]'}`}>
      <span className="block truncate text-sm text-text" title={to}>
        <span className="text-faint">→</span> {to}
      </span>
      <span className={`block text-xs tabular-nums ${found === total ? 'text-mint' : 'text-faint'}`}>
        {n(found)} of {n(total)} found
      </span>
    </span>
  )
}
