import { useEffect, useRef, useState, type MutableRefObject } from 'react'
import {
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnSizingState,
  type Header,
  type Row,
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
import { restrictToHorizontalAxis, restrictToVerticalAxis, restrictToParentElement } from '@dnd-kit/modifiers'
import {
  SortableContext,
  arrayMove,
  horizontalListSortingStrategy,
  verticalListSortingStrategy,
  useSortable,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import type { Track } from '../api'
import { TRACK_COLUMNS } from '../lib/trackColumns'

const ROW_HEIGHT = 44
// Stable reference. A controlled `state.sorting` that's a fresh `[]` each render
// (with no `onSortingChange`) makes TanStack Table re-sync its internal state
// every commit → infinite re-render loop. Playlists never sort, so share one array.
const NO_SORTING: SortingState = []

interface Selection {
  selected: Set<string>
  onToggle: (id: string) => void
  onToggleAll: () => void
  allSelected: boolean
}

interface CommonProps {
  columnVisibility: VisibilityState
  columnOrder: string[]
  columnSizing: ColumnSizingState
  onColumnOrderChange: (o: string[]) => void
  onColumnSizingChange: (s: ColumnSizingState) => void
  onRowContextMenu?: (track: Track, x: number, y: number) => void
  onPlay?: (track: Track) => void
  onEditField?: (track: Track, field: keyof Track, value: string | number) => void
  activeTrackId?: string | null
}

interface Props extends CommonProps {
  tracks: Track[]
  sorting: SortingState
  onSortingChange: (s: SortingState) => void
  selection?: Selection
}

interface PlaylistProps extends CommonProps {
  tracks: Track[]
  onReorder: (ids: string[]) => void
  onRemove: (id: string) => void
  /** False while a filter/search is active: reordering a filtered subset would
   *  drop the hidden entries, so the drag handle goes inert. */
  canReorder: boolean
}

/** The per-row play button (shared by the library grid and the playlist list).
 *  Fades in on row hover; stays lit for the active deck track. */
function PlayButton({
  track,
  isActive,
  onPlay,
}: {
  track: Track
  isActive: boolean
  onPlay: (track: Track) => void
}) {
  return (
    <button
      onClick={(e) => {
        e.stopPropagation()
        onPlay(track)
      }}
      className={`flex h-6 w-6 items-center justify-center rounded-full transition-colors hover:bg-accent hover:text-ink-950 ${
        isActive ? 'text-accent opacity-100' : 'text-faint opacity-0 group-hover:opacity-100'
      }`}
      title="Play in deck"
    >
      <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
        <path d="M8 5.14v13.72a1 1 0 0 0 1.54.84l10.29-6.86a1 1 0 0 0 0-1.68L9.54 4.3A1 1 0 0 0 8 5.14z" />
      </svg>
    </button>
  )
}

/** The configurable data cells for one row — identical in both views. */
function RowCells({ row }: { row: Row<Track> }) {
  return (
    <>
      {row.getVisibleCells().map((cell) => (
        <div
          key={cell.id}
          style={{ width: cell.column.getSize() }}
          className="flex shrink-0 items-center overflow-hidden px-3"
        >
          {flexRender(cell.column.columnDef.cell, cell.getContext())}
        </div>
      ))}
    </>
  )
}

/** One draggable + resizable header cell. `sortable` is false in playlists,
 *  where the manual order stands (the header still reorders/resizes columns).
 *  `draggedRef` is set true for the duration of a reorder so the stray `click`
 *  the browser fires after a drag doesn't also toggle the sort. */
function SortableHeader({
  header,
  draggedRef,
}: {
  header: Header<Track, unknown>
  draggedRef: MutableRefObject<boolean>
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: header.column.id,
  })
  const sorted = header.column.getIsSorted()
  const toggleSort = header.column.getToggleSortingHandler()
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
        onClick={(e) => {
          // A reorder-drag ends with a stray click on the header — ignore it so
          // dragging a column doesn't also flip its sort direction.
          if (draggedRef.current) return
          toggleSort?.(e)
        }}
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

/** Shared draggable header row (column reorder + resize). */
function HeaderRow({
  headers,
  sensors,
  onColumnDragEnd,
  lead,
  trail,
  hasPlay,
  selection,
}: {
  headers: Header<Track, unknown>[]
  sensors: ReturnType<typeof useSensors>
  onColumnDragEnd: (e: DragEndEvent) => void
  lead?: boolean // playlist drag-handle spacer
  trail?: boolean // playlist remove spacer
  hasPlay: boolean
  selection?: Selection
}) {
  // True while (and just after) a column is being reordered, so the trailing
  // click a drag emits is swallowed instead of toggling the sort.
  const draggedRef = useRef(false)
  return (
    <DndContext
      sensors={sensors}
      collisionDetection={closestCenter}
      modifiers={[restrictToHorizontalAxis]}
      onDragStart={() => {
        draggedRef.current = true
      }}
      onDragEnd={(e) => {
        onColumnDragEnd(e)
        // Clear on the next tick — after the synchronous post-drag click fires.
        setTimeout(() => {
          draggedRef.current = false
        }, 0)
      }}
    >
      <div className="sticky top-0 z-10 flex border-b border-line bg-ink-850">
        {lead && <span className="w-8 shrink-0" />}
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
        {hasPlay && <span className="w-9 shrink-0" />}
        <SortableContext
          items={headers.map((h) => h.column.id)}
          strategy={horizontalListSortingStrategy}
        >
          {headers.map((header) => (
            <SortableHeader key={header.id} header={header} draggedRef={draggedRef} />
          ))}
        </SortableContext>
        {trail && <span className="w-10 shrink-0" />}
      </div>
    </DndContext>
  )
}

/** The virtualized library grid (All Tracks). */
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
  onEditField,
  activeTrackId,
}: Props) {
  const table = useReactTable({
    data: tracks,
    columns: TRACK_COLUMNS,
    state: { sorting, columnVisibility, columnOrder, columnSizing },
    meta: { onEditField },
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
    estimateSize: () => ROW_HEIGHT,
    overscan: 12,
  })
  const virtualRows = virtualizer.getVirtualItems()

  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 8 } }))
  const headers = table.getHeaderGroups()[0].headers
  const hasPlay = !!onPlay
  const leadWidth = 40 + (hasPlay ? 36 : 0)
  const totalWidth = table.getTotalSize() + leadWidth

  const onColumnDragEnd = (e: DragEndEvent) => {
    const { active, over } = e
    if (!over || active.id === over.id) return
    const from = columnOrder.indexOf(active.id as string)
    const to = columnOrder.indexOf(over.id as string)
    if (from < 0 || to < 0) return
    onColumnOrderChange(arrayMove(columnOrder, from, to))
  }

  return (
    <div ref={parentRef} className="h-full overflow-auto">
      <div style={{ width: totalWidth, minWidth: '100%' }}>
        <HeaderRow
          headers={headers}
          sensors={sensors}
          onColumnDragEnd={onColumnDragEnd}
          hasPlay={hasPlay}
          selection={selection}
        />
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
                style={{ top: 0, transform: `translateY(${vr.start}px)`, height: vr.size, width: '100%' }}
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
                {hasPlay && (
                  <span className="flex w-9 shrink-0 items-center justify-center">
                    {onPlay && <PlayButton track={row.original} isActive={isActive} onPlay={onPlay} />}
                  </span>
                )}
                <RowCells row={row} />
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}

/** One reorderable playlist row — standard (non-virtualized) dnd-kit sortable,
 *  in normal document flow. This is the proven-stable arrangement; the virtualizer
 *  is intentionally NOT used here (its flushSync-driven measurement loops when
 *  wrapped in a sortable context). */
function PlaylistRow({
  row,
  index,
  hasPlay,
  canReorder,
  activeTrackId,
  onPlay,
  onRemove,
  onRowContextMenu,
}: {
  row: Row<Track>
  index: number
  hasPlay: boolean
  canReorder: boolean
  activeTrackId?: string | null
  onPlay?: (track: Track) => void
  onRemove: (id: string) => void
  onRowContextMenu?: (track: Track, x: number, y: number) => void
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: row.original.id,
    disabled: !canReorder,
  })
  const isActive = activeTrackId === row.original.id
  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition, height: ROW_HEIGHT }}
      className={`group flex items-center border-b border-ink-850 text-sm ${
        isDragging ? 'relative z-10 bg-ink-800 shadow-lg' : 'hover:bg-ink-850'
      }`}
      onContextMenu={(e) => {
        if (!onRowContextMenu) return
        e.preventDefault()
        onRowContextMenu(row.original, e.clientX, e.clientY)
      }}
    >
      {canReorder ? (
        <button
          {...attributes}
          {...listeners}
          title="Drag to reorder"
          className="flex w-8 shrink-0 cursor-grab items-center justify-center text-faint hover:text-text active:cursor-grabbing"
        >
          ⠿
        </button>
      ) : (
        <span
          title="Clear the search / filters to reorder"
          className="flex w-8 shrink-0 cursor-default items-center justify-center text-faint/25"
        >
          ⠿
        </span>
      )}
      <span className="w-10 shrink-0 pr-2 text-right text-xs tabular-nums text-faint">
        {index + 1}
      </span>
      {hasPlay && (
        <span className="flex w-9 shrink-0 items-center justify-center">
          {onPlay && <PlayButton track={row.original} isActive={isActive} onPlay={onPlay} />}
        </span>
      )}
      <RowCells row={row} />
      <button
        title="Remove from playlist"
        onClick={() => onRemove(row.original.id)}
        className="flex w-10 shrink-0 items-center justify-center text-faint hover:text-pink"
      >
        ×
      </button>
    </div>
  )
}

/** The playlist view: the same columns / play button / inline editing as the
 *  library, plus drag-to-reorder + remove. Non-virtualized (playlists are small),
 *  which keeps it clear of the react-virtual + dnd-kit re-render loop. */
export function PlaylistTable({
  tracks,
  columnVisibility,
  columnOrder,
  columnSizing,
  onColumnOrderChange,
  onColumnSizingChange,
  onRowContextMenu,
  onPlay,
  onEditField,
  activeTrackId,
  onReorder,
  onRemove,
  canReorder,
}: PlaylistProps) {
  // Local manual order so a drag feels instant; re-syncs when the server data changes.
  const [items, setItems] = useState<Track[]>(tracks)
  useEffect(() => setItems(tracks), [tracks])

  const table = useReactTable({
    data: items,
    columns: TRACK_COLUMNS,
    state: { sorting: NO_SORTING, columnVisibility, columnOrder, columnSizing },
    meta: { onEditField },
    enableSorting: false, // playlist order is manual
    onColumnSizingChange: (updater) =>
      onColumnSizingChange(typeof updater === 'function' ? updater(columnSizing) : updater),
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    enableColumnResizing: true,
    columnResizeMode: 'onChange',
  })

  const rows = table.getRowModel().rows
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 8 } }))
  const headers = table.getHeaderGroups()[0].headers
  const hasPlay = !!onPlay
  const leadWidth = 32 + 40 + (hasPlay ? 36 : 0)
  const totalWidth = table.getTotalSize() + leadWidth + 40

  const onColumnDragEnd = (e: DragEndEvent) => {
    const { active, over } = e
    if (!over || active.id === over.id) return
    const from = columnOrder.indexOf(active.id as string)
    const to = columnOrder.indexOf(over.id as string)
    if (from < 0 || to < 0) return
    onColumnOrderChange(arrayMove(columnOrder, from, to))
  }

  const onRowDragEnd = (e: DragEndEvent) => {
    const { active, over } = e
    if (!over || active.id === over.id) return
    const from = items.findIndex((t) => t.id === active.id)
    const to = items.findIndex((t) => t.id === over.id)
    if (from < 0 || to < 0) return
    const next = arrayMove(items, from, to)
    setItems(next)
    onReorder(next.map((t) => t.id))
  }

  return (
    <div className="h-full overflow-auto">
      <div style={{ width: totalWidth, minWidth: '100%' }}>
        <HeaderRow
          headers={headers}
          sensors={sensors}
          onColumnDragEnd={onColumnDragEnd}
          hasPlay={hasPlay}
          lead
          trail
        />
        <DndContext
          sensors={sensors}
          collisionDetection={closestCenter}
          modifiers={[restrictToVerticalAxis, restrictToParentElement]}
          onDragEnd={onRowDragEnd}
        >
          <SortableContext
            items={rows.map((r) => r.original.id)}
            strategy={verticalListSortingStrategy}
          >
            {rows.map((row, i) => (
              <PlaylistRow
                key={row.original.id}
                row={row}
                index={i}
                hasPlay={hasPlay}
                canReorder={canReorder}
                activeTrackId={activeTrackId}
                onPlay={onPlay}
                onRemove={onRemove}
                onRowContextMenu={onRowContextMenu}
              />
            ))}
          </SortableContext>
        </DndContext>
      </div>
    </div>
  )
}
