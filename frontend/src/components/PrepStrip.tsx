import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type Track, type TrackOrigin } from '../api'
import { useCaps } from '../lib/capabilities'
import { useDeckKeys, type DeckKeyTargets } from '../lib/deckKeys'
import { usePrefsWriter } from '../lib/prefs'
import { DeckControls } from './deck/DeckControls'
import { DeckHeader } from './deck/DeckHeader'
import { DeckOverlays } from './deck/DeckOverlays'
import { DeckMainWave, DeckOverview, type DeckView } from './deck/DeckWaves'
import { useDeck } from './deck/useDeck'
import { MIN_SEC, MAX_SEC, DEFAULT_SEC } from './MainWaveform'

interface Props {
  /** The track currently loaded into the prep deck, if any. */
  track: Track | null
  /** Bumped by the library's per-row play button to load + auto-play. */
  playRequest?: number
  onError?: (msg: string) => void
  /** Neutral/success feedback (e.g. Auto Hotcues result). */
  onNotify?: (kind: 'success' | 'error', msg: string) => void
  /** Which library the track belongs to (see `DeckOptions.origin`). */
  origin?: TrackOrigin
  /** Bumped when something outside the deck (batch grid analysis) rewrote the
   *  loaded track's cues/grid, so the deck re-reads them. */
  cuesRefresh?: number
  /** Reports whether the deck is playing, so the library can light its row. */
  onPlayingChange?: (playing: boolean) => void
  /** Opens Edit Tags for the loaded track — passed only where the library
   *  will accept the edit, so the cover popup offers it only then. */
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
 * The DJ-style "prep strip" across the top of the window: the track and its
 * readouts, a scrolling zoomable main waveform above a whole-track overview,
 * then transport, loop and hotcue controls. The deck itself — state, audio,
 * every action — is `useDeck` (components/deck/); this lays it out and routes
 * the keyboard to it. Playback, loops and scratch run in the deck's audio
 * worklet (lib/audio/).
 */
export function PrepStrip(props: Props) {
  const caps = useCaps()
  const view = useDeckView()
  const deck = useDeck({ ...props, caps, secPerView: view.secPerView })

  const keys = useRef<DeckKeyTargets>({ focused: null, other: null })
  keys.current = { focused: deck.shortcuts, other: null }
  useDeckKeys(keys)

  return (
    <div className="glass flex h-[19.75rem] shrink-0 flex-col gap-2 p-4">
      <DeckHeader deck={deck} />
      <DeckMainWave deck={deck} view={view} />
      <DeckOverview deck={deck} view={view} />
      <DeckControls deck={deck} />
      <DeckOverlays deck={deck} />
    </div>
  )
}
