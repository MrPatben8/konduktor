// Central definition of the library table's columns: the TanStack column defs,
// their default widths/visibility/order, and the (id,label) list the "Columns"
// menu renders. Both TrackTable and the Toolbar menu read from here so they stay
// in sync. Column layout (visibility, order, sizing) is persisted to userprefs.
//
// Safe metadata fields are inline-editable: double-click a cell value to edit it
// (the right-click "Edit Tags" dialog remains for multi-field edits). Editing is
// wired through the table's `meta.onEditField` (see the module augmentation).
import { useState, type ReactNode } from 'react'
import {
  createColumnHelper,
  type CellContext,
  type ColumnDef,
  type RowData,
  type VisibilityState,
} from '@tanstack/react-table'
import type { Track } from '../api'
import { formatBpm, formatDuration, keyColor } from './format'
import { RatingStars } from '../components/RatingStars'

declare module '@tanstack/react-table' {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface TableMeta<TData extends RowData> {
    onEditField?: (track: Track, field: keyof Track, value: string | number) => void
  }
}

const col = createColumnHelper<Track>()

// Double-click-to-edit text cell. Shows `display` (or the raw value) until the
// user double-clicks, then an input; commits on Enter/blur, cancels on Escape.
function InlineEdit({
  value,
  display,
  onCommit,
  className = 'w-full min-w-0 truncate text-muted',
}: {
  value: string | null
  display?: ReactNode
  onCommit: (v: string) => void
  className?: string
}) {
  const [editing, setEditing] = useState(false)
  const [val, setVal] = useState('')

  if (editing) {
    const commit = () => {
      setEditing(false)
      if (val !== (value ?? '')) onCommit(val)
    }
    return (
      <input
        autoFocus
        value={val}
        onChange={(e) => setVal(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            commit()
          } else if (e.key === 'Escape') {
            e.preventDefault()
            setEditing(false)
          }
          e.stopPropagation()
        }}
        onClick={(e) => e.stopPropagation()}
        onDoubleClick={(e) => e.stopPropagation()}
        className="w-full min-w-0 rounded bg-ink-800 px-1 py-0.5 text-sm text-text outline-none ring-1 ring-accent"
      />
    )
  }
  return (
    <div
      onDoubleClick={(e) => {
        e.stopPropagation()
        setVal(value ?? '')
        setEditing(true)
      }}
      className={className}
      title="Double-click to edit"
    >
      {display ?? (value?.trim() ? value : <span className="text-faint">—</span>)}
    </div>
  )
}

// Cell renderer factory for a plain editable text field.
function editable(field: keyof Track, className?: string) {
  return (c: CellContext<Track, unknown>) => (
    <InlineEdit
      value={c.getValue() as string | null}
      className={className}
      onCommit={(v) => c.table.options.meta?.onEditField?.(c.row.original, field, v)}
    />
  )
}

export const TRACK_COLUMNS: ColumnDef<Track, any>[] = [
  col.accessor('title', {
    id: 'title',
    header: 'Title',
    size: 300,
    cell: (c) => {
      const r = c.row.original
      const edit = c.table.options.meta?.onEditField
      const title = c.getValue() as string | null
      return (
        <div className="min-w-0">
          <InlineEdit
            value={title}
            display={title || <span className="text-faint">Untitled</span>}
            className="w-full min-w-0 truncate font-medium text-text"
            onCommit={(v) => edit?.(r, 'title', v)}
          />
          <InlineEdit
            value={r.artist}
            className="w-full min-w-0 truncate text-xs text-muted"
            onCommit={(v) => edit?.(r, 'artist', v)}
          />
        </div>
      )
    },
  }),
  col.accessor('album', { id: 'album', header: 'Album', size: 180, cell: editable('album') }),
  col.accessor('genre', { id: 'genre', header: 'Genre', size: 140, cell: editable('genre') }),
  col.accessor('label', { id: 'label', header: 'Label', size: 140, cell: editable('label') }),
  col.accessor('remixer', { id: 'remixer', header: 'Remixer', size: 140, cell: editable('remixer') }),
  col.accessor('producer', { id: 'producer', header: 'Producer', size: 140, cell: editable('producer') }),
  col.accessor('mix', { id: 'mix', header: 'Mix', size: 120, cell: editable('mix') }),
  col.accessor('comment', { id: 'comment', header: 'Comment', size: 200, cell: editable('comment') }),
  col.accessor('bpm', {
    id: 'bpm',
    header: 'BPM',
    size: 74,
    cell: (c) => <span className="tabular-nums text-text">{formatBpm(c.getValue())}</span>,
  }),
  col.accessor('key', {
    id: 'key',
    header: 'Key',
    size: 64,
    cell: (c) => {
      const k = c.getValue()
      if (!k) return <span className="text-faint">—</span>
      return (
        <span
          className="rounded px-1.5 py-0.5 text-xs font-semibold"
          style={{ color: keyColor(k), background: 'color-mix(in srgb, currentColor 14%, transparent)' }}
        >
          {k}
        </span>
      )
    },
  }),
  col.accessor('rating', {
    id: 'rating',
    header: 'Rating',
    size: 92,
    cell: (c) => (
      <RatingStars
        value={c.getValue() as number}
        onChange={(v) => c.table.options.meta?.onEditField?.(c.row.original, 'rating', v)}
      />
    ),
  }),
  col.accessor('length', {
    id: 'length',
    header: 'Time',
    size: 64,
    cell: (c) => <span className="tabular-nums text-muted">{formatDuration(c.getValue())}</span>,
  }),
  col.accessor('bitrate', {
    id: 'bitrate',
    header: 'Bitrate',
    size: 84,
    cell: (c) => {
      const b = c.getValue()
      return b ? (
        <span className="tabular-nums text-muted">{Math.round(b / 1000)}</span>
      ) : (
        <span className="text-faint">—</span>
      )
    },
  }),
  col.accessor('cue_count', {
    id: 'cue_count',
    header: 'Cues',
    size: 72,
    cell: (c) => {
      const n = c.getValue()
      const grid = c.row.original.has_grid
      return (
        <span className="flex items-center gap-1 tabular-nums">
          <span className={n > 0 ? 'text-text' : 'text-faint'}>{n}</span>
          {grid && <span className="text-[10px] text-mint" title="Beatgrid analyzed">⊞</span>}
        </span>
      )
    },
  }),
  col.accessor('playcount', {
    id: 'playcount',
    header: 'Plays',
    size: 68,
    cell: (c) => <span className="tabular-nums text-muted">{c.getValue() || 0}</span>,
  }),
  col.accessor('import_date', {
    id: 'import_date',
    header: 'Imported',
    size: 104,
    cell: (c) => <span className="tabular-nums text-muted">{formatDate(c.getValue())}</span>,
  }),
  col.accessor('last_played', {
    id: 'last_played',
    header: 'Last played',
    size: 108,
    cell: (c) => <span className="tabular-nums text-muted">{formatDate(c.getValue())}</span>,
  }),
  col.accessor('release_date', {
    id: 'release_date',
    header: 'Released',
    size: 104,
    cell: (c) => (
      <InlineEdit
        value={c.getValue() as string | null}
        display={<span className="tabular-nums text-muted">{formatDate(c.getValue() as string | null)}</span>}
        onCommit={(v) => c.table.options.meta?.onEditField?.(c.row.original, 'release_date', v)}
      />
    ),
  }),
]

// Traktor dates arrive as "YYYY/M/D" strings; show them compactly (or raw).
function formatDate(v: string | null): string {
  if (!v) return '—'
  const m = v.match(/(\d{4})\/(\d{1,2})\/(\d{1,2})/)
  if (!m) return v
  return `${m[1]}-${m[2].padStart(2, '0')}-${m[3].padStart(2, '0')}`
}

/** (id, label) pairs for the Columns menu, in the canonical definition order. */
export const COLUMN_MENU: { id: string; label: string }[] = TRACK_COLUMNS.map((c) => ({
  id: c.id as string,
  label: typeof c.header === 'string' ? c.header : (c.id as string),
}))

export const DEFAULT_COLUMN_ORDER: string[] = TRACK_COLUMNS.map((c) => c.id as string)

// Columns shown out of the box; the rest are opt-in via the menu.
const DEFAULT_VISIBLE = new Set([
  'title', 'album', 'genre', 'bpm', 'key', 'rating', 'length', 'cue_count', 'playcount',
])
export const DEFAULT_COLUMN_VISIBILITY: VisibilityState = Object.fromEntries(
  DEFAULT_COLUMN_ORDER.map((id) => [id, DEFAULT_VISIBLE.has(id)]),
)
