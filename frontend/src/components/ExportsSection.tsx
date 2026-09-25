import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type ExportSet } from '../api'
import { ExportDialog } from './ExportDialog'
import { ExportRunDialog } from './ExportRunDialog'
import type { Source } from './Sidebar'
import { Icon } from '../lib/icons'

/**
 * Exports in the sidebar: each one a named, persisted slice of the library
 * bound to a destination.
 *
 * An export expands to show the playlists it references. Those are LIVE links —
 * what ships is whatever the playlist holds at export time — which is why the
 * counts here come from `contents`, recomputed on every read, rather than from
 * anything stored in the set.
 *
 * The one deliberate omission: a referenced playlist has no per-track remove.
 * A live reference and per-track removal are in tension, and honouring both
 * would need an exclusion list — hidden state quietly deciding what a future
 * export contains. Remove the whole playlist, or add the tracks instead of it.
 */

interface Props {
  source: Source
  onSelect: (s: Source) => void
  onDone: (msg: string) => void
  onError: (msg: string) => void
}

export function ExportsSection({ source, onSelect, onDone, onError }: Props) {
  const qc = useQueryClient()
  const [dialog, setDialog] = useState<{ editing: ExportSet | null } | null>(null)
  const [running, setRunning] = useState<ExportSet | null>(null)

  const sets = useQuery({ queryKey: ['exports'], queryFn: api.exports })

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteExport(id),
    onSuccess: (_r, id) => {
      qc.invalidateQueries({ queryKey: ['exports'] })
      if (
        (source.kind === 'export' && source.id === id) ||
        ((source.kind === 'export-playlist' || source.kind === 'export-other') &&
          source.exportId === id)
      )
        onSelect({ kind: 'all' })
    },
    onError: (e: Error) => onError(e.message),
  })

  const list = sets.data ?? []

  return (
    <>
      {dialog && (
        <ExportDialog
          editing={dialog.editing}
          onClose={() => setDialog(null)}
          onSaved={(saved, created) => {
            qc.invalidateQueries({ queryKey: ['exports'] })
            qc.invalidateQueries({ queryKey: ['export-contents'] })
            setDialog(null)
            onDone(created ? `Created “${saved.name}”` : `Saved “${saved.name}”`)
            if (created) onSelect({ kind: 'export', id: saved.id, name: saved.name })
          }}
          onError={onError}
        />
      )}

      {running && (
        <ExportRunDialog
          set={running}
          onClose={() => setRunning(null)}
          onDone={onDone}
          onError={onError}
        />
      )}

      <div className="mt-3 flex items-center justify-between px-4">
        <span className="text-[10px] font-semibold uppercase tracking-wider text-faint">
          Exports
        </span>
        <button
          title="New export"
          onClick={() => setDialog({ editing: null })}
          className="rounded px-1 text-sm text-faint hover:text-text"
        >
          +
        </button>
      </div>

      <div className="mt-1 px-2">
        {list.length === 0 && (
          <div className="px-2 py-1.5 text-[11px] text-faint">
            A curated set of tracks, copied to a folder with its own library file.
          </div>
        )}
        {list.map((set) => (
          <ExportRow
            key={set.id}
            set={set}
            source={source}
            onSelect={onSelect}
            onEdit={() => setDialog({ editing: set })}
            onRun={() => setRunning(set)}
            onDelete={() => {
              // Deleting a set never touches its destination folder — the files
              // already exported are the user's, not Konduktor's to clean up.
              if (confirm(`Delete the export “${set.name}”? Nothing already exported is removed.`))
                remove.mutate(set.id)
            }}
            onError={onError}
          />
        ))}
      </div>
    </>
  )
}

function ExportRow({
  set,
  source,
  onSelect,
  onEdit,
  onRun,
  onDelete,
  onError,
}: {
  set: ExportSet
  source: Source
  onSelect: (s: Source) => void
  onEdit: () => void
  onRun: () => void
  onDelete: () => void
  onError: (msg: string) => void
}) {
  const qc = useQueryClient()
  const selected = source.kind === 'export' && source.id === set.id
  const inside =
    selected ||
    ((source.kind === 'export-playlist' || source.kind === 'export-other') &&
      source.exportId === set.id)
  const [open, setOpen] = useState(false)

  // Live: recomputed every read, because a referenced playlist edited after
  // curation changes what this export would ship.
  const contents = useQuery({
    queryKey: ['export-contents', set.id],
    queryFn: () => api.exportContents(set.id),
  })

  const dropPlaylist = useMutation({
    mutationFn: (playlistId: string) =>
      api.removeFromExport(set.id, { playlist_ids: [playlistId] }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['export-contents', set.id] })
      qc.invalidateQueries({ queryKey: ['export-tracks', set.id] })
    },
    onError: (e: Error) => onError(e.message),
  })

  const data = contents.data
  const expanded = open || inside

  return (
    <div>
      <div
        className={`group flex w-full items-center gap-1 rounded-md pr-1 transition-colors ${
          selected ? 'is-selected text-text' : 'text-muted hover:bg-ink-800 hover:text-text'
        }`}
      >
        <button
          title={expanded ? 'Collapse' : 'Expand'}
          onClick={() => setOpen((o) => !o)}
          className="flex w-4 shrink-0 justify-center py-1.5 text-faint hover:text-text"
        >
          <Icon name={expanded ? 'chevronDown' : 'chevronRight'} size={12} strokeWidth={2.4} />
        </button>
        <button
          onClick={() => onSelect({ kind: 'export', id: set.id, name: set.name })}
          className="flex min-w-0 flex-1 items-center gap-2 py-1.5 text-left text-sm"
        >
          <span className="flex w-4 shrink-0 justify-center text-gold"><Icon name="export" size={14} /></span>
          <span className="flex-1 truncate" title={set.destination}>
            {set.name}
          </span>
          <span className="shrink-0 text-[10px] tabular-nums text-faint">
            {data ? data.tracks : ''}
          </span>
        </button>
        <button
          title="Edit name, target and destination"
          onClick={onEdit}
          className="shrink-0 px-1 text-xs text-faint opacity-0 hover:text-text group-hover:opacity-100"
        >
          <Icon name="settings" size={13} />
        </button>
        <button
          title="Delete this export"
          onClick={onDelete}
          className="shrink-0 px-1 text-xs text-faint opacity-0 hover:text-pink group-hover:opacity-100"
        >
          <Icon name="close" size={13} />
        </button>
      </div>

      {expanded && (
        <>
          {(data?.playlists ?? []).map((p) => {
            const chosen = source.kind === 'export-playlist' && source.id === p.id
            return (
              <div
                key={p.id}
                className={`group flex items-center gap-1 rounded-md pr-1 ${
                  chosen ? 'is-selected text-text' : 'text-muted hover:bg-ink-800 hover:text-text'
                }`}
              >
                <button
                  disabled={p.missing}
                  onClick={() =>
                    onSelect({
                      kind: 'export-playlist',
                      exportId: set.id,
                      id: p.id,
                      name: p.name,
                    })
                  }
                  style={{ paddingLeft: 24 }}
                  className="flex min-w-0 flex-1 items-center gap-2 py-1 text-left text-sm disabled:cursor-not-allowed"
                >
                  <span className="flex w-4 shrink-0 justify-center text-faint"><Icon name="playlist" size={14} /></span>
                  {/* A playlist deleted since curation is SHOWN, not dropped:
                      the set records the user's intent, and losing part of it
                      silently is worse than showing something broken. */}
                  <span className={`flex-1 truncate ${p.missing ? 'text-faint line-through' : ''}`}>
                    {p.name}
                  </span>
                  <span className="shrink-0 text-[10px] tabular-nums text-faint">
                    {p.missing ? 'gone' : p.count}
                  </span>
                </button>
                <button
                  title="Remove this playlist from the export"
                  onClick={() => dropPlaylist.mutate(p.id)}
                  className="shrink-0 px-1 text-xs text-faint opacity-0 hover:text-pink group-hover:opacity-100"
                >
                  <Icon name="close" size={13} />
                </button>
              </div>
            )
          })}

          {!!data?.loose && (
            <button
              onClick={() => onSelect({ kind: 'export-other', exportId: set.id, name: 'Other' })}
              style={{ paddingLeft: 24 }}
              className={`flex w-full items-center gap-2 rounded-md py-1 pr-2 text-left text-sm transition-colors ${
                source.kind === 'export-other' && source.exportId === set.id
                  ? 'is-selected text-text'
                  : 'text-muted hover:bg-ink-800 hover:text-text'
              }`}
            >
              <span className="flex w-4 shrink-0 justify-center text-faint"><Icon name="music" size={14} /></span>
              <span className="flex-1 truncate">Other</span>
              <span className="shrink-0 text-[10px] tabular-nums text-faint">{data.loose}</span>
            </button>
          )}

          {!!data?.dangling.length && (
            <div
              style={{ paddingLeft: 24 }}
              className="py-1 pr-2 text-[11px] text-pink"
              title={data.dangling.join('\n')}
            >
              {data.dangling.length} track{data.dangling.length === 1 ? '' : 's'} no longer in your
              library
            </div>
          )}

          {expanded && data && data.tracks === 0 && (
            <div style={{ paddingLeft: 24 }} className="py-1 pr-2 text-[11px] text-faint">
              Empty — right-click tracks and choose Add to.
            </div>
          )}

          {!!data?.tracks && (
            <button
              onClick={onRun}
              style={{ marginLeft: 24 }}
              className="mb-1 mt-1 rounded-full btn-primary px-2 py-1 text-xs font-semibold"
            >
              Export {data.tracks} track{data.tracks === 1 ? '' : 's'}…
            </button>
          )}
        </>
      )}
    </div>
  )
}
