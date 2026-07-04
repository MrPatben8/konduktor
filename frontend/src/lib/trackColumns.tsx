// Central definition of the library table's columns: the TanStack column defs,
// their default widths/visibility/order, and the (id,label) list the "Columns"
// menu renders. Both TrackTable and the Toolbar menu read from here so they stay
// in sync. Column layout (visibility, order, sizing) is persisted to userprefs.
import { createColumnHelper, type ColumnDef, type VisibilityState } from '@tanstack/react-table'
import type { Track } from '../api'
import { formatBpm, formatDuration, keyColor } from './format'
import { RatingStars } from '../components/RatingStars'

const col = createColumnHelper<Track>()

function dash(v: string | null | undefined) {
  const s = v?.toString().trim()
  return s ? <span className="truncate text-muted">{s}</span> : <span className="text-faint">—</span>
}

// Traktor dates arrive as "YYYY/M/D" strings; show them compactly (or raw).
function formatDate(v: string | null): string {
  if (!v) return '—'
  const m = v.match(/(\d{4})\/(\d{1,2})\/(\d{1,2})/)
  if (!m) return v
  return `${m[1]}-${m[2].padStart(2, '0')}-${m[3].padStart(2, '0')}`
}

export const TRACK_COLUMNS: ColumnDef<Track, any>[] = [
  col.accessor('title', {
    id: 'title',
    header: 'Title',
    size: 300,
    cell: (c) => (
      <div className="min-w-0">
        <div className="truncate font-medium text-text">
          {c.getValue() || <span className="text-faint">Untitled</span>}
        </div>
        <div className="truncate text-xs text-muted">{c.row.original.artist}</div>
      </div>
    ),
  }),
  col.accessor('album', { id: 'album', header: 'Album', size: 180, cell: (c) => dash(c.getValue()) }),
  col.accessor('genre', { id: 'genre', header: 'Genre', size: 140, cell: (c) => dash(c.getValue()) }),
  col.accessor('label', { id: 'label', header: 'Label', size: 140, cell: (c) => dash(c.getValue()) }),
  col.accessor('remixer', { id: 'remixer', header: 'Remixer', size: 140, cell: (c) => dash(c.getValue()) }),
  col.accessor('producer', { id: 'producer', header: 'Producer', size: 140, cell: (c) => dash(c.getValue()) }),
  col.accessor('mix', { id: 'mix', header: 'Mix', size: 120, cell: (c) => dash(c.getValue()) }),
  col.accessor('comment', { id: 'comment', header: 'Comment', size: 200, cell: (c) => dash(c.getValue()) }),
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
    cell: (c) => <RatingStars value={c.getValue()} />,
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
    cell: (c) => <span className="tabular-nums text-muted">{formatDate(c.getValue())}</span>,
  }),
]

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
