import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'
import { useCaps } from '../lib/capabilities'
import { overwriteWarning, saveLabel } from '../lib/platformCopy'

interface Props {
  onError: (msg: string) => void
}

// Bottom-of-sidebar save control. Shows unsaved-changes state and writes to the
// NML; every save is recorded in the collection's version history.
export function SaveBar({ onError }: Props) {
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

  return (
    <div className="border-t border-line bg-ink-900 px-3 py-3">
      {dirty && warning && (
        <div className="mb-2 rounded-md bg-gold/10 px-2 py-1.5 text-[11px] leading-snug text-gold">
          ⚠ {warning}
        </div>
      )}
      <button
        disabled={!dirty || save.isPending}
        onClick={() => save.mutate()}
        className={`flex w-full items-center justify-center gap-2 rounded-md px-3 py-2 text-sm font-medium transition-colors ${
          dirty
            ? 'bg-accent text-ink-950 hover:brightness-110'
            : 'cursor-not-allowed bg-ink-800 text-faint'
        }`}
      >
        {save.isPending ? (
          'Saving…'
        ) : dirty ? (
          <>
            <span className="h-1.5 w-1.5 rounded-full bg-ink-950/60" />
            {saveLabel(caps.save)}
          </>
        ) : (
          'No unsaved changes'
        )}
      </button>
      {justSaved && (
        <div className="mt-2 text-center text-[11px] text-mint">{justSaved}</div>
      )}
    </div>
  )
}
