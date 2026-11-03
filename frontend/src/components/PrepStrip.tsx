import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type Capabilities, type Track, type TrackOrigin } from '../api'
import { useDeckKeys, type DeckKeyTargets } from '../lib/deckKeys'
import { useDropTarget, type DragPayload } from '../lib/drag'
import { usePrefsWriter } from '../lib/prefs'
import { mixer } from '../lib/audio/mixer'
import { setMixer } from '../lib/audio/mixerState'
import { useAppSetting } from './AppSettings'
import { alignedPosition, effectiveBpm, octaveFor, phaseOffset, syncRate, type DeckTempo as Tempo } from '../lib/sync'
import { DeckControls } from './deck/DeckControls'
import { DeckHeader } from './deck/DeckHeader'
import { DeckOverlays } from './deck/DeckOverlays'
import { DeckTempo } from './deck/DeckTempo'
import { DeckMainWave, DeckOverview, type DeckView } from './deck/DeckWaves'
import { useDeck, type Deck } from './deck/useDeck'
import { MIN_SEC, MAX_SEC, DEFAULT_SEC } from './MainWaveform'
import { MixerPanel } from './mixer/MixerPanel'

export type DeckId = 'A' | 'B'
/** `performance.now()` — the global, which this file's `performance` prop shadows. */
const performance_now = () => globalThis.performance.now()
export const DECK_IDS: DeckId[] = ['A', 'B']
export const otherDeck = (id: DeckId): DeckId => (id === 'A' ? 'B' : 'A')

/** What App holds per deck: the track, whose library it is, and two counters
 *  it bumps (a row's play button; an outside edit of the track's cues). */
export interface DeckSlot {
  track: Track | null
  origin: TrackOrigin
  playRequest: number
  cuesRefresh: number
}

export const EMPTY_SLOT: DeckSlot = { track: null, origin: 'collection', playRequest: 0, cuesRefresh: 0 }

/** A track or file dragged onto a deck (App knows how to load each). */
export interface DeckDrop {
  hint: (p: DragPayload) => true | null
  label: (p: DragPayload, deck: DeckId) => string | null
  onDrop: (p: DragPayload, deck: DeckId) => void
}

interface Props {
  decks: Record<DeckId, DeckSlot>
  /** Each deck's capabilities: its OWN track's library. */
  caps: Record<DeckId, Capabilities>
  /** Performance mode: deck B, stacked waveforms (decided 2026-11-02). */
  performance: boolean
  /** P (the button is the track list toolbar's). */
  onTogglePerformance: () => void
  /** The deck keys and loads go to (in performance mode). */
  focused: DeckId
  onFocus: (id: DeckId) => void
  drop: DeckDrop
  onError?: (msg: string) => void
  /** Neutral/success feedback (e.g. Auto Hotcues result). */
  onNotify?: (kind: 'success' | 'error', msg: string) => void
  /** Reports whether a deck is playing, so the library can light its row. */
  onPlayingChange: (id: DeckId, playing: boolean) => void
  /** Opens Edit Tags — passed only where the library will accept the edit;
   *  a deck offers it only for a collection track. */
  onEditTags?: (track: Track) => void
}

/**
 * The main waveform's zoom and raw/bars toggle, persisted to userprefs.json so
 * they survive track switches (MainWaveform unmounts during each load) and app
 * restarts. Hydrated once from the shared ['prefs'] query, then written through
 * the shared, debounced prefs writer (as App's column layout is). One value for
 * every deck: lanes at one zoom line their beats up.
 */
function useDeckView(): DeckView {
  const [secPerView, setSecPerView] = useState(DEFAULT_SEC)
  const [rawWave, setRawWave] = useState(false)
  const prefsQuery = useQuery({ queryKey: ['prefs'], queryFn: api.getPrefs })
  const hydratedRef = useRef(false)
  const writePrefs = usePrefsWriter()
  useEffect(() => {
    if (hydratedRef.current || !prefsQuery.data) return
    hydratedRef.current = true
    const z = (prefsQuery.data as { mainZoomSec?: unknown }).mainZoomSec
    if (typeof z === 'number' && isFinite(z)) {
      setSecPerView(Math.min(MAX_SEC, Math.max(MIN_SEC, z)))
    }
    if ((prefsQuery.data as { mainWaveRaw?: unknown }).mainWaveRaw === true) setRawWave(true)
  }, [prefsQuery.data])
  useEffect(() => {
    if (!hydratedRef.current) return // don't clobber saved prefs pre-hydration
    writePrefs({ mainZoomSec: secPerView, mainWaveRaw: rawWave })
  }, [secPerView, rawWave, writePrefs])
  return { secPerView, setSecPerView, rawWave, setRawWave }
}

/**
 * The DJ-style deck strip across the top of the window. The decks themselves —
 * state, audio, every action — are `useDeck` (components/deck/); this lays
 * them out and routes the keyboard to them.
 *
 * One deck normally. PERFORMANCE MODE (P, or the Performance button at the
 * right of the track list's toolbar)
 * adds deck B: the two headers side by side, the two main waveforms stacked at
 * one zoom past one playhead line, the overviews side by side, then each
 * deck's controls with the mixer between them. Keys go to the FOCUSED deck
 * (click a deck), Alt/Option to the other. Deck B stays mounted while hidden,
 * so leaving and coming back finds it as it was — paused on the way out.
 */
export function PrepStrip({
  decks,
  caps,
  performance,
  onTogglePerformance,
  focused,
  onFocus,
  drop,
  onError,
  onNotify,
  onPlayingChange,
  onEditTags,
}: Props) {
  const view = useDeckView()
  // Performance-mode settings (Settings ▸ Performance mode…).
  const perfStems = useAppSetting('perfStems')
  const keylockDefault = useAppSetting('keylockDefault')
  const autoFocus = useAppSetting('autoFocusDeck')
  const eqKill = useAppSetting('eqKill')
  const sharpness = useAppSetting('crossfaderSharpness')
  const filterRange = useAppSetting('fxFilterRange')
  const effectRange = useAppSetting('fxEffectRange')
  const filterResonance = useAppSetting('fxFilterResonance')
  const reverbSize = useAppSetting('fxReverbSize')
  const delayBeats = useAppSetting('fxDelayBeats')
  const crusherDrive = useAppSetting('fxCrusherDrive')
  const flangerBeats = useAppSetting('fxFlangerBeats')
  useEffect(() => {
    setMixer({
      eqKill,
      sharpness,
      fx: { filterRange, effectRange, filterResonance, reverbSize, delayBeats, crusherDrive, flangerBeats },
    })
  }, [eqKill, sharpness, filterRange, effectRange, filterResonance, reverbSize, delayBeats, crusherDrive, flangerBeats])
  const lead: DeckId = performance ? focused : 'A'
  const playingA = useCallback((p: boolean) => onPlayingChange('A', p), [onPlayingChange])
  const playingB = useCallback((p: boolean) => onPlayingChange('B', p), [onPlayingChange])
  const common = {
    secPerView: view.secPerView,
    onError,
    onNotify,
    tempoActive: performance,
    // A stem track loaded INTO performance mode follows the setting; the prep
    // deck always loads stems.
    stemsAllowed: !performance || perfStems,
    keylockDefault,
  }
  const editTags = (slot: DeckSlot) => (onEditTags && slot.origin === 'collection' ? onEditTags : undefined)
  const a = useDeck({
    ...common,
    ...decks.A,
    caps: caps.A,
    channel: 'A',
    onPlayingChange: playingA,
    onEditTags: editTags(decks.A),
    persist: true,
    leadsAmbient: lead === 'A',
  })
  const b = useDeck({
    ...common,
    ...decks.B,
    caps: caps.B,
    channel: 'B',
    onPlayingChange: playingB,
    onEditTags: editTags(decks.B),
    persist: false,
    leadsAmbient: lead === 'B',
  })
  const deckOf = (id: DeckId) => (id === 'A' ? a : b)

  // Leaving performance mode hides deck B: stop it, or it plays on unseen.
  const pauseB = useRef(b.pause)
  pauseB.current = b.pause
  useEffect(() => {
    if (!performance) pauseB.current()
  }, [performance])

  // The mixer: in circuit only in performance mode (prep listening stays as it
  // was), each channel's trim starting at its track's own auto gain, and each
  // deck's tempo for the echo.
  useEffect(() => mixer().setActive(performance), [performance])
  const gainA = decks.A.track?.auto_gain_db ?? null
  const gainB = decks.B.track?.auto_gain_db ?? null
  // The `useAutoGain` setting off: every channel starts at 0 dB.
  const useAutoGain = useAppSetting('useAutoGain')
  useEffect(() => mixer().setAutoGain('A', useAutoGain ? (gainA ?? 0) : 0), [gainA, useAutoGain])
  useEffect(() => mixer().setAutoGain('B', useAutoGain ? (gainB ?? 0) : 0), [gainB, useAutoGain])
  const bpmA = (a.activeMarker?.bpm ?? 0) * a.rate
  const bpmB = (b.activeMarker?.bpm ?? 0) * b.rate
  useEffect(() => mixer().setTempo('A', bpmA), [bpmA])
  useEffect(() => mixer().setTempo('B', bpmB), [bpmB])

  // ---- tempo master + SYNC -------------------------------------------------
  // A synced deck follows the MASTER's tempo (at the nearer octave) and, while
  // both play, its beats: re-aligned whenever they drift past 3% of a beat —
  // after a hotcue, a jump, the follower starting. Moving its pitch fader by
  // hand releases it. All from the grids (lib/sync.ts).
  const [master, setMaster] = useState<DeckId>('A')
  const [synced, setSynced] = useState<Record<DeckId, boolean>>({ A: false, B: false })
  const live = useRef({ a, b, master, synced })
  live.current = { a, b, master, synced }
  const lastAlign = useRef<Record<DeckId, number>>({ A: 0, B: 0 })
  const tempoOf = (d: Deck): Tempo => ({ grid: d.grid, pos: d.playheadNow(), rate: d.rate })

  /** Bring follower `id` onto master `m`'s tempo, and its beats onto the
   *  master's when both play and `phase` (or a drift past the threshold). */
  const follow = useCallback((id: DeckId, m: DeckId, force: boolean) => {
    const { a, b } = live.current
    const f = id === 'A' ? a : b
    const md = m === 'A' ? a : b
    const r = syncRate(tempoOf(md), tempoOf(f))
    if (!r) return
    if (Math.abs(r.rate - f.rate) > 1e-6) f.setRate(r.rate)
    if (!f.playing || !md.playing) return
    const now = performance_now()
    if (!force && now - lastAlign.current[id] < 400) return
    const ft = tempoOf(f)
    const off = phaseOffset(tempoOf(md), ft, r.octave)
    if (off == null || Math.abs(off) < (force ? 0.005 : 0.03)) return
    lastAlign.current[id] = now
    f.seek(alignedPosition(ft, off, r.octave))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!performance) return
    const t = setInterval(() => {
      const { master, synced } = live.current
      for (const id of DECK_IDS) if (synced[id] && id !== master) follow(id, master, false)
    }, 150)
    return () => clearInterval(t)
  }, [performance, follow])
  // A follower that starts playing lines up at once, not on the next tick.
  useEffect(() => {
    const { master, synced } = live.current
    for (const id of DECK_IDS) if (synced[id] && id !== master) follow(id, master, true)
  }, [a.playing, b.playing, follow])
  // Leaving performance mode releases SYNC (the decks go back to their own tempo).
  useEffect(() => {
    if (!performance) setSynced({ A: false, B: false })
  }, [performance])

  const toggleSync = (id: DeckId) => {
    if (synced[id]) {
      setSynced((s) => ({ ...s, [id]: false }))
      return
    }
    // SYNC on the master hands the master to the other deck.
    const m = master === id ? otherDeck(id) : master
    if (m !== master) setMaster(m)
    setSynced({ [id]: true, [m]: false } as Record<DeckId, boolean>)
    follow(id, m, true)
  }
  const makeMaster = (id: DeckId) => {
    setMaster(id)
    setSynced((s) => ({ ...s, [id]: false }))
  }
  const syncBlocked = (id: DeckId): string | null => {
    const m = master === id ? otherDeck(id) : master
    if (!deckOf(id).grid) return 'This track has no beatgrid — Analyze it to sync'
    if (!deckOf(m).track) return `Deck ${m} has nothing loaded to sync to`
    if (!deckOf(m).grid) return `Deck ${m}'s track has no beatgrid to sync to`
    return null
  }
  /** This deck's beats against the other's, for the phase meter. */
  const phaseOf = (id: DeckId) => () => {
    const { a, b } = live.current
    const me = id === 'A' ? a : b
    const other = id === 'A' ? b : a
    if (!me.playing || !other.playing) return null
    const mine = tempoOf(me)
    const theirs = tempoOf(other)
    const em = effectiveBpm(mine)
    const eo = effectiveBpm(theirs)
    if (em == null || eo == null) return null
    return phaseOffset(theirs, mine, octaveFor(eo, em))
  }

  const keys = useRef<DeckKeyTargets>({ focused: null, other: null })
  keys.current = {
    focused: deckOf(lead).shortcuts,
    other: performance ? deckOf(otherDeck(lead)).shortcuts : null,
    togglePerformance: onTogglePerformance,
  }
  useDeckKeys(keys)

  if (!performance) {
    return (
      <div className="glass flex h-[19.75rem] shrink-0 flex-col gap-2 p-4">
        <DeckHeader deck={a} />
        <DeckMainWave deck={a} view={view} />
        <DeckOverview deck={a} view={view} />
        <DeckControls deck={a} />
        <DeckOverlays deck={a} />
      </div>
    )
  }

  // Any press on a deck focuses it — unless the `autoFocusDeck` setting is
  // off, when only its letter badge does (decided 2026-11-03).
  const clickFocus = autoFocus ? onFocus : undefined
  const pane = (id: DeckId, children: ReactNode, className = 'min-w-0 flex-1') => (
    <div className={className} onPointerDownCapture={clickFocus && (() => clickFocus(id))}>
      {children}
    </div>
  )
  const badge = (id: DeckId) => (
    <button
      type="button"
      onPointerDown={() => onFocus(id)}
      className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-lg font-mono text-[13px] font-semibold transition-colors ${
        id === focused
          ? 'bg-accent text-ink-950 shadow-[0_0_14px_-2px_var(--color-accent)]'
          : 'bg-ink-800 text-muted hover:text-text'
      }`}
      title={id === focused ? `Deck ${id}: the keys act on it` : `Deck ${id}: click to focus`}
    >
      {id}
    </button>
  )
  return (
    <div className="glass flex h-[32rem] shrink-0 flex-col gap-2 p-4">
      <div className="flex shrink-0 items-center gap-3">
        {pane('A', <DeckHeader deck={a} compact badge={badge('A')} />)}
        {/* Where deck A's header ends and B's begins: a wider gap and a rule. */}
        <span aria-hidden className="mx-3 h-9 w-px shrink-0 bg-line" />
        {pane('B', <DeckHeader deck={b} compact badge={badge('B')} />)}
      </div>
      {DECK_IDS.map((id) => (
        <DeckLane key={id} id={id} deck={deckOf(id)} view={view} focused={id === focused} drop={drop} onFocus={clickFocus} />
      ))}
      <div className="flex shrink-0 gap-3">
        {pane(
          'A',
          <>
            <div className="h-7">
              <DeckOverview deck={a} view={view} />
            </div>
            <DeckControls deck={a} stacked />
            <DeckTempo
              deck={a}
              master={master === 'A'}
              synced={synced.A}
              syncBlocked={syncBlocked('A')}
              onSync={() => toggleSync('A')}
              onMaster={() => makeMaster('A')}
              onManualPitch={() => synced.A && setSynced((s) => ({ ...s, A: false }))}
              phase={phaseOf('A')}
            />
          </>,
          'flex min-w-0 flex-1 flex-col gap-2',
        )}
        <MixerPanel autoGain={{ A: gainA, B: gainB }} autoGainOn={useAutoGain} />
        {pane(
          'B',
          <>
            <div className="h-7">
              <DeckOverview deck={b} view={view} />
            </div>
            <DeckControls deck={b} stacked />
            <DeckTempo
              deck={b}
              master={master === 'B'}
              synced={synced.B}
              syncBlocked={syncBlocked('B')}
              onSync={() => toggleSync('B')}
              onMaster={() => makeMaster('B')}
              onManualPitch={() => synced.B && setSynced((s) => ({ ...s, B: false }))}
              phase={phaseOf('B')}
            />
          </>,
          'flex min-w-0 flex-1 flex-col gap-2',
        )}
      </div>
      <DeckOverlays deck={a} />
      <DeckOverlays deck={b} />
    </div>
  )
}

/** One deck's main waveform in performance mode: its own drop target (a track
 *  dropped here loads into THIS deck), ringed while focused. */
function DeckLane({
  id,
  deck,
  view,
  focused,
  drop,
  onFocus,
}: {
  id: DeckId
  deck: Deck
  view: DeckView
  focused: boolean
  drop: DeckDrop
  /** Focus on any press (the `autoFocusDeck` setting); undefined = off. */
  onFocus?: (id: DeckId) => void
}) {
  const [ref, over] = useDropTarget<true>({
    hint: drop.hint,
    label: (p) => drop.label(p, id),
    onDrop: (p) => drop.onDrop(p, id),
  })
  return (
    <div
      ref={ref}
      onPointerDownCapture={onFocus && (() => onFocus(id))}
      className={`relative flex min-h-0 flex-1 rounded-2xl transition-shadow ${
        over ? 'ring-2 ring-accent' : focused ? 'ring-1 ring-accent/45' : ''
      }`}
    >
      <DeckMainWave deck={deck} view={view} empty={`Deck ${id} — drop a track here, or focus it and load one`} />
    </div>
  )
}
