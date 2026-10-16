import type { CuePoint } from '../api'
import { cueColor, cueIcon } from '../lib/cues'
import { Icon } from '../lib/icons'
import { lighten, padStyle } from './HotcueBar'
import { RenameField } from './RenameField'

interface Props {
  /** The track's cues; only the memory cues are shown, in time order. */
  cues: CuePoint[]
  /** The deck's CUE point, so the memory cue it sits on reads as selected. */
  cuePoint: number
  onCall: (cue: CuePoint) => void
  /** Right-click: the deck opens its Rename / Colour / Delete menu. */
  onMenu: (cue: CuePoint, x: number, y: number) => void
  onAdd: () => void
  /** Why + MEMORY cannot add one (read-only library, the platform's cap), or null. */
  addRefusal: string | null
  /** The memory cue whose name is being edited in place, if any. */
  renamingId: string | null
  onRenameCommit: (cue: CuePoint, name: string) => void
  onRenameCancel: () => void
}

/** `m:ss.s` — a chip's label when the cue has no name. */
function shortTime(secs: number): string {
  const m = Math.floor(secs / 60)
  const s = secs - m * 60
  return `${m}:${s.toFixed(1).padStart(4, '0')}`
}

/** `m:ss.mmm` — the full position, for the chip's tooltip. */
function fullTime(secs: number): string {
  const m = Math.floor(secs / 60)
  const s = secs - m * 60
  return `${m}:${s.toFixed(3).padStart(6, '0')}`
}

const named = (cue: CuePoint) => !!cue.name && cue.name !== 'n.n.'

/**
 * The memory-cue bank: what the hot cue pads become in Memory mode. Memory cues
 * have no pads, so this is + Memory, then a strip of chips in TIME order,
 * scrolling sideways when it outgrows the row.
 *
 * Clicking a chip CALLS the cue, CDJ-style: the deck jumps there and makes it
 * the CUE point (a memory loop also engages) — unlike a hot cue pad, it is not
 * a trigger to perform with. Right-click edits it, as on a pad.
 */
export function MemoryCueStrip({
  cues,
  cuePoint,
  onCall,
  onMenu,
  onAdd,
  addRefusal,
  renamingId,
  onRenameCommit,
  onRenameCancel,
}: Props) {
  const memory = cues.filter((c) => c.role === 'memory').sort((a, b) => a.start - b.start)
  return (
    // At least one chip always shows (~6rem): the row's breakpoints fold the
    // tempo group before the strip gets that narrow.
    <div className="flex min-w-[12rem] flex-1 items-center gap-1.5">
      <button
        type="button"
        onClick={onAdd}
        aria-disabled={addRefusal != null}
        title={addRefusal ?? 'Store a memory cue at the playhead (a memory loop when a loop is on)'}
        className={
          'btn-glass flex h-11 shrink-0 items-center gap-1 rounded-xl px-2.5 text-[11px] font-semibold ' +
          (addRefusal != null ? 'cursor-not-allowed opacity-45' : '')
        }
      >
        <Icon name="plus" size={13} strokeWidth={2.2} />
        Memory
      </button>
      <div
        className="flex min-w-[5.5rem] flex-1 items-center gap-1.5 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
        // A vertical wheel scrolls the strip sideways: a mouse has no other way.
        onWheel={(e) => {
          if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) e.currentTarget.scrollLeft += e.deltaY
        }}
      >
        {memory.length === 0 && (
          <span className="px-1 text-[11px] text-faint">
            No memory cues — + Memory stores one at the playhead
          </span>
        )}
        {memory.map((cue) => {
          const color = cueColor(cue)
          const selected = Math.abs(cue.start - cuePoint) < 0.0015
          const locked = !cue.editable
          const key = cue.id ?? `${cue.start}:${cue.type}`
          return (
            <button
              key={key}
              type="button"
              onClick={() => onCall(cue)}
              onContextMenu={(e) => {
                e.preventDefault()
                onMenu(cue, e.clientX, e.clientY)
              }}
              title={`${named(cue) ? `${cue.name} · ` : ''}${fullTime(cue.start)}${
                cue.type === 'loop' ? ` · loop ${cue.length.toFixed(2)} s` : ''
              }${locked ? ' · not editable here' : ''}`}
              style={padStyle(color, selected)}
              className="flex h-11 max-w-[9rem] shrink-0 items-center gap-1.5 rounded-xl px-2.5 text-left transition-[filter] hover:brightness-115"
            >
              <span className="flex shrink-0" style={{ color: lighten(color) }}>
                <Icon name={locked ? 'lock' : cue.type === 'loop' ? 'loop' : cueIcon(cue)} size={13} strokeWidth={2.1} />
              </span>
              {renamingId != null && renamingId === cue.id ? (
                <RenameField
                  initial={named(cue) ? cue.name! : ''}
                  onCommit={(name) => onRenameCommit(cue, name)}
                  onCancel={onRenameCancel}
                />
              ) : (
                <span
                  className={`truncate text-[11px] leading-none ${named(cue) ? 'text-text' : 'font-mono text-text/80'}`}
                >
                  {named(cue) ? cue.name : shortTime(cue.start)}
                </span>
              )}
            </button>
          )
        })}
      </div>
    </div>
  )
}
