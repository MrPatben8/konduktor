import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type MutableRefObject } from 'react'
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
import { Icon } from '../lib/icons'
import { useCaps } from '../lib/capabilities'
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
import type { Track, TrackOrigin } from '../api'
import { beginDrag, useDragPayload, useDropTarget } from '../lib/drag'
import { TRACK_COLUMNS } from '../lib/trackColumns'

/** Fields this platform can persist, for the table's per-cell edit gating.
 *  Platforms differ in WHICH fields they accept, so the all-or-nothing
 *  `onEditField` handler is not enough on its own. */
function useEditableFields(): ReadonlySet<string> {
  const caps = useCaps()
  return useMemo(() => new Set(caps.tracks.editable_fields), [caps.tracks.editable_fields])
}


const ROW_HEIGHT = 44

/** Row selection, Finder-style: click selects one row, Cmd/Ctrl+click toggles,
 *  Shift+click selects a range from the anchor (the last row clicked without
 *  Shift) in the table's SORTED order. The table owns the anchor; the caller
 *  owns the set. */
interface Selection {
  selected: Set<string>
  onChange: (next: Set<string>) => void
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
  /** The row-number column shows these (a playlist's own 1-based positions)
   *  instead of the row's place in the current view. */
  positions?: Map<string, number>
  /** Rows to flag in the # column with a check and this tooltip — a browsed
   *  folder uses it for files the collection already holds. */
  marked?: { ids: Set<string>; title: string }
  /** A view with a manual order. `enabled` is false while a sort or filter is
   *  active: reordering a partial or re-sorted list would lose the real order. */
  reorder?: { enabled: boolean; onReorder: (ids: string[]) => void }
  /** Where dragged rows come from: whose ids they are, and the editable
   *  playlist they leave on an Option-drag (move). Defaults to the collection. */
  dragFrom?: { origin: TrackOrigin; playlistId?: string }
  /** Delete/Backspace removes the selected tracks from this view. */
  onRemove?: (ids: string[]) => void
  onRowContextMenu?: (track: Track, x: number, y: number) => void
  /** Right-click on the header row (the column chooser). */
  onHeaderContextMenu?: (x: number, y: number) => void
  onPlay?: (track: Track) => void
  onEditField?: (track: Track, field: keyof Track, value: string | number) => void
  activeTrackId?: string | null
  /** Whether the deck is playing `activeTrackId` (vs. loaded but paused). */
  activePlaying?: boolean
}

/** The per-row play button, in three states: a small triangle always, an
 *  outlined circle on row hover, and a filled circle while its track plays in
 *  the deck (the loaded track, paused, keeps an accent triangle). */
function PlayButton({
  track,
  isActive,
  isPlaying,
  onPlay,
}: {
  track: Track
  isActive: boolean
  isPlaying: boolean
  onPlay: (track: Track) => void
}) {
  const look = isPlaying
    ? 'border-accent bg-accent text-ink-950'
    : `border-transparent group-hover:border-ink-600 hover:!border-accent hover:text-accent ${
        isActive ? 'text-accent' : 'text-faint group-hover:text-text'
      }`
  return (
    <button
      onClick={(e) => {
        e.stopPropagation()
        onPlay(track)
      }}
      className={`flex h-6 w-6 items-center justify-center rounded-full border-[1.5px] transition-colors ${look}`}
      title="Play in deck"
    >
      <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" aria-hidden className="ml-px">
        <path d="M8 5.14v13.72a1 1 0 0 0 1.54.84l10.29-6.86a1 1 0 0 0 0-1.68L9.54 4.3A1 1 0 0 0 8 5.14z" />
      </svg>
    </button>
  )
}

/** The configurable data cells for one row. */
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

/** One draggable + resizable header cell.
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
  hasPlay,
  onContextMenu,
}: {
  headers: Header<Track, unknown>[]
  sensors: ReturnType<typeof useSensors>
  onColumnDragEnd: (e: DragEndEvent) => void
  hasPlay: boolean
  onContextMenu?: (x: number, y: number) => void
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
      <div
        // Frosted glass: rows scroll UNDER the sticky header, so it blurs
        // them rather than hiding them behind a solid bar. Safe to filter —
        // the column menu it opens is portalled (see "Theme" in CLAUDE.md).
        // Keep the radius SMALL: on a strip this short, Chrome silently skips
        // a large backdrop blur (12 px and up left the rows perfectly sharp).
        // A LIGHT tint, as frosted glass is lighter than what it covers, and
        // inset from the panel's edges so it never blurs the panel's bright
        // rim into a glowing band along its side.
        className="sticky top-0 z-10 mt-1.5 flex rounded-xl bg-white/[0.07] shadow-[inset_0_1px_0_rgb(255_255_255/0.14),inset_0_0_0_1px_rgb(255_255_255/0.07),0_6px_16px_-8px_rgb(0_0_0/0.6)] backdrop-blur-[8px]"
        onContextMenu={(e) => {
          if (!onContextMenu) return
          e.preventDefault()
          onContextMenu(e.clientX, e.clientY)
        }}
      >
        <span className="w-10 shrink-0" />
        {hasPlay && <span className="w-9 shrink-0" />}
        <SortableContext
          items={headers.map((h) => h.column.id)}
          strategy={horizontalListSortingStrategy}
        >
          {headers.map((header) => (
            <SortableHeader key={header.id} header={header} draggedRef={draggedRef} />
          ))}
        </SortableContext>
      </div>
    </DndContext>
  )
}

/** The one track list — All Tracks, playlists, exports and devices alike.
 *  Virtualized, sortable, with Finder-style selection; a view that has a manual
 *  order passes `reorder`, and one whose entries can be removed, `onRemove`.
 *
 *  Row reordering is hand-rolled rather than dnd-kit's sortable: wrapping the
 *  virtualizer in a SortableContext loops on its flushSync-driven measurement,
 *  which is why playlists used to be a separate, non-virtualized table. Rows are
 *  a fixed height, so the drop position is just pointer-y / ROW_HEIGHT. */
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
  positions,
  marked,
  reorder,
  dragFrom,
  onRemove,
  onRowContextMenu,
  onHeaderContextMenu,
  onPlay,
  onEditField,
  activeTrackId,
  activePlaying = false,
}: Props) {
  // A local copy so a reorder shows instantly; re-syncs when the data changes.
  const [data, setData] = useState<Track[]>(tracks)
  useEffect(() => setData(tracks), [tracks])

  const editableFields = useEditableFields()
  const table = useReactTable({
    data,
    columns: TRACK_COLUMNS,
    state: { sorting, columnVisibility, columnOrder, columnSizing },
    meta: { onEditField, editableFields },
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
  const bodyRef = useRef<HTMLDivElement>(null)

  // The rows start BELOW the sticky header, inside the same scroller, so the
  // virtualizer is told where they start (scrollMargin) and how much of the
  // top the header covers (scrollPaddingStart). Without both, scrollToIndex
  // lands a row too far down on ↓ — just out of view — and under the header on ↑.
  const [bodyOffset, setBodyOffset] = useState({ top: 0, header: 0 })
  useLayoutEffect(() => {
    const body = bodyRef.current
    const scroller = parentRef.current
    if (!body || !scroller) return
    const top = body.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop
    const header = (body.previousElementSibling as HTMLElement | null)?.offsetHeight ?? 0
    setBodyOffset((o) => (o.top === top && o.header === header ? o : { top, header }))
  }, [])
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 12,
    scrollMargin: bodyOffset.top,
    scrollPaddingStart: bodyOffset.header,
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

  // Range selection runs from the anchor; arrow keys move from the lead (the
  // row most recently clicked or arrowed to). Both are ids, not indices, so a
  // re-sort keeps them on the same track.
  const anchorRef = useRef<string | null>(null)
  const leadRef = useRef<string | null>(null)
  const indexOf = (id: string | null) => (id == null ? -1 : rows.findIndex((r) => r.original.id === id))
  const rangeIds = (a: number, b: number) =>
    rows.slice(Math.min(a, b), Math.max(a, b) + 1).map((r) => r.original.id)

  const selectOnly = (index: number) => {
    const id = rows[index].original.id
    anchorRef.current = leadRef.current = id
    selection?.onChange(new Set([id]))
  }

  const clickRow = (index: number, e: { shiftKey: boolean; metaKey: boolean; ctrlKey: boolean }) => {
    if (!selection) return
    const id = rows[index].original.id
    const toggle = e.metaKey || e.ctrlKey
    const anchor = indexOf(anchorRef.current)
    if (e.shiftKey && anchor >= 0) {
      const range = rangeIds(anchor, index)
      selection.onChange(toggle ? new Set([...selection.selected, ...range]) : new Set(range))
      leadRef.current = id
    } else if (toggle) {
      const next = new Set(selection.selected)
      next.has(id) ? next.delete(id) : next.add(id)
      anchorRef.current = leadRef.current = id
      selection.onChange(next)
    } else {
      selectOnly(index)
    }
  }

  // ---- dragging rows ------------------------------------------------------
  // Every table's rows are a drag SOURCE (onto playlists, exports, the deck —
  // lib/drag.tsx); a view with a manual order is also its own drop TARGET, the
  // in-table reorder. `drop` is the gap the rows would land in (0 = above the
  // first row).
  const pressRef = useRef<{ x: number; y: number; index: number } | null>(null)
  const tableKey = useRef({}).current
  const dragging = useDragPayload()
  const ownDrag = dragging?.kind === 'tracks' && dragging.source === tableKey ? dragging : null
  const movingIds = useMemo(() => new Set(ownDrag?.ids ?? []), [ownDrag])

  useEffect(() => {
    const onMove = (e: PointerEvent) => {
      const press = pressRef.current
      if (!press || Math.hypot(e.clientX - press.x, e.clientY - press.y) < 6) return
      pressRef.current = null
      // Dragging a selected row carries the whole selection (in the table's
      // order); any other row goes alone and becomes the selection, so what is
      // being dragged is visible.
      const rs = rowsRef.current
      const sel = selectionRef.current
      const pressed = rs[press.index]?.original
      if (!pressed) return
      let carried: Track[]
      if (sel?.selected.has(pressed.id)) {
        carried = rs.filter((r) => sel.selected.has(r.original.id)).map((r) => r.original)
      } else {
        carried = [pressed]
        sel?.onChange(new Set([pressed.id]))
        anchorRef.current = leadRef.current = pressed.id
      }
      const from = dragFromRef.current
      beginDrag(
        {
          kind: 'tracks',
          ids: [...new Set(carried.map((t) => t.id))],
          tracks: carried,
          lead: pressed,
          origin: from?.origin ?? 'collection',
          fromPlaylist: from?.playlistId,
          source: tableKey,
        },
        { x: e.clientX, y: e.clientY, alt: e.altKey },
      )
    }
    const release = () => (pressRef.current = null)
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', release)
    window.addEventListener('pointercancel', release)
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', release)
      window.removeEventListener('pointercancel', release)
    }
  }, [tableKey])

  const [dropRef, dropHint] = useDropTarget<{ drop: number }>({
    hint: (p, at) => {
      if (p.kind !== 'tracks' || p.source !== tableKey || !reorderRef.current?.enabled) return null
      const top = bodyRef.current?.getBoundingClientRect().top ?? 0
      return { drop: Math.max(0, Math.min(rowsRef.current.length, Math.round((at.y - top) / ROW_HEIGHT))) }
    },
    onDrop: (p, { drop }) => {
      if (p.kind !== 'tracks') return
      const ids = new Set(p.ids)
      const rs = rowsRef.current.map((r) => r.original)
      const kept = rs.filter((t) => !ids.has(t.id))
      const block = rs.filter((t) => ids.has(t.id))
      const at = drop - rs.slice(0, drop).filter((t) => ids.has(t.id)).length
      const next = [...kept.slice(0, at), ...block, ...kept.slice(at)]
      if (next.every((t, i) => t === rs[i])) return
      setData(next)
      reorderRef.current?.onReorder(next.map((t) => t.id))
    },
  })
  const scrollerRef = useCallback(
    (el: HTMLDivElement | null) => {
      parentRef.current = el
      dropRef(el)
    },
    [dropRef],
  )

  // Latest values for the once-attached window listeners.
  const rowsRef = useRef(rows)
  rowsRef.current = rows
  const selectionRef = useRef(selection)
  selectionRef.current = selection
  const reorderRef = useRef(reorder)
  reorderRef.current = reorder
  const dragFromRef = useRef(dragFrom)
  dragFromRef.current = dragFrom

  // Keyboard: ↑/↓ move the selection (Shift extends it), Cmd/Ctrl+A selects
  // every visible row, Esc clears, Delete/Backspace removes (where the view
  // allows it), Enter loads the highlighted row into the deck and plays it.
  // Read through a ref so the listener is attached once.
  // ←/→, Space, C and digits belong to the deck (PrepStrip).
  const keysRef = useRef<(e: KeyboardEvent) => void>(() => {})
  keysRef.current = (e) => {
    if (!selection || rows.length === 0) return
    const t = e.target as HTMLElement | null
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return
    // A dialog or menu on top owns the keyboard (and its own Esc).
    if (document.querySelector('[aria-modal="true"], [role="menu"]')) return
    if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && e.code === 'KeyA') {
      e.preventDefault()
      selection.onChange(new Set(rows.map((r) => r.original.id)))
      return
    }
    if (e.metaKey || e.ctrlKey || e.altKey) return
    if (e.key === 'Escape') {
      if (selection.selected.size > 0) selection.onChange(new Set())
      return
    }
    if ((e.key === 'Delete' || e.key === 'Backspace') && onRemove && selection.selected.size > 0) {
      e.preventDefault()
      onRemove([...selection.selected])
      selection.onChange(new Set())
      return
    }
    if (e.key === 'Enter' && onPlay) {
      // The highlighted row is the lead — the one last clicked or arrowed to,
      // which in a multi-selection is the row the cursor is on.
      const lead = indexOf(leadRef.current)
      if (lead < 0 || !selection.selected.has(rows[lead].original.id)) return
      // Wins over a focused button, as Space does for the deck: after clicking
      // a playlist and arrowing down, Enter must not re-press that playlist.
      e.preventDefault()
      if (!e.repeat) onPlay(rows[lead].original)
      return
    }
    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return
    e.preventDefault()
    const lead = indexOf(leadRef.current)
    const step = e.key === 'ArrowDown' ? 1 : -1
    const next = lead < 0 ? 0 : Math.max(0, Math.min(rows.length - 1, lead + step))
    const anchor = indexOf(anchorRef.current)
    if (e.shiftKey && anchor >= 0) {
      leadRef.current = rows[next].original.id
      selection.onChange(new Set(rangeIds(anchor, next)))
    } else {
      selectOnly(next)
    }
    virtualizer.scrollToIndex(next)
  }
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => keysRef.current(e)
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  return (
    <div
      ref={scrollerRef}
      data-drag-scroll={bodyOffset.header}
      className="h-full overflow-auto px-1.5"
      onClick={(e) => {
        // A click in the empty space below the rows clears the selection.
        const t = e.target as HTMLElement
        if (selection && !t.closest('[data-row]') && !t.closest('.sticky')) selection.onChange(new Set())
      }}
    >
      <div style={{ width: totalWidth, minWidth: '100%' }}>
        <HeaderRow
          headers={headers}
          sensors={sensors}
          onColumnDragEnd={onColumnDragEnd}
          hasPlay={hasPlay}
          onContextMenu={onHeaderContextMenu}
        />
        <div ref={bodyRef} style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>
          {virtualRows.map((vr) => {
            const row = rows[vr.index]
            const isSelected = selection?.selected.has(row.original.id) ?? false
            const isActive = activeTrackId === row.original.id
            const isMoving = movingIds.has(row.original.id)
            return (
              <div
                key={row.id}
                data-row
                className={`group absolute left-0 flex items-center border-b border-ink-850 text-sm ${
                  isSelected ? 'bg-accent-soft/50' : dragging ? '' : 'hover:bg-ink-850'
                } ${isMoving ? 'opacity-40' : ''}`}
                style={{ top: 0, transform: `translateY(${vr.start - bodyOffset.top}px)`, height: vr.size, width: '100%' }}
                onMouseDown={(e) => {
                  // Shift/Cmd-click would otherwise also select page text.
                  if (selection && (e.shiftKey || e.metaKey || e.ctrlKey)) e.preventDefault()
                }}
                onPointerDown={(e) => {
                  if (e.button !== 0 || e.shiftKey || e.metaKey || e.ctrlKey) return
                  // Not from a control inside the row (play, stars, an edit field).
                  if ((e.target as HTMLElement).closest('button, input, textarea, select, a')) return
                  pressRef.current = { x: e.clientX, y: e.clientY, index: vr.index }
                }}
                onClick={(e) => selection && clickRow(vr.index, e)}
                onContextMenu={(e) => {
                  if (!onRowContextMenu) return
                  e.preventDefault()
                  // Right-clicking outside the selection retargets it, so the
                  // menu always acts on what is highlighted.
                  if (selection && !isSelected) selectOnly(vr.index)
                  onRowContextMenu(row.original, e.clientX, e.clientY)
                }}
              >
                <span className="w-10 shrink-0 pr-2 text-right text-xs tabular-nums text-faint">
                  {marked?.ids.has(row.original.id) ? (
                    <span title={marked.title} className="inline-flex text-mint">
                      <Icon name="check" size={13} />
                    </span>
                  ) : (
                    (positions?.get(row.original.id) ?? vr.index + 1)
                  )}
                </span>
                {hasPlay && (
                  <span className="flex w-9 shrink-0 items-center justify-center">
                    {onPlay && <PlayButton
                        track={row.original}
                        isActive={isActive}
                        isPlaying={isActive && activePlaying}
                        onPlay={onPlay}
                      />}
                  </span>
                )}
                <RowCells row={row} />
              </div>
            )
          })}
          {dropHint && (
            <div
              className="pointer-events-none absolute left-0 z-10 h-0.5 w-full bg-accent"
              style={{ top: dropHint.drop * ROW_HEIGHT - 1 }}
            />
          )}
        </div>
      </div>
    </div>
  )
}
