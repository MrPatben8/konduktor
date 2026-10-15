import { createPortal } from 'react-dom'
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { useCaps } from '../lib/capabilities'
import { Icon } from '../lib/icons'
import {
  ARROW_GAP,
  ARROW_KEYS,
  AREA_COLORS,
  AREAS,
  KEY_ROWS,
  KEYBOARD_UNITS,
  KEYS_BY_CODE,
  LAYER_NAMES,
  LAYERS,
  MODIFIER_KEY,
  MOUSE,
  bindings,
  modifierLayer,
  padRange,
  unusedNote,
  type Area,
  type Binding,
  type KeyDef,
  type Layer,
  type MouseGesture,
} from '../lib/shortcuts'

interface Props {
  onClose: () => void
}

/** Key unit bounds, px. The map shrinks to fit the dialog; below LABEL_MIN the
 *  words on the keys go (the caption still explains each key), below SHORT_MIN
 *  the long modifier legends become symbols. */
const UNIT_MAX = 52
const UNIT_MIN = 20
const LABEL_MIN = 44
const SHORT_MIN = 38

const ALL_AREAS = Object.keys(AREAS) as Area[]
const matchText = (b: Binding) => `${b.text} ${b.label}`.toLowerCase()
const mouseText = (r: MouseGesture) => `${r.gesture} ${r.on ?? ''} ${r.text}`.toLowerCase()

/** Every shortcut, drawn on a keyboard: a key lights in the colour of what it
 *  does, pointing at (or pressing) it explains it, and holding a modifier — or
 *  picking one above — relights the map with that layer. Mouse gestures are
 *  listed beneath. The content lives in `lib/shortcuts.ts`. */
export function ShortcutsDialog({ onClose }: Props) {
  const slots = useCaps().cues.hotcue_slots
  const binds = useMemo(() => bindings(slots), [slots])

  const [sticky, setSticky] = useState<Layer>('base')
  const [held, setHeld] = useState<ReadonlySet<Layer>>(new Set())
  const [hover, setHover] = useState<string | null>(null)
  const [pressed, setPressed] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [areas, setAreas] = useState<ReadonlySet<Area>>(new Set(ALL_AREAS))
  const layer: Layer = held.has('mod') ? 'mod' : held.has('shift') ? 'shift' : sticky
  const q = query.trim().toLowerCase()

  // ---- fit: one key unit from the width the map has ----
  const fitRef = useRef<HTMLDivElement>(null)
  const [unit, setUnit] = useState(UNIT_MAX)
  useLayoutEffect(() => {
    const el = fitRef.current
    if (!el) return
    const fit = () => {
      const u = Math.floor(el.clientWidth / KEYBOARD_UNITS)
      setUnit(Math.max(UNIT_MIN, Math.min(UNIT_MAX, u)))
    }
    fit()
    const ro = new ResizeObserver(fit)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // ---- keyboard: pressing a key explains it rather than doing it ----
  // Capture phase on window, so the deck's and the table's own listeners (which
  // are on window too, bubbling) never see a key pressed in here.
  const queryRef = useRef(query)
  queryRef.current = query
  useEffect(() => {
    let release: number | undefined
    const onDown = (e: KeyboardEvent) => {
      const inField = (e.target as HTMLElement | null)?.tagName === 'INPUT'
      if (e.key === 'Escape') {
        e.preventDefault()
        e.stopPropagation()
        if (inField && queryRef.current) setQuery('')
        else onClose()
        return
      }
      if (inField) return
      const mod = modifierLayer(e.code)
      if (mod) {
        setHeld((h) => new Set(h).add(mod))
        return
      }
      if (e.code === 'Tab' || !KEYS_BY_CODE[e.code]) return
      e.stopPropagation()
      // Space / Enter still press a focused control (a layer tab, a modifier key).
      const t = e.target as HTMLElement | null
      if ((e.code === 'Space' || e.code === 'Enter') && t?.tagName === 'BUTTON' && !t.dataset.mapKey) return
      e.preventDefault()
      setHover(e.code)
      setPressed(e.code)
      window.clearTimeout(release)
      release = window.setTimeout(() => setPressed(null), 160)
    }
    const onUp = (e: KeyboardEvent) => {
      const mod = modifierLayer(e.code)
      if (mod) {
        setHeld((h) => {
          const next = new Set(h)
          next.delete(mod)
          return next
        })
      } else if (KEYS_BY_CODE[e.code] && (e.target as HTMLElement | null)?.tagName !== 'INPUT') {
        e.stopPropagation() // the deck's keyup ends a held cue/hotcue preview
      }
    }
    const onBlur = () => setHeld(new Set())
    window.addEventListener('keydown', onDown, true)
    window.addEventListener('keyup', onUp, true)
    window.addEventListener('blur', onBlur)
    return () => {
      window.clearTimeout(release)
      window.removeEventListener('keydown', onDown, true)
      window.removeEventListener('keyup', onUp, true)
      window.removeEventListener('blur', onBlur)
    }
  }, [onClose])

  const visible = (l: Layer, code: string) => (binds[l][code] ?? []).filter((b) => areas.has(b.area))
  const layerCount = (l: Layer) =>
    Object.values(binds[l]).flat().filter((b) => areas.has(b.area) && (!q || matchText(b).includes(q))).length +
    MOUSE.flatMap((c) => c.rows).filter((r) => (r.mod ?? 'base') === l && (!q || mouseText(r).includes(q))).length

  const pickLayer = (l: Layer) => setSticky((s) => (s === l && l !== 'base' ? 'base' : l))
  const toggleArea = (a: Area) =>
    setAreas((cur) => {
      const next = new Set(cur)
      if (next.has(a) && next.size > 1) next.delete(a)
      else next.add(a)
      return next
    })

  const gap = Math.max(2, Math.round(unit / 10))
  const keyProps = {
    unit,
    gap,
    labels: unit >= LABEL_MIN,
    short: unit < SHORT_MIN,
    layer,
    hover,
    pressed,
    q,
    visible,
    noted: (code: string) => !!unusedNote(code, slots),
    bound: (code: string) => LAYERS.some((l) => binds[l][code]),
    onHover: setHover,
    onPick: pickLayer,
  }

  return createPortal(
    <div
      aria-modal="true"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-[3px] p-6"
      onClick={onClose}
    >
      <div
        className="flex max-h-full w-full max-w-[960px] flex-col overflow-hidden glass-overlay"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex flex-wrap items-center gap-3 border-b border-line py-3.5 pl-5 pr-4">
          <div className="text-[15px] font-semibold tracking-tight">Keyboard Shortcuts</div>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search shortcuts"
            aria-label="Search shortcuts"
            className="well ml-auto w-64 min-w-0 rounded-[9px] border border-line px-2.5 py-1.5 text-[13px] text-text outline-none"
          />
          <button
            onClick={onClose}
            aria-label="Close"
            className="h-7 w-7 rounded-md text-lg leading-none text-muted hover:bg-ink-800 hover:text-text"
          >
            ×
          </button>
        </div>

        <div className="overflow-y-auto">
          <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2.5 px-5 pb-1 pt-3.5">
            <div className="well inline-flex flex-wrap gap-0.5 rounded-[10px] p-[3px]">
              {LAYERS.map((l) => (
                <button
                  key={l}
                  onClick={() => setSticky(l)}
                  aria-pressed={l === layer}
                  className={`inline-flex items-baseline gap-1.5 rounded-[7px] px-2.5 py-1 text-xs font-medium ${
                    l === layer ? 'bg-ink-600 text-text' : 'text-muted hover:text-text'
                  }`}
                >
                  {LAYER_NAMES[l]}
                  <span className="font-mono text-[10px] text-faint">{layerCount(l)}</span>
                </button>
              ))}
            </div>
            <div className="flex flex-wrap gap-1.5">
              {ALL_AREAS.map((a) => (
                <button
                  key={a}
                  onClick={() => toggleArea(a)}
                  aria-pressed={areas.has(a)}
                  className={`inline-flex items-center gap-1.5 rounded-full border border-line py-0.5 pl-2 pr-2.5 text-[11.5px] font-medium text-muted ${
                    areas.has(a) ? '' : 'opacity-40'
                  }`}
                >
                  <i
                    className="h-[7px] w-[7px] rounded-full"
                    style={{ background: AREA_COLORS[a], boxShadow: areas.has(a) ? `0 0 8px ${AREA_COLORS[a]}` : undefined }}
                  />
                  {AREAS[a]}
                </button>
              ))}
            </div>
          </div>

          <div ref={fitRef} className="mx-5 mb-2.5 mt-4">
            <div
              className="mx-auto flex w-max items-end"
              style={{ gap: unit * ARROW_GAP }}
              onPointerLeave={() => setHover(null)}
            >
              <div className="flex flex-col" style={{ gap }}>
                {KEY_ROWS.map((row, i) => (
                  <div key={i} className="flex" style={{ gap, marginBottom: i === 0 ? gap * 0.4 : 0 }}>
                    {row.map((k) => (
                      <Key key={k.code} def={k} fnRow={i === 0} {...keyProps} />
                    ))}
                  </div>
                ))}
              </div>
              <div className="grid" style={{ gap, gridTemplateColumns: `repeat(3, ${unit - gap}px)` }}>
                {ARROW_KEYS.map((k, i) =>
                  k ? <Key key={k.code} def={k} fnRow={false} {...keyProps} /> : <span key={i} />,
                )}
              </div>
            </div>
          </div>

          <div
            aria-live="polite"
            // A FIXED height, so the dialog does not resize under the pointer as
            // it moves between keys: tall enough for the fullest key (three
            // lines + "Also on this key"); search results scroll inside it.
            className="well mx-5 flex h-32 shrink-0 flex-col gap-[7px] overflow-y-auto rounded-xl px-3.5 py-3 text-[13px] text-muted"
          >
            <Caption
              hover={hover}
              layer={layer}
              q={q}
              query={query}
              slots={slots}
              binds={binds}
              areas={areas}
            />
          </div>

          <div className="px-5 pb-2 pt-5">
            <div className="mb-2.5 text-[10px] font-semibold uppercase tracking-wider text-faint">Mouse</div>
            <div className="grid grid-cols-[repeat(auto-fit,minmax(250px,1fr))] gap-x-7 gap-y-2">
              {MOUSE.map((col) => (
                <div key={col.title} className="min-w-0">
                  <div className="pb-1.5 text-xs font-semibold text-muted">{col.title}</div>
                  {col.rows.map((r, i) => {
                    const m = r.mod ?? 'base'
                    const hit = !q || mouseText(r).includes(q)
                    const lit = layer !== 'base' && m === layer && hit
                    const dim = (layer !== 'base' && m !== layer) || !hit
                    return (
                      <div
                        key={i}
                        className={`-mx-1.5 grid grid-cols-[minmax(8.5rem,auto)_1fr] items-baseline gap-2.5 rounded-md border-t px-1.5 py-1 text-[12.5px] transition-opacity ${
                          lit ? 'border-transparent bg-accent-soft text-text' : 'border-line text-muted'
                        } ${dim ? 'opacity-30' : ''}`}
                      >
                        <span className="inline-flex flex-wrap items-baseline gap-1">
                          {r.mod && <Kbd>{MODIFIER_KEY[r.mod]}</Kbd>}
                          <span className="inline-flex items-center gap-1 whitespace-nowrap rounded-full border border-dashed border-ink-600 py-px pl-1.5 pr-2 text-[11px] font-medium text-text">
                            <Icon name="mouse" size={11} strokeWidth={2} />
                            {r.gesture}
                            {r.on ? ` ${r.on}` : ''}
                          </span>
                        </span>
                        <span>{r.text}</span>
                      </div>
                    )
                  })}
                </div>
              ))}
            </div>
          </div>

          <div className="mt-2.5 border-t border-line px-5 py-3 text-xs text-faint">
            Press the <Kbd>?</Kbd> key anywhere to open this list.
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}

function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="min-w-[1.5em] rounded-[5px] border border-b-2 border-line bg-ink-800 px-1.5 py-px text-center font-mono text-[11px] text-text">
      {children}
    </kbd>
  )
}

function Combo({ layer, code }: { layer: Layer; code: string }) {
  const legend = KEYS_BY_CODE[code]?.legend ?? code
  return (
    <span className="inline-flex shrink-0 gap-[3px]">
      {layer !== 'base' && <Kbd>{MODIFIER_KEY[layer]}</Kbd>}
      <Kbd>{legend}</Kbd>
    </span>
  )
}

function Line({ layer, code, b, other }: { layer: Layer; code: string; b: Binding; other?: boolean }) {
  return (
    <div className={`flex flex-wrap items-baseline gap-2.5 ${other ? 'text-xs' : ''}`}>
      <Combo layer={layer} code={code} />
      <span className={`min-w-0 ${other ? 'text-muted' : 'text-text'}`}>{b.text}</span>
      <span className="text-[9.5px] font-semibold uppercase tracking-wider" style={{ color: AREA_COLORS[b.area] }}>
        {AREAS[b.area]}
      </span>
    </div>
  )
}

const Sub = ({ children }: { children: ReactNode }) => (
  <div className="mt-0.5 text-[10px] font-semibold uppercase tracking-wider text-faint">{children}</div>
)

function Caption({
  hover,
  layer,
  q,
  query,
  slots,
  binds,
  areas,
}: {
  hover: string | null
  layer: Layer
  q: string
  query: string
  slots: number
  binds: ReturnType<typeof bindings>
  areas: ReadonlySet<Area>
}) {
  if (hover) {
    const role = KEYS_BY_CODE[hover]?.role
    if (role) {
      const keys = Object.keys(binds[role]).length
      const mouse = MOUSE.flatMap((c) => c.rows).filter((r) => r.mod === role).length
      return (
        <div className="flex flex-wrap items-baseline gap-2.5">
          <Kbd>{MODIFIER_KEY[role]}</Kbd>
          <span className="text-text">
            Hold, or click, to light up what {LAYER_NAMES[role]} changes: {keys} key{keys === 1 ? '' : 's'} and{' '}
            {mouse} mouse gesture{mouse === 1 ? '' : 's'}.
          </span>
        </div>
      )
    }
    const here = binds[layer][hover] ?? []
    const others = LAYERS.filter((l) => l !== layer && binds[l][hover])
    return (
      <>
        {here.length ? (
          here.map((b, i) => <Line key={i} layer={layer} code={hover} b={b} />)
        ) : (
          <div className="flex flex-wrap items-baseline gap-2.5">
            <Combo layer={layer} code={hover} />
            <span>{unusedNote(hover, slots) ?? 'Does nothing in Konduktor.'}</span>
          </div>
        )}
        {others.length > 0 && <Sub>Also on this key</Sub>}
        {others.flatMap((l) => binds[l][hover].map((b, i) => <Line key={`${l}${i}`} layer={l} code={hover} b={b} other />))}
      </>
    )
  }

  if (q) {
    const hits = LAYERS.flatMap((l) =>
      Object.entries(binds[l]).flatMap(([code, bs]) =>
        bs
          .filter((b) => areas.has(b.area) && matchText(b).includes(q))
          .map((b, i) => <Line key={`${l}${code}${i}`} layer={l} code={code} b={b} />),
      ),
    )
    const mouse = MOUSE.flatMap((c) => c.rows).filter((r) => mouseText(r).includes(q)).length
    if (!hits.length && !mouse) return <span>No shortcut matches “{query.trim()}”.</span>
    return (
      <>
        {hits}
        {mouse > 0 && <Sub>{mouse} mouse gesture{mouse === 1 ? '' : 's'} below</Sub>}
      </>
    )
  }

  if (layer !== 'base') {
    return (
      <>
        <span className="text-text">
          Showing what {LAYER_NAMES[layer]} changes. The highlighted mouse gestures below use it too.
        </span>
        <span className="text-xs">Let go of the key, or pick “No modifier”, to go back.</span>
      </>
    )
  }
  return (
    <>
      <span className="text-text">Point at a key, or press it, to see what it does.</span>
      <span className="text-xs">
        Hold <Kbd>{MODIFIER_KEY.shift}</Kbd> or <Kbd>{MODIFIER_KEY.mod}</Kbd>, or pick one above, to see what each
        modifier changes.
      </span>
      <span className="text-xs">
        {padRange(slots)} are this library's {slots} hotcue pads; Q W E R are the stems of a stem track.
      </span>
    </>
  )
}

interface KeyProps {
  def: KeyDef
  fnRow: boolean
  unit: number
  gap: number
  labels: boolean
  short: boolean
  layer: Layer
  hover: string | null
  pressed: string | null
  q: string
  visible: (layer: Layer, code: string) => Binding[]
  noted: (code: string) => boolean
  bound: (code: string) => boolean
  onHover: (code: string | null) => void
  onPick: (layer: Layer) => void
}

function Key({ def, fnRow, unit, gap, labels, short, layer, hover, pressed, q, visible, noted, bound, onHover, onPick }: KeyProps) {
  const { code, role } = def
  const legend = short && def.short ? def.short : def.legend
  const ents = role ? [] : visible(layer, code)
  const first = ents[0]
  const kc = first ? (first.color ?? AREA_COLORS[first.area]) : 'var(--color-faint)'
  const on = role === layer
  const miss = !!first && !!q && !ents.some((b) => matchText(b).includes(q))
  const pad = first?.label === 'Pad' || first?.label === 'Delete'
  const interactive = !!role || bound(code) || noted(code)
  const inset = Math.max(2, Math.round(unit * 0.1))

  const style: CSSProperties = {
    width: (def.w ?? 1) * unit - gap,
    height: fnRow ? Math.round(unit * 0.72) : unit - gap,
    padding: `${Math.max(2, inset - 1)}px ${inset}px`,
    borderRadius: pad ? Math.round(unit * 0.18) : Math.round(unit * 0.14),
    borderColor: 'rgb(255 255 255 / 0.05)',
  }
  if (on) {
    Object.assign(style, {
      background: 'var(--color-accent)',
      borderColor: 'transparent',
      color: 'var(--color-ink-950)',
      boxShadow: '0 0 22px -4px var(--color-accent)',
    })
  } else if (role) {
    style.borderColor = 'rgb(255 255 255 / 0.12)'
  } else if (first) {
    Object.assign(style, {
      background: pressed === code
        ? `color-mix(in oklab, ${kc} 45%, transparent)`
        : `color-mix(in oklab, ${kc} ${pad ? 24 : 15}%, rgb(255 255 255 / 0.05))`,
      borderColor: hover === code
        ? `color-mix(in oklab, ${kc} 85%, white)`
        : `color-mix(in oklab, ${kc} 50%, transparent)`,
      boxShadow: `0 0 18px -6px color-mix(in oklab, ${kc} 70%, transparent), inset 0 1px 0 rgb(255 255 255 / 0.1)`,
    })
  } else if (hover === code) {
    style.borderColor = 'rgb(255 255 255 / 0.3)'
  }

  const legendSize = Math.max(8.5, Math.min(11, unit * 0.22)) - (role && !short ? 1 : 0)
  const body = (
    <>
      <span
        className={`whitespace-nowrap font-medium leading-none ${first || on ? (on ? '' : 'text-text') : ''}`}
        style={{ fontSize: legendSize }}
      >
        {legend}
      </span>
      {labels && first && (
        <span
          className="max-w-full overflow-hidden text-ellipsis whitespace-nowrap text-[9px] font-semibold leading-tight"
          style={{ color: kc }}
        >
          {first.label}
        </span>
      )}
      {ents.length > 1 && (
        <span className="absolute right-1 top-1 flex gap-0.5">
          {ents.map((b, i) => (
            <i key={i} className="h-[5px] w-[5px] rounded-full" style={{ background: AREA_COLORS[b.area] }} />
          ))}
        </span>
      )}
    </>
  )
  const className = `relative flex shrink-0 flex-col items-start justify-between border text-left transition-[background,border-color,opacity,transform,box-shadow] duration-150 ${
    first || on ? '' : 'bg-ink-850'
  } ${role && !on ? 'text-muted' : 'text-faint'} ${miss ? 'opacity-25' : ''} ${pressed === code ? 'scale-[0.96]' : ''}`

  if (!interactive) {
    return (
      <span className={className} style={style} onPointerEnter={() => onHover(def.legend ? code : null)}>
        {body}
      </span>
    )
  }
  return (
    <button
      type="button"
      className={`${className} ${role ? 'cursor-pointer' : 'cursor-help'}`}
      style={style}
      aria-label={first ? `${def.legend}: ${first.text}` : def.legend}
      aria-pressed={role ? on : undefined}
      data-map-key={role ? undefined : code}
      onPointerEnter={() => onHover(code)}
      onFocus={() => onHover(code)}
      onClick={() => role && onPick(role)}
    >
      {body}
    </button>
  )
}
