import { useMemo, useRef } from 'react'
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type SortingState,
} from '@tanstack/react-table'
import { useVirtualizer } from '@tanstack/react-virtual'
import type { Track } from '../api'
import { formatBpm, formatDuration, keyColor } from '../lib/format'
import { RatingStars } from './RatingStars'

const col = createColumnHelper<Track>()

const columns = [
  col.accessor('title', {
    header: 'Title',
    size: 320,
    cell: (c) => (
      <div className="min-w-0">
        <div className="truncate font-medium text-text">
          {c.getValue() || <span className="text-faint">Untitled</span>}
        </div>
        <div className="truncate text-xs text-muted">{c.row.original.artist}</div>
      </div>
    ),
  }),
  col.accessor('album', {
    header: 'Album',
    size: 200,
    cell: (c) => <span className="truncate text-muted">{c.getValue() || '—'}</span>,
  }),
  col.accessor('genre', {
    header: 'Genre',
    size: 150,
    cell: (c) => {
      const g = c.getValue()?.trim()
      return g ? (
        <span className="truncate text-muted">{g}</span>
      ) : (
        <span className="text-faint">—</span>
      )
    },
  }),
  col.accessor('bpm', {
    header: 'BPM',
    size: 74,
    cell: (c) => <span className="tabular-nums text-text">{formatBpm(c.getValue())}</span>,
  }),
  col.accessor('key', {
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
    header: 'Rating',
    size: 92,
    cell: (c) => <RatingStars value={c.getValue()} />,
  }),
  col.accessor('length', {
    header: 'Time',
    size: 64,
    cell: (c) => <span className="tabular-nums text-muted">{formatDuration(c.getValue())}</span>,
  }),
  col.accessor('cue_count', {
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
    header: 'Plays',
    size: 68,
    cell: (c) => <span className="tabular-nums text-muted">{c.getValue() || 0}</span>,
  }),
]

interface Props {
  tracks: Track[]
  sorting: SortingState
  onSortingChange: (s: SortingState) => void
  // Playlists have a meaningful order; disable default sort to preserve it.
  ordered?: boolean
}

export function TrackTable({ tracks, sorting, onSortingChange }: Props) {
  const table = useReactTable({
    data: tracks,
    columns,
    state: { sorting },
    onSortingChange: (updater) =>
      onSortingChange(typeof updater === 'function' ? updater(sorting) : updater),
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  })

  const rows = table.getRowModel().rows
  const parentRef = useRef<HTMLDivElement>(null)

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 44,
    overscan: 12,
  })

  const totalWidth = useMemo(
    () => table.getTotalSize(),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  )
  const virtualRows = virtualizer.getVirtualItems()

  return (
    <div ref={parentRef} className="h-full overflow-auto">
      <div style={{ width: totalWidth, minWidth: '100%' }}>
        {/* Header */}
        <div className="sticky top-0 z-10 flex border-b border-line bg-ink-850">
          <span className="w-10 shrink-0" />
          {table.getHeaderGroups()[0].headers.map((header) => {
            const sorted = header.column.getIsSorted()
            return (
              <button
                key={header.id}
                onClick={header.column.getToggleSortingHandler()}
                style={{ width: header.getSize() }}
                className="flex items-center gap-1 px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-muted hover:text-text"
              >
                {flexRender(header.column.columnDef.header, header.getContext())}
                <span className="text-accent">
                  {sorted === 'asc' ? '↑' : sorted === 'desc' ? '↓' : ''}
                </span>
              </button>
            )
          })}
        </div>

        {/* Virtualized rows */}
        <div style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>
          {virtualRows.map((vr) => {
            const row = rows[vr.index]
            return (
              <div
                key={row.id}
                className="absolute left-0 flex items-center border-b border-ink-850 text-sm hover:bg-ink-850"
                style={{
                  top: 0,
                  transform: `translateY(${vr.start}px)`,
                  height: vr.size,
                  width: '100%',
                }}
              >
                <span className="w-10 shrink-0 pr-2 text-right text-xs tabular-nums text-faint">
                  {vr.index + 1}
                </span>
                {row.getVisibleCells().map((cell) => (
                  <div
                    key={cell.id}
                    style={{ width: cell.column.getSize() }}
                    className="flex items-center overflow-hidden px-3"
                  >
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </div>
                ))}
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
