import { useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'
import { useCaps } from '../lib/capabilities'
import { overwriteWarning, readOnlyNotice, saveLabel } from '../lib/platformCopy'
import { Icon } from '../lib/icons'
import { askConfirm } from '../lib/confirm'

interface Props {
  onError: (msg: string) => void
  /** Rendered to the right of the save button (the settings gear). */
  trailing?: ReactNode
  /** After a discard, so the owner can refresh what it holds outside queries. */
  onDiscarded?: () => void
}

// Bottom-of-sidebar save control. Shows unsaved-changes state and writes to the
// NML; every save is recorded in the collection's version history.
export function SaveBar({ onError, trailing, onDiscarded }: Props) {
  const qc = useQueryClient()
  const [justSaved, setJustSaved] = useState<string | null>(null)
  const { data: state } = useQuery({ queryKey: ['state'], queryFn: api.state })

  const save = useMutation({
    mutationFn: api.save,
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ['state'] })
      qc.invalidateQueries({ queryKey: ['history'] })
      if (res.saved && res.commit) {
        setJustSaved(`Saved · version ${res.commit.slice(0, 8)}`)
        setTimeout(() => setJustSaved(null), 6000)
      }
    },
    onError: (e: Error) => onError(e.message),
  })

  // The way back that did not exist: every edit since the last save goes, and
  // the library re-reads itself from disk. Everything cached is refetched.
  const discard = useMutation({
    mutationFn: api.discard,
    onSuccess: () => {
      qc.invalidateQueries()
      onDiscarded?.()
    },
    onError: (e: Error) => onError(e.message),
  })
  const confirmDiscard = async () => {
    const ok = await askConfirm({
      title: 'Discard unsaved changes?',
      body: 'Every change since the last save is thrown away and the library is re-read from disk. This cannot be undone.',
      confirmLabel: 'Discard changes',
    })
    if (ok) discard.mutate()
  }

  const caps = useCaps()
  const warning = overwriteWarning(caps.save)
  const dirty = state?.dirty ?? false
  const pending = state?.pending_stems
  const readOnly = readOnlyNotice(caps)

  // A read-only library has no save to offer, and saying so plainly is the whole
  // point: a greyed-out button with no explanation reads as a bug.
  if (readOnly) {
    return (
      <div className="flex items-stretch gap-2 border-t border-line px-3 py-3">
        <div className="min-w-0 flex-1 rounded-xl bg-ink-800 px-2.5 py-2 text-[11px] leading-snug text-faint">
          <span className="font-medium text-text">Read-only</span>
          <div className="mt-1">{readOnly}</div>
        </div>
        {trailing}
      </div>
    )
  }

  return (
    <div className="border-t border-line px-3 py-3">
      {dirty && warning && (
        <div className="mb-2 flex gap-1.5 rounded-xl bg-gold/10 px-2.5 py-1.5 text-[11px] leading-snug text-gold">
          <Icon name="warning" size={13} className="mt-px shrink-0" />
          <span>{warning}</span>
        </div>
      )}
      {/* Decided: the consequence is shown where Save is pressed, not only in
          the Stems panel — Save deletes what a Replace conversion parked. */}
      {pending && pending.parked > 0 && (
        <div className="mb-2 flex gap-1.5 rounded-xl bg-gold/10 px-2.5 py-1.5 text-[11px] leading-snug text-gold">
          <Icon name="warning" size={13} className="mt-px shrink-0" />
          <span>
            Saving deletes {pending.parked} original file{pending.parked === 1 ? '' : 's'} (
            {pending.bytes >= 1e9 ? `${(pending.bytes / 1e9).toFixed(1)} GB` : `${Math.round(pending.bytes / 1e6)} MB`}
            ) replaced by stem files. Discard to keep them.
          </span>
        </div>
      )}
      <div className="flex items-stretch gap-2">
      <button
        disabled={!dirty || save.isPending}
        onClick={() => save.mutate()}
        className={`flex h-10 min-w-0 flex-1 items-center justify-center gap-2 rounded-full px-3 text-sm font-semibold transition-colors ${
          dirty ? 'btn-primary' : 'cursor-not-allowed bg-ink-800 font-medium text-faint'
        }`}
      >
        {save.isPending ? (
          'Saving…'
        ) : dirty ? (
          <>
            <span className="h-1.5 w-1.5 rounded-full bg-well" />
            {saveLabel(caps.save)}
          </>
        ) : (
          'No unsaved changes'
        )}
      </button>
      {trailing}
      </div>
      {dirty && !save.isPending && (
        <div className="mt-1.5 text-center">
          <button
            onClick={() => void confirmDiscard()}
            disabled={discard.isPending}
            className="text-[11px] text-faint hover:text-pink disabled:opacity-50"
          >
            {discard.isPending ? 'Discarding…' : 'Discard changes…'}
          </button>
        </div>
      )}
      {justSaved && (
        <div className="mt-2 text-center text-[11px] text-mint">{justSaved}</div>
      )}
    </div>
  )
}
