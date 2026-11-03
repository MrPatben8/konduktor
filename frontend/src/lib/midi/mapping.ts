// A controller's mapping: which physical control does what, in the normal
// and the Shift layer. Keyed by SOURCE (the control), so two buttons may share
// an action; the editor looks the other way, action -> sources. One mapping
// per controller, keyed by its port name, kept in userprefs (`midiMappings`)
// and exported / imported as a file.

import { actionDef, shiftDefault } from './actions'
import { ENCODINGS, isFourteenBit, type Encoding } from './encoding'
import { parseSource, splitSource, turnSource, type SourceKey } from './messages'

export type Layer = 'normal' | 'shift'

export interface Binding {
  encoding: Encoding
  invert?: boolean
  normal?: string
  shift?: string
}

export interface Mapping {
  version: 1
  /** The control acting as Shift (itself a learned control). */
  shift: SourceKey | null
  bindings: Record<SourceKey, Binding>
  /** Jog: scratch / nudge / scrub sensitivity (1 = the default feel). */
  jogSensitivity: number
  /** Fast scrub (Shift + jog) relative to an ordinary scrub. */
  scrubSpeed: number
}

export type Mappings = Record<string, Mapping>

export const DEFAULT_JOG_SENSITIVITY = 1
export const DEFAULT_SCRUB_SPEED = 8

export function emptyMapping(): Mapping {
  return { version: 1, shift: null, bindings: {}, jogSensitivity: DEFAULT_JOG_SENSITIVITY, scrubSpeed: DEFAULT_SCRUB_SPEED }
}

const num = (v: unknown, lo: number, hi: number, fallback: number) =>
  typeof v === 'number' && isFinite(v) ? Math.min(hi, Math.max(lo, v)) : fallback

/** A mapping from prefs or a file, keeping only what is valid. */
export function sanitizeMapping(raw: unknown): Mapping | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as Record<string, unknown>
  if (r.version !== 1) return null
  const out = emptyMapping()
  if (typeof r.shift === 'string' && parseSource(r.shift)) out.shift = r.shift
  out.jogSensitivity = num(r.jogSensitivity, 0.05, 10, DEFAULT_JOG_SENSITIVITY)
  out.scrubSpeed = num(r.scrubSpeed, 1, 64, DEFAULT_SCRUB_SPEED)
  const bindings = r.bindings && typeof r.bindings === 'object' ? (r.bindings as Record<string, unknown>) : {}
  for (const [key, b] of Object.entries(bindings)) {
    if (!parseSource(key) || !b || typeof b !== 'object') continue
    const x = b as Record<string, unknown>
    if (!ENCODINGS.includes(x.encoding as Encoding)) continue
    const binding: Binding = { encoding: x.encoding as Encoding }
    if (x.invert === true) binding.invert = true
    if (typeof x.normal === 'string' && actionDef(x.normal)) binding.normal = x.normal
    if (typeof x.shift === 'string' && actionDef(x.shift)) binding.shift = x.shift
    if (binding.normal || binding.shift) out.bindings[key] = binding
  }
  return out
}

export function sanitizeMappings(raw: unknown): Mappings {
  const out: Mappings = {}
  if (!raw || typeof raw !== 'object') return out
  for (const [port, m] of Object.entries(raw as Record<string, unknown>)) {
    const clean = sanitizeMapping(m)
    if (clean) out[port] = clean
  }
  return out
}

/** The sources bound to `action` in `layer`. */
export function sourcesFor(m: Mapping, action: string, layer: Layer): SourceKey[] {
  return Object.entries(m.bindings)
    .filter(([, b]) => b[layer] === action)
    .map(([k]) => k)
}

/** Drop `action` from `layer` everywhere (and bindings left empty). */
export function unbind(m: Mapping, action: string, layer: Layer): Mapping {
  const bindings: Record<SourceKey, Binding> = {}
  for (const [k, b] of Object.entries(m.bindings)) {
    const next = b[layer] === action ? { ...b, [layer]: undefined } : b
    if (next.normal || next.shift) bindings[k] = next
  }
  return { ...m, bindings }
}

/**
 * Learn: `source` now does `action` in `layer`. It REPLACES the action's
 * previous control in that layer (learning means "this is the control for
 * X"). Learning a control's normal action fills its Shift layer with the
 * keyboard's Shift meaning when the Shift layer is empty. A control that was
 * Shift stops being Shift.
 */
export function bind(m: Mapping, source: SourceKey, encoding: Encoding, layer: Layer, action: string): Mapping {
  const cleared = unbind(m, action, layer)
  const prev = cleared.bindings[source]
  // A new encoding (another kind of control) leaves the other layer's action
  // only if it still makes sense: same kind of encoding.
  const sameKind = prev && (prev.encoding === 'button') === (encoding === 'button')
  const next: Binding = { encoding, invert: prev?.invert, normal: sameKind ? prev?.normal : undefined, shift: sameKind ? prev?.shift : undefined }
  next[layer] = action
  if (layer === 'normal' && !next.shift) {
    const d = shiftDefault(action)
    if (d && sourcesFor(cleared, d, 'shift').length === 0) next.shift = d
  }
  if (!next.invert) delete next.invert
  const bindings = { ...cleared.bindings, [source]: next }
  // An encoder's two directions are one control, decoded once: they share
  // its encoding.
  const { base, dir } = splitSource(source)
  const other = dir && bindings[turnSource(base, dir === '+' ? '-' : '+')]
  if (dir && other) bindings[turnSource(base, dir === '+' ? '-' : '+')] = { ...other, encoding }
  return { ...cleared, shift: cleared.shift === source ? null : cleared.shift, bindings }
}

/** Make `source` the Shift control (it then does nothing else). */
export function bindShift(m: Mapping, source: SourceKey): Mapping {
  const bindings = { ...m.bindings }
  delete bindings[source]
  return { ...m, shift: source, bindings }
}

export function setBinding(m: Mapping, source: SourceKey, patch: Partial<Binding>): Mapping {
  const b = m.bindings[source]
  if (!b) return m
  const bindings = { ...m.bindings, [source]: { ...b, ...patch } }
  // A directional turn's encoding is its encoder's: both directions follow.
  const { base, dir } = splitSource(source)
  const otherKey = dir && turnSource(base, dir === '+' ? '-' : '+')
  if (otherKey && bindings[otherKey] && patch.encoding) bindings[otherKey] = { ...bindings[otherKey], encoding: patch.encoding }
  return { ...m, bindings }
}

/**
 * The binding a message's source reaches: its own, or — for the LSB half of
 * a 14-bit pair (CC n+32) — its MSB's. `isLsb` says which half it is.
 */
export function resolveBinding(m: Mapping, source: SourceKey): { key: SourceKey; binding: Binding; isLsb: boolean } | null {
  const own = m.bindings[source]
  if (own) return { key: source, binding: own, isLsb: false }
  const s = parseSource(source)
  if (s?.kind === 'cc' && s.number >= 32 && s.number < 64) {
    const key = `cc:${s.channel}:${s.number - 32}`
    const msb = m.bindings[key]
    if (msb && isFourteenBit(msb.encoding)) return { key, binding: msb, isLsb: true }
  }
  return null
}
