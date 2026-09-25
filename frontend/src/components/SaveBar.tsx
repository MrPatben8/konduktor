import { useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'
import { useCaps } from '../lib/capabilities'
import { overwriteWarning, readOnlyNotice, saveLabel } from '../lib/platformCopy'
import { Icon } from '../lib/icons'

interface Props {
  onError: (msg: string) => void
  /** Rendered to the right of the save button (the settings gear). */
  trailing?: ReactNode
}

// Bottom-of-sidebar save control. Shows unsaved-changes state and writes to the
// NML; every save is recorded in the collection's version history.
export function SaveBar({ onError, trailing }: Props) {
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

  const caps = useCaps()
  const warning = overwriteWarning(caps.save)
  const dirty = state?.dirty ?? false
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
      {justSaved && (
        <div className="mt-2 text-center text-[11px] text-mint">{justSaved}</div>
      )}
    </div>
  )
}
