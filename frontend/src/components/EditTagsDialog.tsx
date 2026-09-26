import { createPortal } from 'react-dom'
import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type Track } from '../api'
import { formatBpm, formatDuration } from '../lib/format'
import { useCaps } from '../lib/capabilities'
import { RatingStars } from './RatingStars'
import { writeHint } from '../lib/platformCopy'

interface Props {
  track: Track
  onClose: () => void
  onApplied: (msg: string) => void
  onError: (msg: string) => void
}

// Editable free-text fields (mirrors the backend's safe set). Rating is separate.
const TEXT_FIELDS: { key: keyof Track; label: string; textarea?: boolean }[] = [
  { key: 'title', label: 'Title' },
  { key: 'artist', label: 'Artist' },
  { key: 'album', label: 'Album' },
  { key: 'genre', label: 'Genre' },
  { key: 'label', label: 'Label' },
  { key: 'remixer', label: 'Remixer' },
  { key: 'producer', label: 'Producer' },
  { key: 'mix', label: 'Mix' },
  { key: 'release_date', label: 'Release date' },
  { key: 'comment', label: 'Comment', textarea: true },
  { key: 'comment2', label: 'Comment 2', textarea: true },
]

export function EditTagsDialog({ track, onClose, onApplied, onError }: Props) {
  const caps = useCaps()
  // Only offer fields THIS platform can persist. Platforms differ in which ones
  // they have — Rekordbox has no Producer or Mix column — and a field the
  // adapter ignores would take the user's typing and silently drop it on save,
  // which is the exact failure the capability system exists to prevent.
  const editable = new Set<string>(caps.tracks.editable_fields)
  const fields = TEXT_FIELDS.filter((f) => editable.has(f.key as string))
  const canRate = editable.has('rating')
  const qc = useQueryClient()
  const { data: facets } = useQuery({ queryKey: ['facets'], queryFn: api.facets })

  const [form, setForm] = useState<Record<string, string>>(() => {
    const f: Record<string, string> = {}
    for (const { key } of TEXT_FIELDS) f[key] = (track[key] as string | null) ?? ''
    return f
  })
  const [rating, setRating] = useState<number>(track.rating)
  const [artFile, setArtFile] = useState<File | null>(null)
  const [artPreview, setArtPreview] = useState<string | null>(null)
  const [artMissing, setArtMissing] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)

  const changedFields = () => {
    const changed: Record<string, string | number | null> = {}
    for (const { key } of fields) {
      const orig = (track[key] as string | null) ?? ''
      if (form[key] !== orig) changed[key] = form[key]
    }
    if (canRate && rating !== track.rating) changed.rating = rating
    return changed
  }

  const apply = useMutation({
    mutationFn: async () => {
      if (artFile) await api.uploadArt(track.id, artFile)
      const changed = changedFields()
      if (Object.keys(changed).length > 0) await api.editTrack(track.id, changed)
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tracks'] })
      qc.invalidateQueries({ queryKey: ['playlist'] })
      qc.invalidateQueries({ queryKey: ['state'] })
      qc.invalidateQueries({ queryKey: ['facets'] })
      qc.invalidateQueries({ queryKey: ['stats'] })
      onApplied(
        `Updated “${form.title || track.title || 'track'}” — ${writeHint(caps.save)}`,
      )
      onClose()
    },
    onError: (e: Error) => onError(e.message),
  })

  const pickArt = (f: File | null) => {
    if (!f) return
    setArtFile(f)
    setArtPreview(URL.createObjectURL(f))
    setArtMissing(false)
  }

  const submit = () => {
    if (!artFile && Object.keys(changedFields()).length === 0) {
      onClose()
      return
    }
    apply.mutate()
  }

  return createPortal(
    <div
      aria-modal="true" className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-[3px] p-6"
      onClick={onClose}
    >
      <div
        className="flex max-h-[85vh] w-full max-w-lg flex-col overflow-hidden glass-overlay"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="border-b border-line px-5 py-4">
          <div className="text-[15px] font-semibold tracking-tight">Edit Tags</div>
          <div className="truncate text-xs text-muted">
            {track.artist} — {track.title}
          </div>
        </div>

        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-5 py-4">
          {/* Album art */}
          <div className="flex items-center gap-4">
            <div className="flex h-24 w-24 shrink-0 items-center justify-center overflow-hidden rounded-md border border-line bg-ink-850">
              {artPreview ? (
                <img src={artPreview} alt="cover" className="h-full w-full object-cover" />
              ) : artMissing ? (
                <span className="text-[10px] uppercase tracking-wider text-faint">No art</span>
              ) : (
                <img
                  src={api.artUrl(track.id)}
                  alt="cover"
                  className="h-full w-full object-cover"
                  onError={() => setArtMissing(true)}
                />
              )}
            </div>
            <div className="min-w-0">
              <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-faint">
                Album art
              </span>
              {/* Replacing art is only offered where the adapter can write it;
                  the existing image is still shown either way. */}
              {caps.tracks.artwork ? (
                <>
                  <input
                    ref={fileInput}
                    type="file"
                    accept="image/jpeg,image/png"
                    className="hidden"
                    onChange={(e) => pickArt(e.target.files?.[0] ?? null)}
                  />
                  <button
                    onClick={() => fileInput.current?.click()}
                    className="rounded-md border border-line bg-ink-850 px-3 py-1.5 text-sm text-text hover:border-ink-600"
                  >
                    {artFile ? 'Change image…' : 'Replace…'}
                  </button>
                  {artFile && (
                    <div className="mt-1 truncate text-[11px] text-mint">
                      {artFile.name} (applies on Save)
                    </div>
                  )}
                </>
              ) : (
                <div className="text-[11px] text-faint">
                  Cover art can’t be changed on {caps.save.app_name} libraries.
                </div>
              )}
            </div>
          </div>

          {fields.map(({ key, label, textarea }) => (
            <label key={key} className="block">
              <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-faint">
                {label}
              </span>
              {textarea ? (
                <textarea
                  value={form[key]}
                  onChange={(e) => setForm({ ...form, [key]: e.target.value })}
                  rows={2}
                  className="w-full resize-y rounded-lg well px-2.5 py-1.5 text-sm text-text outline-none focus:ring-1 focus:ring-accent"
                />
              ) : (
                <input
                  value={form[key]}
                  onChange={(e) => setForm({ ...form, [key]: e.target.value })}
                  list={key === 'genre' ? 'genre-suggestions' : undefined}
                  className="w-full rounded-lg well px-2.5 py-1.5 text-sm text-text outline-none focus:ring-1 focus:ring-accent"
                />
              )}
            </label>
          ))}
          <datalist id="genre-suggestions">
            {facets?.genres.map((g) => <option key={g.name} value={g.name} />)}
          </datalist>

          {/* Rating */}
          {canRate && (
          <div>
            <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-faint">
              Rating
            </span>
            <div className="flex items-center gap-1 text-xl">
              <RatingStars value={rating} max={caps.tracks.rating_max} onChange={setRating} />
              {rating > 0 && (
                <button
                  onClick={() => setRating(0)}
                  className="ml-2 text-xs text-faint hover:text-text"
                >
                  clear
                </button>
              )}
            </div>
          </div>
          )}

          {/* Read-only technical fields */}
          <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 rounded-md bg-ink-850 px-3 py-2 text-xs text-muted">
            <div>BPM: <span className="tabular-nums text-text">{formatBpm(track.bpm)}</span></div>
            <div>Key: <span className="text-text">{track.key ?? '—'}</span></div>
            <div>Length: <span className="tabular-nums text-text">{formatDuration(track.length)}</span></div>
            <div>Bitrate: <span className="tabular-nums text-text">{track.bitrate ? Math.round(track.bitrate / 1000) + 'k' : '—'}</span></div>
            <div>Plays: <span className="tabular-nums text-text">{track.playcount ?? 0}</span></div>
            <div>Cues: <span className="tabular-nums text-text">{track.cue_count}</span></div>
            <div className="col-span-2 truncate" title={track.filepath ?? ''}>
              Path: <span className="text-text">{track.filepath ?? '—'}</span>
            </div>
          </div>
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-line px-5 py-3">
          <button
            onClick={onClose}
            className="rounded-md px-3 py-1.5 text-sm text-muted hover:bg-ink-800 hover:text-text"
          >
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={apply.isPending}
            className="rounded-full btn-primary px-4 py-1.5 text-sm font-semibold disabled:opacity-50"
          >
            {apply.isPending ? 'Applying…' : 'Apply'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
