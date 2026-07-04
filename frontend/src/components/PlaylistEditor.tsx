import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  DndContext,
  closestCenter,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from '@dnd-kit/core'
import {
  SortableContext,
  useSortable,
  verticalListSortingStrategy,
  arrayMove,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { restrictToVerticalAxis, restrictToParentElement } from '@dnd-kit/modifiers'
import { api, type Track } from '../api'
import { formatBpm, formatDuration, keyColor } from '../lib/format'
import { RatingStars } from './RatingStars'

interface Props {
  uuid: string
  tracks: Track[]
  onError: (msg: string) => void
  onRowContextMenu?: (track: Track, x: number, y: number) => void
}

function Row({
  track,
  index,
  onRemove,
  onContextMenu,
}: {
  track: Track
  index: number
  onRemove: (id: string) => void
  onContextMenu?: (track: Track, x: number, y: number) => void
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } =
    useSortable({ id: track.id })

  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      className={`flex items-center border-b border-ink-850 text-sm ${
        isDragging ? 'z-10 bg-ink-800 shadow-lg' : 'hover:bg-ink-850'
      }`}
      onContextMenu={(e) => {
        if (!onContextMenu) return
        e.preventDefault()
        onContextMenu(track, e.clientX, e.clientY)
      }}
    >
      <button
        {...attributes}
        {...listeners}
        title="Drag to reorder"
        className="w-8 shrink-0 cursor-grab px-2 text-center text-faint hover:text-text active:cursor-grabbing"
      >
        ⠿
      </button>
      <span className="w-8 shrink-0 text-right text-xs tabular-nums text-faint">
        {index + 1}
      </span>
      <div className="min-w-0 flex-1 px-3 py-2">
        <div className="truncate font-medium text-text">
          {track.title || <span className="text-faint">Untitled</span>}
        </div>
        <div className="truncate text-xs text-muted">{track.artist}</div>
      </div>
      <div className="w-16 shrink-0 px-2 text-right tabular-nums">{formatBpm(track.bpm)}</div>
      <div className="w-14 shrink-0 px-2 text-center">
        {track.key ? (
          <span className="text-xs font-semibold" style={{ color: keyColor(track.key) }}>
            {track.key}
          </span>
        ) : (
          <span className="text-faint">—</span>
        )}
      </div>
      <div className="w-24 shrink-0 px-2">
        <RatingStars value={track.rating} />
      </div>
      <div className="w-14 shrink-0 px-2 text-right tabular-nums text-muted">
        {formatDuration(track.length)}
      </div>
      <button
        title="Remove from playlist"
        onClick={() => onRemove(track.id)}
        className="w-10 shrink-0 px-2 text-center text-faint hover:text-pink"
      >
        ×
      </button>
    </div>
  )
}

export function PlaylistEditor({ uuid, tracks, onError, onRowContextMenu }: Props) {
  const qc = useQueryClient()
  const [items, setItems] = useState<Track[]>(tracks)

  // Re-sync when the server data changes (initial load, or tracks added elsewhere).
  useEffect(() => setItems(tracks), [tracks])

  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 4 } }))

  const persist = useMutation({
    mutationFn: (ids: string[]) => api.setEntries(uuid, ids),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['state'] })
      qc.invalidateQueries({ queryKey: ['playlists'] }) // refresh counts
    },
    onError: (e: Error) => onError(e.message),
  })

  const commit = (next: Track[]) => {
    setItems(next)
    // Keep the cached playlist query in sync so navigating away/back is stable.
    qc.setQueryData(['playlist', uuid], next)
    persist.mutate(next.map((t) => t.id))
  }

  const onDragEnd = (e: DragEndEvent) => {
    const { active, over } = e
    if (!over || active.id === over.id) return
    const from = items.findIndex((t) => t.id === active.id)
    const to = items.findIndex((t) => t.id === over.id)
    if (from < 0 || to < 0) return
    commit(arrayMove(items, from, to))
  }

  const remove = (id: string) => commit(items.filter((t) => t.id !== id))

  if (items.length === 0) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 text-muted">
        <div className="text-lg">This playlist is empty</div>
        <div className="text-sm text-faint">
          Go to <span className="text-text">All Tracks</span>, select tracks, and add them here.
        </div>
      </div>
    )
  }

  return (
    <div className="h-full overflow-auto">
      <div className="sticky top-0 z-10 flex border-b border-line bg-ink-850 text-[11px] font-semibold uppercase tracking-wider text-muted">
        <span className="w-8 shrink-0" />
        <span className="w-8 shrink-0 text-right">#</span>
        <span className="flex-1 px-3 py-2.5">Title</span>
        <span className="w-16 shrink-0 px-2 text-right">BPM</span>
        <span className="w-14 shrink-0 px-2 text-center">Key</span>
        <span className="w-24 shrink-0 px-2">Rating</span>
        <span className="w-14 shrink-0 px-2 text-right">Time</span>
        <span className="w-10 shrink-0" />
      </div>
      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragEnd={onDragEnd}
        modifiers={[restrictToVerticalAxis, restrictToParentElement]}
      >
        <SortableContext items={items.map((t) => t.id)} strategy={verticalListSortingStrategy}>
          {items.map((t, i) => (
            <Row
              key={t.id}
              track={t}
              index={i}
              onRemove={remove}
              onContextMenu={onRowContextMenu}
            />
          ))}
        </SortableContext>
      </DndContext>
    </div>
  )
}
