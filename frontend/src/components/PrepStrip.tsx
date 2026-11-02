import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type Capabilities, type Track, type TrackOrigin } from '../api'
import { useDeckKeys, type DeckKeyTargets } from '../lib/deckKeys'
import { useDropTarget, type DragPayload } from '../lib/drag'
import { Icon } from '../lib/icons'
import { usePrefsWriter } from '../lib/prefs'
import { DeckControls } from './deck/DeckControls'
import { DeckHeader } from './deck/DeckHeader'
import { DeckOverlays } from './deck/DeckOverlays'
import { DeckMainWave, DeckOverview, type DeckView } from './deck/DeckWaves'
import { useDeck, type Deck } from './deck/useDeck'
import { MIN_SEC, MAX_SEC, DEFAULT_SEC } from './MainWaveform'

export type DeckId = 'A' | 'B'
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
 * One deck normally. PERFORMANCE MODE (P, or the button beside Auto hotcues)
 * adds deck B: the two headers side by side, the two main waveforms stacked at
 * one zoom past one playhead line, the overviews side by side, then each
 * deck's controls (the mixer goes between them). Keys go to the FOCUSED deck
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
  const lead: DeckId = performance ? focused : 'A'
  const playingA = useCallback((p: boolean) => onPlayingChange('A', p), [onPlayingChange])
  const playingB = useCallback((p: boolean) => onPlayingChange('B', p), [onPlayingChange])
  const common = { secPerView: view.secPerView, onError, onNotify }
  const editTags = (slot: DeckSlot) => (onEditTags && slot.origin === 'collection' ? onEditTags : undefined)
  const a = useDeck({
    ...common,
    ...decks.A,
    caps: caps.A,
    onPlayingChange: playingA,
    onEditTags: editTags(decks.A),
    persist: true,
    leadsAmbient: lead === 'A',
  })
  const b = useDeck({
    ...common,
    ...decks.B,
    caps: caps.B,
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

  const keys = useRef<DeckKeyTargets>({ focused: null, other: null })
  keys.current = {
    focused: deckOf(lead).shortcuts,
    other: performance ? deckOf(otherDeck(lead)).shortcuts : null,
    togglePerformance: onTogglePerformance,
  }
  useDeckKeys(keys)

  const toggle = (
    <button
      onClick={onTogglePerformance}
      aria-pressed={performance}
      aria-label="Performance mode"
      title={performance ? 'Leave performance mode (P)' : 'Performance mode — a second deck to try transitions (P)'}
      className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-full ${
        performance ? 'is-selected text-text' : 'btn-glass text-muted'
      }`}
    >
      <Icon name="decks" size={18} />
    </button>
  )

  if (!performance) {
    return (
      <div className="glass flex h-[19.75rem] shrink-0 flex-col gap-2 p-4">
        <DeckHeader deck={a} actions={toggle} />
        <DeckMainWave deck={a} view={view} />
        <DeckOverview deck={a} view={view} />
        <DeckControls deck={a} />
        <DeckOverlays deck={a} />
      </div>
    )
  }

  const pane = (id: DeckId, children: ReactNode, className = 'min-w-0 flex-1') => (
    <div className={className} onPointerDownCapture={() => onFocus(id)}>
      {children}
    </div>
  )
  const badge = (id: DeckId) => (
    <span
      className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-lg font-mono text-[13px] font-semibold transition-colors ${
        id === focused
          ? 'bg-accent text-ink-950 shadow-[0_0_14px_-2px_var(--color-accent)]'
          : 'bg-ink-800 text-muted'
      }`}
      title={id === focused ? `Deck ${id} — the keys act on it` : `Deck ${id} — click it to focus (Alt/Option + key acts on it)`}
    >
      {id}
    </span>
  )
  // The centre column: the toggle above, the mixer (phase 4) below.
  const centre = 'w-9 shrink-0'
  return (
    <div className="glass flex h-[29rem] shrink-0 flex-col gap-2 p-4">
      <div className="flex shrink-0 items-center gap-3">
        {pane('A', <DeckHeader deck={a} compact badge={badge('A')} />)}
        {toggle}
        {pane('B', <DeckHeader deck={b} compact badge={badge('B')} />)}
      </div>
      {DECK_IDS.map((id) => (
        <DeckLane key={id} id={id} deck={deckOf(id)} view={view} focused={id === focused} drop={drop} onFocus={onFocus} />
      ))}
      <div className="flex h-7 shrink-0 gap-3">
        {pane('A', <DeckOverview deck={a} view={view} />)}
        <div className={centre} />
        {pane('B', <DeckOverview deck={b} view={view} />)}
      </div>
      <div className="flex shrink-0 gap-3">
        {pane('A', <DeckControls deck={a} stacked />, 'flex min-w-0 flex-1')}
        <div className={centre} />
        {pane('B', <DeckControls deck={b} stacked />, 'flex min-w-0 flex-1')}
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
  onFocus: (id: DeckId) => void
}) {
  const [ref, over] = useDropTarget<true>({
    hint: drop.hint,
    label: (p) => drop.label(p, id),
    onDrop: (p) => drop.onDrop(p, id),
  })
  return (
    <div
      ref={ref}
      onPointerDownCapture={() => onFocus(id)}
      className={`relative flex min-h-0 flex-1 rounded-2xl transition-shadow ${
        over ? 'ring-2 ring-accent' : focused ? 'ring-1 ring-accent/45' : ''
      }`}
    >
      <DeckMainWave deck={deck} view={view} empty={`Deck ${id} — drop a track here, or focus it and load one`} />
    </div>
  )
}
