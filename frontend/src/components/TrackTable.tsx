import { useRef } from 'react'
import {
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnSizingState,
  type Header,
  type SortingState,
  type VisibilityState,
} from '@tanstack/react-table'
import { useVirtualizer } from '@tanstack/react-virtual'
import {
  DndContext,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from '@dnd-kit/core'
import { restrictToHorizontalAxis } from '@dnd-kit/modifiers'
import {
  SortableContext,
  arrayMove,
  horizontalListSortingStrategy,
  useSortable,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import type { Track } from '../api'
import { TRACK_COLUMNS } from '../lib/trackColumns'

interface Selection {
  selected: Set<string>
  onToggle: (id: string) => void
  onToggleAll: () => void
  allSelected: boolean
}

interface Props {
  tracks: Track[]
  sorting: SortingState
  onSortingChange: (s: SortingState) => void
  columnVisibility: VisibilityState
  columnOrder: string[]
  columnSizing: ColumnSizingState
  onColumnOrderChange: (o: string[]) => void
  onColumnSizingChange: (s: ColumnSizingState) => void
  selection?: Selection
  onRowContextMenu?: (track: Track, x: number, y: number) => void
  onPlay?: (track: Track) => void
  activeTrackId?: string | null
}

/** One draggable + resizable header cell. */
function SortableHeader({ header }: { header: Header<Track, unknown> }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: header.column.id,
  })
  const sorted = header.column.getIsSorted()
  return (
    <div
      ref={setNodeRef}
      style={{
        width: header.getSize(),
        transform: CSS.Translate.toString(transform),
        transition,
        opacity: isDragging ? 0.6 : 1,
        zIndex: isDragging ? 20 : undefined,
      }}
      className="relative flex shrink-0 items-center"
    >
      <button
        {...attributes}
        {...listeners}
        onClick={header.column.getToggleSortingHandler()}
        className="flex flex-1 cursor-grab items-center gap-1 px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-muted hover:text-text active:cursor-grabbing"
      >
        {flexRender(header.column.columnDef.header, header.getContext())}
        <span className="text-accent">
          {sorted === 'asc' ? '↑' : sorted === 'desc' ? '↓' : ''}
        </span>
      </button>
      {header.column.getCanResize() && (
        <div
          onMouseDown={header.getResizeHandler()}
          onTouchStart={header.getResizeHandler()}
          onPointerDown={(e) => e.stopPropagation()}
          onClick={(e) => e.stopPropagation()}
          className="absolute right-0 top-0 h-full w-1.5 cursor-col-resize touch-none select-none hover:bg-accent/40"
          title="Drag to resize"
        />
      )}
    </div>
  )
}

export function TrackTable({
  tracks,
  sorting,
  onSortingChange,
  columnVisibility,
  columnOrder,
  columnSizing,
  onColumnOrderChange,
  onColumnSizingChange,
  selection,
  onRowContextMenu,
  onPlay,
  activeTrackId,
}: Props) {
  const table = useReactTable({
    data: tracks,
    columns: TRACK_COLUMNS,
    state: { sorting, columnVisibility, columnOrder, columnSizing },
    onSortingChange: (updater) =>
      onSortingChange(typeof updater === 'function' ? updater(sorting) : updater),
    onColumnSizingChange: (updater) =>
      onColumnSizingChange(typeof updater === 'function' ? updater(columnSizing) : updater),
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    enableColumnResizing: true,
    columnResizeMode: 'onChange',
  })

  const rows = table.getRowModel().rows
  const parentRef = useRef<HTMLDivElement>(null)

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 44,
    overscan: 12,
  })
  const virtualRows = virtualizer.getVirtualItems()

  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 5 } }))
  const headers = table.getHeaderGroups()[0].headers
  const leadWidth = 40 + (onPlay ? 36 : 0)
  const totalWidth = table.getTotalSize() + leadWidth

  const onDragEnd = (e: DragEndEvent) => {
    const { active, over } = e
    if (!over || active.id === over.id) return
    const from = columnOrder.indexOf(active.id as string)
    const to = columnOrder.indexOf(over.id as string)
    if (from < 0 || to < 0) return
    onColumnOrderChange(arrayMove(columnOrder, from, to))
  }

  return (
    <div ref={parentRef} className="h-full overflow-auto">
      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        modifiers={[restrictToHorizontalAxis]}
        onDragEnd={onDragEnd}
      >
        <div style={{ width: totalWidth, minWidth: '100%' }}>
          {/* Header */}
          <div className="sticky top-0 z-10 flex border-b border-line bg-ink-850">
            {selection ? (
              <span className="flex w-10 shrink-0 items-center justify-center">
                <input
                  type="checkbox"
                  checked={selection.allSelected}
                  onChange={selection.onToggleAll}
                  className="accent-accent"
                  title="Select all"
                />
              </span>
            ) : (
              <span className="w-10 shrink-0" />
            )}
            {onPlay && <span className="w-9 shrink-0" />}
            <SortableContext
              items={headers.map((h) => h.column.id)}
              strategy={horizontalListSortingStrategy}
            >
              {headers.map((header) => (
                <SortableHeader key={header.id} header={header} />
              ))}
            </SortableContext>
          </div>

          {/* Virtualized rows */}
          <div style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>
            {virtualRows.map((vr) => {
              const row = rows[vr.index]
              const isSelected = selection?.selected.has(row.original.id) ?? false
              const isActive = activeTrackId === row.original.id
              return (
                <div
                  key={row.id}
                  className={`group absolute left-0 flex items-center border-b border-ink-850 text-sm ${
                    isSelected ? 'bg-accent-soft/50' : 'hover:bg-ink-850'
                  }`}
                  style={{
                    top: 0,
                    transform: `translateY(${vr.start}px)`,
                    height: vr.size,
                    width: '100%',
                  }}
                  onContextMenu={(e) => {
                    if (!onRowContextMenu) return
                    e.preventDefault()
                    onRowContextMenu(row.original, e.clientX, e.clientY)
                  }}
                >
                  {selection ? (
                    <span className="flex w-10 shrink-0 items-center justify-center">
                      <input
                        type="checkbox"
                        checked={isSelected}
                        onChange={() => selection.onToggle(row.original.id)}
                        className="accent-accent"
                      />
                    </span>
                  ) : (
                    <span className="w-10 shrink-0 pr-2 text-right text-xs tabular-nums text-faint">
                      {vr.index + 1}
                    </span>
                  )}
                  {onPlay && (
                    <span className="flex w-9 shrink-0 items-center justify-center">
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          onPlay(row.original)
                        }}
                        className={`flex h-6 w-6 items-center justify-center rounded-full transition-colors hover:bg-accent hover:text-ink-950 ${
                          isActive
                            ? 'text-accent opacity-100'
                            : 'text-faint opacity-0 group-hover:opacity-100'
                        }`}
                        title="Play in deck"
                      >
                        <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
                          <path d="M8 5.14v13.72a1 1 0 0 0 1.54.84l10.29-6.86a1 1 0 0 0 0-1.68L9.54 4.3A1 1 0 0 0 8 5.14z" />
                        </svg>
                      </button>
                    </span>
                  )}
                  {row.getVisibleCells().map((cell) => (
                    <div
                      key={cell.id}
                      style={{ width: cell.column.getSize() }}
                      className="flex shrink-0 items-center overflow-hidden px-3"
                    >
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </div>
                  ))}
                </div>
              )
            })}
          </div>
        </div>
      </DndContext>
    </div>
  )
}
