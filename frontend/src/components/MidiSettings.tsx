import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { ACTION_GROUPS, ACTIONS, type ActionDef } from '../lib/midi/actions'
import { ENCODING_LABELS, ENCODINGS } from '../lib/midi/encoding'
import {
  DEFAULT_JOG_SENSITIVITY,
  DEFAULT_SCRUB_SPEED,
  emptyMapping,
  sanitizeMapping,
  setBinding,
  sourcesFor,
  unbind,
  type Layer,
  type Mapping,
} from '../lib/midi/mapping'
import { describeSource, splitSource, type SourceKey } from '../lib/midi/messages'
import { armLearn, replaceMappings, setOnScreenLearn, updateMapping, useMidiState, type LearnTarget } from '../lib/midi/store'
import { askConfirm } from '../lib/confirm'
import { ContextMenu, type MenuItem } from './ContextMenu'
import { FileBrowser } from './FileBrowser'
import { FolderPicker } from './FolderPicker'
import { Modal } from './Modal'

const section = 'text-[11px] font-semibold uppercase tracking-wider text-faint'
const MAPPING_SUFFIX = '.konduktor-midi.json'

const sameTarget = (a: LearnTarget | null, b: LearnTarget) =>
  a != null && a.action === b.action && ('layer' in a ? a.layer : null) === ('layer' in b ? b.layer : null)

/**
 * Settings ▸ MIDI Controllers… (decided 2026-11-03): every controller is
 * mapped by LEARNING — press Learn beside an action (or "Learn on screen" and
 * click the control), then move the control. Each control has a normal and a
 * free Shift binding; Shift itself is a learned control. Mappings are per
 * controller, kept in userprefs, and shareable as files.
 */
export function MidiSettings({ onClose }: { onClose: () => void }) {
  const available = useMidiState((s) => s.available)
  const blocked = useMidiState((s) => s.blocked)
  const ports = useMidiState((s) => s.ports)
  const mappings = useMidiState((s) => s.mappings)
  const armed = useMidiState((s) => s.armed)
  const activity = useMidiState((s) => s.activity)
  const learned = useMidiState((s) => s.learned)
  const known = useMemo(() => [...new Set([...ports, ...Object.keys(mappings)])], [ports, mappings])
  const [selected, setSelected] = useState<string | null>(null)
  const port = selected && known.includes(selected) ? selected : (known[0] ?? null)
  const mapping: Mapping = (port && mappings[port]) || emptyMapping()
  const [filter, setFilter] = useState('')
  const [menu, setMenu] = useState<{ x: number; y: number; items: MenuItem[] } | null>(null)
  const [exporting, setExporting] = useState(false)
  const [importing, setImporting] = useState(false)
  const [note, setNote] = useState<{ error: boolean; text: string } | null>(null)

  // What was just learned shows under its own controller.
  useEffect(() => {
    if (learned) setSelected(learned.port)
  }, [learned])
  // Closing the dialog stops waiting for a control.
  useEffect(() => () => armLearn(null), [])

  const learn = (t: LearnTarget) => armLearn(sameTarget(armed, t) ? null : t)
  const edit = (fn: (m: Mapping) => Mapping) => port && updateMapping(port, fn)

  const bindingMenu = (e: React.MouseEvent, src: SourceKey, action: ActionDef, layer: Layer) => {
    const b = mapping.bindings[src]
    if (!b) return
    const r = e.currentTarget.getBoundingClientRect()
    const items: MenuItem[] = [{ heading: describeSource(src) }]
    if (splitSource(src).dir) {
      // An encoder turned for a button action: how it counts its clicks.
      items.push(
        {
          label: 'Encoding',
          submenu: ENCODINGS.filter((x) => x !== 'button' && x !== 'abs7' && x !== 'abs14').map((x) => ({
            label: ENCODING_LABELS[x],
            hint: x === b.encoding ? '✓' : undefined,
            onClick: () => edit((m) => setBinding(m, src, { encoding: x })),
          })),
        },
        { separator: true },
      )
    } else if (action.kind !== 'button' && action.kind !== 'hold') {
      items.push(
        {
          label: 'Encoding',
          submenu: ENCODINGS.filter((x) => x !== 'button').map((x) => ({
            label: ENCODING_LABELS[x],
            hint: x === b.encoding ? '✓' : undefined,
            onClick: () => edit((m) => setBinding(m, src, { encoding: x })),
          })),
        },
        { label: 'Reversed', checked: !!b.invert, onToggle: () => edit((m) => setBinding(m, src, { invert: !b.invert || undefined })) },
        { separator: true },
      )
    }
    items.push(
      { label: 'Learn again', onClick: () => learn({ action: action.id, layer }) },
      { label: 'Clear', danger: true, onClick: () => edit((m) => unbind(m, action.id, layer)) },
    )
    setMenu({ x: r.left, y: r.bottom + 4, items })
  }

  const cell = (action: ActionDef, layer: Layer) => {
    const t: LearnTarget = { action: action.id, layer }
    if (sameTarget(armed, t)) {
      return (
        <button onClick={() => armLearn(null)} className="w-full animate-pulse rounded-md bg-accent/25 px-2 py-1 text-left text-[11px] text-text ring-1 ring-accent">
          Move a control…
        </button>
      )
    }
    const sources = sourcesFor(mapping, action.id, layer)
    if (sources.length === 0) {
      return (
        <button onClick={() => learn(t)} className="w-full rounded-md px-2 py-1 text-left text-[11px] text-faint hover:bg-ink-800 hover:text-text">
          Learn
        </button>
      )
    }
    return (
      <div className="flex flex-wrap gap-1">
        {sources.map((src) => {
          const enc = mapping.bindings[src]?.encoding
          return (
            <button
              key={src}
              onClick={(e) => bindingMenu(e, src, action, layer)}
              title={enc && enc !== 'button' ? ENCODING_LABELS[enc] : undefined}
              className="rounded-md bg-ink-800 px-2 py-1 font-mono text-[10.5px] text-text hover:bg-ink-700"
            >
              {describeSource(src)}
              {mapping.bindings[src]?.invert ? ' ⇄' : ''}
            </button>
          )
        })}
      </div>
    )
  }

  const needle = filter.trim().toLowerCase()
  const shown = ACTIONS.filter((a) => !needle || a.label.toLowerCase().includes(needle) || a.group.toLowerCase().includes(needle))

  const forget = async () => {
    if (!port) return
    const ok = await askConfirm({
      title: `Forget ${port}'s mapping?`,
      body: 'Every learned control on this controller is cleared.',
      confirmLabel: 'Forget',
    })
    if (!ok) return
    const next = { ...mappings }
    delete next[port]
    replaceMappings(next)
  }

  const doExport = async (dir: string) => {
    if (!port) return
    try {
      const { path } = await api.midiExport(dir, port, mapping)
      setNote({ error: false, text: `Saved ${path}` })
    } catch (e) {
      setNote({ error: true, text: (e as Error).message })
    }
  }
  const doImport = async (path: string) => {
    setImporting(false)
    if (!port) return
    try {
      const m = sanitizeMapping((await api.midiImport(path)).mapping)
      if (!m) throw new Error('That file is not a MIDI mapping')
      if (Object.keys(mapping.bindings).length > 0 || mapping.shift) {
        const ok = await askConfirm({
          title: `Replace ${port}'s mapping?`,
          body: 'The imported mapping replaces every control learned on this controller.',
          confirmLabel: 'Replace',
        })
        if (!ok) return
      }
      updateMapping(port, () => m)
      setNote({ error: false, text: `Imported ${Object.keys(m.bindings).length} controls` })
    } catch (e) {
      setNote({ error: true, text: (e as Error).message })
    }
  }

  return (
    <Modal onDismiss={armed ? () => armLearn(null) : onClose}>
      <div className="flex h-[80vh] w-full max-w-3xl flex-col overflow-hidden glass-overlay">
        <div className="flex items-center justify-between border-b border-line px-5 py-3">
          <h2 className="text-sm font-semibold text-text">MIDI Controllers</h2>
          <button onClick={onClose} className="rounded px-1.5 text-faint hover:text-text" aria-label="Close">
            ×
          </button>
        </div>

        {!available ? (
          <div className="px-5 py-6 text-sm text-muted">
            MIDI is not available here. The desktop app reads controllers itself; in a browser, use Chrome.
          </div>
        ) : blocked && known.length === 0 ? (
          <div className="space-y-1 px-5 py-6 text-sm text-muted">
            <p>The browser refused access to MIDI devices.</p>
            <p className="text-xs text-faint">
              Allow it from the site settings (the icon left of the address: MIDI devices → Allow). Konduktor asks again on
              its own. ({blocked})
            </p>
          </div>
        ) : known.length === 0 ? (
          <div className="px-5 py-6 text-sm text-muted">Plug in a MIDI controller — it shows up here within a couple of seconds.</div>
        ) : (
          <>
            <div className="space-y-3 border-b border-line px-5 py-3 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <select
                  value={port ?? ''}
                  onChange={(e) => setSelected(e.target.value)}
                  className="min-w-0 max-w-[16rem] rounded-lg well px-2 py-1 text-sm text-text outline-none"
                >
                  {known.map((p) => (
                    <option key={p} value={p}>
                      {p}
                      {ports.includes(p) ? '' : ' (not connected)'}
                    </option>
                  ))}
                </select>
                <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-faint">
                  {activity
                    ? `${activity.port}: ${describeSource(activity.source)} → ${activity.value}`
                    : 'Move a control to see it here'}
                </span>
                <button
                  onClick={() => {
                    setOnScreenLearn(true)
                    onClose()
                  }}
                  className="btn-glass rounded-full px-3 py-1 text-xs text-text"
                  title="Click a control on screen, then move it on your controller"
                >
                  Learn on screen
                </button>
                <button onClick={() => setImporting(true)} className="btn-glass rounded-full px-3 py-1 text-xs text-muted hover:text-text">
                  Import…
                </button>
                <button onClick={() => setExporting(true)} className="btn-glass rounded-full px-3 py-1 text-xs text-muted hover:text-text">
                  Export…
                </button>
                {mappings[port ?? ''] && (
                  <button onClick={() => void forget()} className="rounded-full px-2 py-1 text-xs text-faint hover:text-pink">
                    Forget
                  </button>
                )}
              </div>
              {note && <div className={`text-xs ${note.error ? 'text-pink' : 'text-faint'}`}>{note.text}</div>}
              <div className="grid grid-cols-[auto_1fr] items-center gap-x-4 gap-y-2">
                <span className="text-text">Shift button</span>
                <div className="flex items-center gap-2">
                  {sameTarget(armed, { action: 'shift' }) ? (
                    <button onClick={() => armLearn(null)} className="animate-pulse rounded-md bg-accent/25 px-2 py-1 text-[11px] text-text ring-1 ring-accent">
                      Press the controller's Shift…
                    </button>
                  ) : mapping.shift ? (
                    <>
                      <span className="rounded-md bg-ink-800 px-2 py-1 font-mono text-[10.5px] text-text">{describeSource(mapping.shift)}</span>
                      <button onClick={() => learn({ action: 'shift' })} className="text-[11px] text-faint hover:text-text">
                        Learn again
                      </button>
                      <button onClick={() => edit((m) => ({ ...m, shift: null }))} className="text-[11px] text-faint hover:text-pink">
                        Clear
                      </button>
                    </>
                  ) : (
                    <button onClick={() => learn({ action: 'shift' })} className="rounded-md px-2 py-1 text-[11px] text-faint hover:bg-ink-800 hover:text-text">
                      Learn
                    </button>
                  )}
                  <span className="text-[11px] text-faint">Hold it while moving a control to learn its Shift action.</span>
                </div>
                <span className="text-text">Jog sensitivity</span>
                <Slider
                  value={mapping.jogSensitivity}
                  min={0.05}
                  max={4}
                  step={0.05}
                  reset={DEFAULT_JOG_SENSITIVITY}
                  format={(v) => `×${v.toFixed(2)}`}
                  onChange={(v) => edit((m) => ({ ...m, jogSensitivity: v }))}
                />
                <span className="text-text">Fast scrub</span>
                <Slider
                  value={mapping.scrubSpeed}
                  min={2}
                  max={32}
                  step={1}
                  reset={DEFAULT_SCRUB_SPEED}
                  format={(v) => `×${v} a jog`}
                  onChange={(v) => edit((m) => ({ ...m, scrubSpeed: v }))}
                />
              </div>
            </div>

            <div className="flex items-center gap-3 border-b border-line px-5 py-2">
              <input
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="Find an action…"
                className="w-56 rounded-md well px-2 py-1 text-xs text-text outline-none focus:ring-1 focus:ring-accent"
              />
              <span className="min-w-0 flex-1 truncate text-[11px] text-faint" title="A button action can be an encoder turned one way: learn it by turning a few clicks in that direction (↻ / ↺).">
                Buttons: press it, or turn an encoder a few clicks in this action's direction.
              </span>
              <span className="w-36 text-[11px] font-semibold uppercase tracking-wider text-faint">Control</span>
              <span className="w-36 text-[11px] font-semibold uppercase tracking-wider text-faint">With Shift</span>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-4">
              {ACTION_GROUPS.map((g) => {
                const rows = shown.filter((a) => a.group === g)
                if (rows.length === 0) return null
                return (
                  <section key={g} className="pt-3">
                    <h3 className={`${section} pb-1`}>
                      {g}
                      {g === 'Deck B' || g === 'Mixer' ? <span className="ml-2 normal-case tracking-normal">— performance mode only</span> : null}
                    </h3>
                    {rows.map((a) => (
                      <div key={a.id} className="flex items-center gap-3 border-b border-line/40 py-1">
                        <span className="min-w-0 flex-1 truncate text-sm text-text">{a.label}</span>
                        <div className="w-36 shrink-0">{cell(a, 'normal')}</div>
                        <div className="w-36 shrink-0">{cell(a, 'shift')}</div>
                      </div>
                    ))}
                  </section>
                )
              })}
            </div>
          </>
        )}
      </div>
      {menu && <ContextMenu {...menu} onClose={() => setMenu(null)} />}
      {exporting && <FolderPicker value="" onChange={(dir) => void doExport(dir)} onClose={() => setExporting(false)} />}
      {importing && <ImportPicker onPick={(p) => void doImport(p)} onClose={() => setImporting(false)} />}
    </Modal>
  )
}

function Slider({ value, min, max, step, reset, format, onChange }: {
  value: number; min: number; max: number; step: number; reset: number; format: (v: number) => string; onChange: (v: number) => void
}) {
  return (
    <div className="flex items-center gap-3">
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        onDoubleClick={() => onChange(reset)}
        className="w-48"
      />
      <span className="font-mono text-[11px] text-muted">{format(value)}</span>
    </div>
  )
}

/** Pick a `.konduktor-midi.json` file. */
function ImportPicker({ onPick, onClose }: { onPick: (path: string) => void; onClose: () => void }) {
  const [dir, setDir] = useState<string | undefined>(undefined)
  return (
    <Modal onDismiss={onClose} layer="nested" padding="p-4">
      <div className="flex h-[70vh] w-full max-w-2xl flex-col overflow-hidden glass-overlay">
        <div className="border-b border-line px-5 py-3">
          <h2 className="text-sm font-semibold text-text">Import a MIDI mapping</h2>
        </div>
        <FileBrowser mode="file" suffix={MAPPING_SUFFIX} path={dir} onNavigate={setDir} onPickFile={onPick} />
        <div className="flex justify-end border-t border-line px-5 py-3">
          <button onClick={onClose} className="btn-glass rounded-full px-3 py-1.5 text-sm text-muted hover:text-text">
            Cancel
          </button>
        </div>
      </div>
    </Modal>
  )
}
