// One deck's state and behaviour: audio engine, transport, CUE, hotcues,
// memory cues, loops, beatgrid editing, stems and analysis — everything the
// prep deck DOES, without how it looks. Moved out of PrepStrip (2026-11-02,
// performance mode phase 2) so a second deck is a second call: each deck gets
// its own track, origin and capabilities, and the components in this folder
// render whichever deck they are handed.
//
// Capabilities arrive as a parameter, not from context: in performance mode
// each deck may hold a track from a different library (a device's, a loose
// file), and the hook runs in a component that holds both.

import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  api,
  type AutoHotcuesResult,
  type Capabilities,
  type CuePoint,
  type CueType,
  type GridMarker,
  type Track,
  type TrackCues,
  type StemInfo,
  type TrackOrigin,
  trackAudioUrl,
  trackCuesFor,
  trackStemsFor,
  trackStemUrl,
} from '../../api'
import { buildBeatGrid, GRID_EPS } from '../../lib/beatgrid'
import { slotLabeller } from '../../lib/capabilities'
import { CUE_TYPE_LABELS, cueTypeColor } from '../../lib/cues'
import { formatLoudness } from '../../lib/format'
import { readOnlyNotice } from '../../lib/platformCopy'
import { analyzeStems, analyzeWaveform, type WaveColumn } from '../../lib/waveform'
import type { RawWave } from '../../lib/rawWave'
import { setAmbientFromArt } from '../../lib/ambient'
import { DeckEngine } from '../../lib/audio/deckEngine'
import { resumeAudio } from '../../lib/audio/context'
import { STEM_KEYS, type DeckShortcuts } from '../../lib/deckKeys'
import { invalidateTrackLists } from '../../lib/trackQueries'
import { eventLabel } from '../AutoCueDialog'
import type { MenuItem } from '../ContextMenu'
import { LOOP_SIZES, type LoopMode } from '../LoopControls'
import type { StemLane } from '../MainWaveform'
import { usePrefsWriter } from '../../lib/prefs'

export interface DeckOptions {
  /** The track loaded into this deck, if any. */
  track: Track | null
  /** What this deck's library can do (capsFor its origin). */
  caps: Capabilities
  /** Seconds across the main waveform — the shared zoom. */
  secPerView: number
  /** Bumped by the library's per-row play button to load + auto-play. */
  playRequest?: number
  onError?: (msg: string) => void
  /** Neutral/success feedback (e.g. Auto Hotcues result). */
  onNotify?: (kind: 'success' | 'error', msg: string) => void
  /**
   * Which library the track belongs to, and so which endpoints serve its audio
   * and cues: the loaded collection, a browsed DEVICE, or a browsed FOLDER of
   * loose files. Every EDIT control is already inert for the last two — their
   * capabilities say `writable: false`, and the deck gates on those — so this
   * only needs to redirect the two READS.
   */
  origin?: TrackOrigin
  /** Bumped when something outside the deck (batch grid analysis) rewrote the
   *  loaded track's cues/grid, so the deck re-reads them. */
  cuesRefresh?: number
  /** Reports whether the deck is playing, so the library can light its row. */
  onPlayingChange?: (playing: boolean) => void
  /** Opens Edit Tags for the loaded track — passed only where the library
   *  will accept the edit, so the cover popup offers it only then. */
  onEditTags?: (track: Track) => void
  /** Persist this deck's loop size and cue bank (deck A; default true). */
  persist?: boolean
  /** Recolour the app from this deck's cover art (default true). */
  leadsAmbient?: boolean
}

export function useDeck({
  track,
  caps,
  secPerView,
  playRequest = 0,
  onError,
  onNotify,
  origin = 'collection',
  cuesRefresh = 0,
  onPlayingChange,
  onEditTags,
  persist = true,
  leadsAmbient = true,
}: DeckOptions) {
  const qc = useQueryClient()
  const [playing, setPlaying] = useState(false)
  useEffect(() => onPlayingChange?.(playing), [playing, onPlayingChange])
  const [previewing, setPreviewing] = useState(false) // momentary hold-to-play active
  // CUE is held down (mouse OR the C key) — drives the button's pressed look,
  // which the pointer's :active alone cannot, since the key never touches it.
  const [cueHeld, setCueHeld] = useState(false)
  const [current, setCurrent] = useState(0)
  const [duration, setDuration] = useState(0)
  const [ready, setReady] = useState(false)
  const [cols, setCols] = useState<WaveColumn[] | null>(null)
  // The raw waveform's data: the mix's samples, or one per stem lane.
  const [raw, setRaw] = useState<RawWave | null>(null)
  const [stemRaws, setStemRaws] = useState<RawWave[] | null>(null)
  // A stem track: its stems (from the FILE), their lanes, and which are muted
  // — reset per track, as Traktor's stem deck does. There is no separate solo
  // state: solo is a shortcut that SETS the mutes, so every stem stays freely
  // mutable afterwards.
  const [stems, setStems] = useState<StemInfo[] | null>(null)
  const [stemLanes, setStemLanes] = useState<Float32Array[] | null>(null)
  const [stemMuted, setStemMuted] = useState<boolean[]>([])
  const [waveStatus, setWaveStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [cueData, setCueData] = useState<TrackCues | null>(null)
  // The beatgrid, rebuilt whenever the cue data is replaced (every edit returns
  // a fresh TrackCues, so this stays in step automatically). Null = no grid.
  const grid = useMemo(() => buildBeatGrid(cueData?.grid_markers), [cueData?.grid_markers])
  const [snap, setSnap] = useState(true)
  const [selectedSlot, setSelectedSlot] = useState<number | null>(null)
  const [cuePoint, setCuePoint] = useState(0) // floating "CUE" point (frontend-only)
  const [infoOpen, setInfoOpen] = useState(false) // the cover's track-info popup
  // ONE size, in beats, for beat loops and beat jump alike (persisted).
  const [beatSize, setBeatSize] = useState(4)
  const [loopMode, setLoopMode] = useState<LoopMode>('beat')
  // Grid mode swaps the hotcue pads for the grid-editing strip.
  const [gridMode, setGridMode] = useState(false)
  const [padMenu, setPadMenu] = useState<{ slot: number; x: number; y: number } | null>(null)
  const [renamingSlot, setRenamingSlot] = useState<number | null>(null)
  // Hot / Memory: which bank the pads' place in the row shows (persisted).
  const [bankMode, setBankMode] = useState<'hot' | 'memory'>('hot')
  const [memoryMenu, setMemoryMenu] = useState<{ cue: CuePoint; x: number; y: number } | null>(null)
  const [renamingMemory, setRenamingMemory] = useState<string | null>(null)

  // Loop state (transient — persisted only when saved as a hotcue later).
  const [loopRegion, setLoopRegion] = useState<{ start: number; end: number } | null>(null)
  const [loopActive, setLoopActive] = useState(false)
  const [activeBeats, setActiveBeats] = useState<number | null>(null)
  // The armed manual loop-in point: IN was pressed, OUT not yet. STATE, not a
  // ref, because the IN button and the waveform both show it while it waits.
  const [loopInPoint, setLoopInPoint] = useState<number | null>(null)
  const originalGridRef = useRef<GridMarker[] | null>(null)

  // The deck's audio (created once, asynchronously: the worklet module loads
  // first). playbackRef is set once it exists; getEngine() awaits it.
  const playbackRef = useRef<DeckEngine | null>(null)
  const enginePromiseRef = useRef<Promise<DeckEngine> | null>(null)
  const wasPlayingRef = useRef(false)
  const scratchRafRef = useRef(0)
  const engagedRef = useRef(false) // the deck is scratching (dragging or coasting)
  const pendingPlayRef = useRef(false) // a play was requested; start once ready
  const loadedIdRef = useRef<string | null>(null) // trackId whose buffer is loaded
  const [gridBusy, setGridBusy] = useState(false)
  // Active momentary "cue preview": a hotcue OR the CUE button held while paused
  // plays from its point and stops on release, unless `latched` (play pressed
  // during the hold). `id` identifies the holder so its release matches.
  const previewRef = useRef<{ id: string; start: number; latched: boolean } | null>(null)

  const trackId = track?.id ?? null

  // beatSize and the Hot / Memory bank persist to userprefs.json (survive
  // track switches and restarts) — deck A's only, when there are two. Hydrate
  // once from the shared ['prefs'] query, then write through the shared,
  // debounced prefs writer.
  const prefsQuery = useQuery({ queryKey: ['prefs'], queryFn: api.getPrefs })
  const writePrefs = usePrefsWriter()

  // The loop/jump size persists the same way (survives track switches +
  // restarts). `beatJumpBeats` is its name from when only beat jump had a size;
  // it is read once so an upgrade keeps the user's choice.
  const sizeHydratedRef = useRef(false)
  useEffect(() => {
    if (sizeHydratedRef.current || !prefsQuery.data) return
    sizeHydratedRef.current = true
    const prefs = prefsQuery.data as { beatSize?: unknown; beatJumpBeats?: unknown }
    const b = prefs.beatSize ?? prefs.beatJumpBeats
    if (typeof b === 'number' && LOOP_SIZES.includes(b)) setBeatSize(b)
  }, [prefsQuery.data])
  useEffect(() => {
    if (!sizeHydratedRef.current || !persist) return // don't clobber saved prefs pre-hydration
    writePrefs({ beatSize })
  }, [beatSize, writePrefs, persist])

  // Hot / Memory bank, remembered the same way (decided 2026-10-16).
  const bankHydratedRef = useRef(false)
  useEffect(() => {
    if (bankHydratedRef.current || !prefsQuery.data) return
    bankHydratedRef.current = true
    if ((prefsQuery.data as { cueBankMode?: unknown }).cueBankMode === 'memory') setBankMode('memory')
  }, [prefsQuery.data])
  useEffect(() => {
    if (!bankHydratedRef.current || !persist) return // don't clobber saved prefs pre-hydration
    writePrefs({ cueBankMode: bankMode })
  }, [bankMode, writePrefs, persist])

  const getEngine = (): Promise<DeckEngine> => {
    if (!enginePromiseRef.current) {
      enginePromiseRef.current = DeckEngine.create().then((eng) => {
        playbackRef.current = eng
        eng.setOnEnded(() => setPlaying(false))
        // The deck hands itself back after a scratch's coast settles.
        eng.setOnScratchDone((resumed) => scratchDoneRef.current(resumed))
        return eng
      })
      enginePromiseRef.current.catch(() => {
        enginePromiseRef.current = null
      })
    }
    return enginePromiseRef.current
  }

  // Tear the deck's audio down when the strip itself unmounts (e.g. the
  // collection picker early-returns and replaces the whole UI) — otherwise a
  // remount would play a SECOND track over the orphaned one. The AudioContext
  // is the app's and stays; this deck's nodes leave it.
  useEffect(() => {
    return () => {
      const pending = enginePromiseRef.current
      enginePromiseRef.current = null
      playbackRef.current = null
      void pending?.then((eng) => eng.dispose())
    }
  }, [])

  // Reset transport + loop state when the loaded track changes.
  useEffect(() => {
    playbackRef.current?.pause()
    setPlaying(false)
    setCurrent(0)
    setDuration(0)
    setReady(false)
    setSelectedSlot(null)
    setPadMenu(null)
    setRenamingSlot(null)
    setMemoryMenu(null)
    setRenamingMemory(null)
    setLoopRegion(null)
    setLoopActive(false)
    setActiveBeats(null)
    setCuePoint(0)
    setLoopInPoint(null)
    previewRef.current = null
    setPreviewing(false)
    setCueHeld(false)
    setStemMuted([])
  }, [trackId])

  // Recolour the whole app from this track's cover art. A device's art is not
  // served (only its audio is), so a device track keeps the default look.
  useEffect(() => {
    if (!leadsAmbient) return
    void setAmbientFromArt(trackId && origin === 'collection' ? api.artUrl(trackId) : null)
  }, [trackId, origin, leadsAmbient])

  // Analyse once per track; the decoded buffer feeds both the playback and
  // scratch engines (no re-decode) and both waveform views share the columns.
  useEffect(() => {
    if (!trackId) {
      setCols(null)
      setRaw(null)
      setStemRaws(null)
      setWaveStatus('loading')
      return
    }
    let cancelled = false
    setCols(null)
    setRaw(null)
    setStemRaws(null)
    setStems(null)
    setStemLanes(null)
    setWaveStatus('loading')
    // A stem FILE loads its four stems and plays their sum (decided: always,
    // for every stem track); anything else loads as one buffer, as before.
    const load = trackStemsFor(origin, trackId)
      .catch(() => [] as StemInfo[])
      .then(async (found) => {
        if (found.length) {
          const res = await analyzeStems(found.map((_, k) => trackStemUrl(origin, trackId, k)))
          return { cols: res.cols, audios: res.audios, lanes: res.lanes, stems: found, raw: null, raws: res.raws }
        }
        const res = await analyzeWaveform(trackAudioUrl(origin, trackId))
        return { cols: res.cols, audios: [res.audio], lanes: null, stems: null, raw: res.raw, raws: null }
      })
    Promise.all([load, getEngine()])
      .then(([res, eng]) => {
        if (cancelled) return
        setCols(res.cols)
        setRaw(res.raw)
        setStemRaws(res.raws)
        setStems(res.stems)
        setStemLanes(res.lanes)
        setWaveStatus('ready')
        // The PCM moves to the audio thread here (transferred, not copied).
        eng.load(res.audios)
        setDuration(res.audios[0].duration)
        setReady(true)
        loadedIdRef.current = trackId
        // A play was requested (library row button) → start now that it's ready.
        if (pendingPlayRef.current) {
          pendingPlayRef.current = false
          eng.play()
          setPlaying(true)
        }
      })
      .catch(() => {
        if (!cancelled) setWaveStatus('error')
      })
    return () => {
      cancelled = true
    }
  }, [trackId, origin])

  // Fetch beatgrid + cue markers for the loaded track.
  useEffect(() => {
    if (!trackId) {
      setCueData(null)
      return
    }
    let cancelled = false
    setCueData(null)
    ;trackCuesFor(origin, trackId)
      .then((d) => {
        if (cancelled) return
        setCueData(d)
        // Remember the grid as loaded, for "Reset". Flexible-grid editing is
        // multi-step and destructive, so restoring exactly what Traktor had is
        // the safety net. Set only here (keyed on trackId), never by an edit.
        originalGridRef.current = d.grid_markers
      })
      .catch(() => {
        if (!cancelled) setCueData(null)
      })
    return () => {
      cancelled = true
    }
  }, [trackId, origin])

  // Re-read after an outside edit. Deliberately NOT the effect above: that one
  // resets `originalGridRef`, and "Reset" should still restore the grid the
  // track had before the outside edit replaced it.
  useEffect(() => {
    if (!trackId || !cuesRefresh) return
    let cancelled = false
    api.trackCues(trackId).then((d) => !cancelled && setCueData(d)).catch(() => {})
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cuesRefresh])

  const toggle = () => {
    const eng = playbackRef.current
    if (!eng || !eng.ready) return
    resumeAudio() // resume on this user gesture
    // Play pressed during a momentary cue preview → latch playback on so it
    // keeps going after the hotcue button is released.
    const pv = previewRef.current
    if (pv && !pv.latched) {
      pv.latched = true
      setPreviewing(false) // becomes normal (green) playback
      if (!eng.playing) {
        eng.play()
        setPlaying(true)
      }
      setCurrent(eng.getPosition())
      return
    }
    if (eng.playing) {
      eng.pause()
      setPlaying(false)
    } else {
      eng.play()
      setPlaying(true)
    }
    setCurrent(eng.getPosition())
  }

  /** Stop playing, if it is (performance mode pauses deck B on the way out). */
  const pause = () => {
    const eng = playbackRef.current
    if (!eng?.playing) return
    previewRef.current = null
    setPreviewing(false)
    eng.pause()
    setPlaying(false)
    setCurrent(eng.getPosition())
  }

  // Start a momentary "cue preview" from `start` for holder `id`: play now, and
  // remember to stop + return on release (see endPreview) unless it's latched.
  const beginPreview = (id: string, start: number) => {
    const eng = playbackRef.current
    if (!eng || !eng.ready) return
    previewRef.current = { id, start, latched: false }
    eng.play()
    setPlaying(true)
    setPreviewing(true)
  }

  // End holder `id`'s momentary preview: stop and return to the point — unless a
  // play press latched it during the hold, in which case playback continues.
  const endPreview = (id: string) => {
    const pv = previewRef.current
    if (!pv || pv.id !== id) return
    previewRef.current = null
    setPreviewing(false)
    if (pv.latched) return
    const eng = playbackRef.current
    if (!eng) return
    eng.pause()
    eng.seek(pv.start)
    setPlaying(false)
    setCurrent(pv.start)
  }

  // CUE (Traktor/CDJ-style): while playing, jump back to the cue point and
  // pause; while paused and away from the cue, drop the cue at the playhead;
  // while paused and already at the cue, play from it for as long as the button
  // is held (momentary preview — mirrors the hotcue buttons).
  const onCuePress = () => {
    const eng = playbackRef.current
    if (!eng || !eng.ready) return
    setCueHeld(true)
    resumeAudio()
    if (eng.playing) {
      leaveLoopFor(cuePoint)
      eng.pause()
      eng.seek(cuePoint)
      setPlaying(false)
      setCurrent(cuePoint)
    } else if (Math.abs(current - cuePoint) > 0.03) {
      // Drop the cue at the (snapped) playhead and park on it, so the next press
      // is "at the cue" and previews rather than re-setting.
      const t = snapTime(current)
      setCuePoint(t)
      eng.seek(t)
      setCurrent(t)
    } else {
      leaveLoopFor(cuePoint)
      beginPreview('cue', cuePoint)
    }
  }
  const onCueRelease = () => {
    setCueHeld(false)
    endPreview('cue')
  }

  // A key-up that happens in another window never arrives, which would leave
  // the button looking held; letting go of the window lets go of CUE.
  useEffect(() => {
    const letGo = () => setCueHeld(false)
    window.addEventListener('blur', letGo)
    return () => window.removeEventListener('blur', letGo)
  }, [])

  // A library row's play button bumps `playRequest`: load (if needed) + play.
  // If the requested track is already loaded and ready, start immediately;
  // otherwise flag it and the analysis effect starts playback once ready.
  // Only a CHANGE is a request: the deck unmounts behind the library picker,
  // and remounting with a non-zero count must not start playback.
  const seenPlayRequestRef = useRef(playRequest)
  useEffect(() => {
    if (playRequest === seenPlayRequestRef.current) return
    seenPlayRequestRef.current = playRequest
    pendingPlayRef.current = true
    const eng = playbackRef.current
    if (loadedIdRef.current === trackId && eng?.ready) {
      pendingPlayRef.current = false
      resumeAudio()
      eng.play()
      setPlaying(true)
      setCurrent(eng.getPosition())
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playRequest])

  // Follow the Web Audio clock while playing (high-res → smooth playhead).
  useEffect(() => {
    if (!playing) return
    let raf = 0
    const tick = () => {
      const eng = playbackRef.current
      if (eng) setCurrent(eng.getPosition())
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [playing])

  const seek = (t: number) => {
    const eng = playbackRef.current
    if (!eng) return
    eng.seek(t)
    setCurrent(t)
  }

  // A cue jump that lands outside the engaged loop leaves it, Traktor-style.
  // Without this the loop stays in force and Web Audio, started past a loop's
  // end, wraps straight back into it. A jump inside the loop keeps it.
  const leaveLoopFor = (t: number) => {
    if (!loopActive || !loopRegion) return
    if (t >= loopRegion.start && t < loopRegion.end) return
    playbackRef.current?.setLoopEnabled(false)
    setLoopActive(false)
  }

  // User-driven seek from the waveforms: navigating away drops the active loop
  // (otherwise it would just pull playback back into the loop region).
  const seekManual = (t: number) => {
    if (loopActive) {
      playbackRef.current?.setLoopEnabled(false)
      setLoopActive(false)
    }
    seek(t)
  }

  // ---- beat jump / loop move ------------------------------------------
  // ‹ › (and ←/→): an engaged loop MOVES by its own length and keeps playing,
  // Traktor-style; otherwise the playhead jumps `beatSize` beats. Both preserve
  // sub-beat phase (pure translation, no grid snap), and seek() keeps the
  // play/pause state.
  const moveOrJump = (dir: -1 | 1) => {
    const eng = playbackRef.current
    if (!eng || !eng.ready) return
    if (loopActive && loopRegion) {
      const { start, end } = loopRegion
      // A beat loop moves in BEATS, so it lands right across a tempo change; a
      // manual loop has no beat length, so it moves by its duration.
      let nStart: number
      let nEnd: number
      if (activeBeats != null && grid) {
        nStart = grid.advanceBeats(start, dir * activeBeats)
        nEnd = grid.advanceBeats(nStart, activeBeats)
      } else {
        const len = end - start
        nStart = start + dir * len
        nEnd = nStart + len
      }
      if (nStart < 0 || nEnd > duration) return // would leave the track
      // The playhead travels with the loop, keeping its place inside it. The
      // NEW loop is set first: the engine's seek honours the loop in force, so
      // seeking first would wrap the playhead straight back into the old one.
      const pos = eng.getPosition() + (nStart - start)
      engagLoop(nStart, nEnd, activeBeats)
      eng.seek(Math.min(nEnd - 0.001, Math.max(nStart, pos)))
      setCurrent(eng.getPosition())
      return
    }
    if (!grid) return
    // Phase is preserved in BEAT space, so a jump crossing a tempo change lands
    // on the musically right beat rather than a fixed number of seconds away.
    eng.seek(grid.advanceBeats(eng.getPosition(), dir * beatSize))
    setCurrent(eng.getPosition())
  }

  // ---- scratch ----------------------------------------------------------
  // The deck scratches itself: a drag moves its position, a release coasts to
  // playing speed (or a stop) and it plays on BY ITSELF, then says so — so
  // there is no gap between the coast and the music. React only mirrors it.
  const scratchDone = (resumed: boolean) => {
    cancelAnimationFrame(scratchRafRef.current)
    engagedRef.current = false
    const eng = playbackRef.current
    if (eng) setCurrent(eng.getPosition())
    setPlaying(resumed)
  }
  const scratchDoneRef = useRef(scratchDone)
  scratchDoneRef.current = scratchDone

  const runScratchRaf = () => {
    cancelAnimationFrame(scratchRafRef.current)
    const tick = () => {
      const eng = playbackRef.current
      if (!eng || !engagedRef.current) return
      setCurrent(eng.getPosition())
      scratchRafRef.current = requestAnimationFrame(tick)
    }
    scratchRafRef.current = requestAnimationFrame(tick)
  }

  const onScratchStart = () => {
    const eng = playbackRef.current
    if (!eng || !eng.ready) return
    if (engagedRef.current) {
      eng.scratchRegrab() // re-grab while coasting
      return
    }
    wasPlayingRef.current = eng.playing
    if (eng.playing) setPlaying(false)
    eng.scratchBegin(eng.getPosition())
    engagedRef.current = true
    runScratchRaf()
  }

  const onScratchMove = (t: number) => {
    if (engagedRef.current) playbackRef.current?.scratchTarget(t)
    else setCurrent(t)
  }

  const onScratchEnd = (t: number) => {
    if (engagedRef.current) {
      playbackRef.current?.scratchRelease(wasPlayingRef.current)
    } else {
      seek(t)
      if (wasPlayingRef.current) {
        playbackRef.current?.play()
        setPlaying(true)
      }
    }
  }

  // ---- loops ------------------------------------------------------------
  // Snap a time to the nearest beat when Snap is on (needs a beatgrid).
  const snapTime = (t: number) => (snap && grid ? grid.snapToBeat(t) : Math.max(0, t))

  const engagLoop = (start: number, end: number, beats: number | null) => {
    const eng = playbackRef.current
    if (!eng) return
    eng.setLoop(start, end, true)
    setLoopRegion({ start, end })
    setLoopActive(true)
    setActiveBeats(beats)
    setLoopInPoint(null)
    setCurrent(eng.getPosition())
  }

  const setBeatLoop = (beats: number) => {
    const eng = playbackRef.current
    if (!eng || !eng.ready || !grid) return
    // Pressing the size of the loop that's already playing disables it.
    if (loopActive && activeBeats === beats) {
      eng.setLoopEnabled(false)
      setLoopActive(false)
      setCurrent(eng.getPosition())
      return
    }
    // Resizing an active loop keeps its start locked; only a fresh loop starts
    // at the current playhead.
    const start = loopActive && loopRegion ? loopRegion.start : snapTime(eng.getPosition())
    // Measured from the loop's own start, so a loop spanning a tempo change
    // still covers the right number of beats.
    engagLoop(start, grid.advanceBeats(start, beats), beats)
  }

  const loopIn = () => {
    const eng = playbackRef.current
    if (!eng || !eng.ready) return
    setLoopInPoint(snapTime(eng.getPosition()))
  }

  const loopOut = () => {
    const eng = playbackRef.current
    if (!eng || !eng.ready) return
    const end = snapTime(eng.getPosition())
    // OUT closes an armed IN — or, with a manual loop already set, moves that
    // loop's end to the playhead (Traktor's loop-out adjust).
    const start = loopInPoint ?? (loopRegion && activeBeats == null ? loopRegion.start : null)
    if (start == null || end <= start) return
    engagLoop(start, end, null)
  }

  const toggleLoop = () => {
    const eng = playbackRef.current
    if (!eng || !loopRegion) return
    const next = !loopActive
    eng.setLoopEnabled(next)
    setLoopActive(next)
    setCurrent(eng.getPosition())
  }

  // − / + (and Cmd/Ctrl+↓/↑): step the shared size, clamping at both ends. An
  // engaged BEAT loop follows it — halving or doubling from its locked start, as
  // on a CDJ. A manual loop has no beat size to follow, so it is left alone.
  const stepSize = (dir: -1 | 1) => {
    const i = LOOP_SIZES.indexOf(beatSize)
    const next = LOOP_SIZES[Math.min(LOOP_SIZES.length - 1, Math.max(0, (i < 0 ? 7 : i) + dir))]
    if (next === beatSize) return
    setBeatSize(next)
    if (loopActive && activeBeats != null) setBeatLoop(next)
  }

  // The size key: a loop of exactly this size switches off; otherwise a loop of
  // this size is set (from the playhead, or resized from an engaged loop's start).
  const toggleSizeLoop = () => setBeatLoop(beatSize)

  // A beat loop's end is stored in seconds, so editing the tempo of a marker
  // inside it would silently detune it. Re-derive the end whenever the grid
  // changes, keeping the loop the beat length the user actually asked for.
  useEffect(() => {
    if (!grid || !loopActive || !loopRegion || activeBeats == null) return
    const end = grid.advanceBeats(loopRegion.start, activeBeats)
    if (Math.abs(end - loopRegion.end) < 1e-4) return
    playbackRef.current?.setLoop(loopRegion.start, end, true)
    setLoopRegion({ start: loopRegion.start, end })
    // Only the grid should trigger this; loop state changes set the end already.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [grid])

  // ---- capability-derived -----------------------------------------------
  // Edits are guarded at the point of INTENT, so the user gets the reason
  // instead of a 422 from the adapter's backstop, and the deck stays fully
  // usable for listening — which is most of its value on a library you cannot
  // write.
  //
  // TWO gates, not one. `writable` is the library; `cues.editable` and
  // `grid.editable` are the features. A platform can be writable overall while
  // its cue or grid store is not implemented yet, so gating only on `writable`
  // would re-expose these controls the moment that flips — which is exactly how
  // this would regress unnoticed when Rekordbox writes land.
  const canEditCues = caps.writable && caps.cues.editable
  const canEditGrid = caps.writable && caps.grid.editable
  const refuseCueEdit = () => {
    if (canEditCues) return false
    onError?.(
      readOnlyNotice(caps) ?? `Cues cannot be edited on ${caps.save.app_name} libraries yet.`,
    )
    return true
  }
  const refuseGridEdit = () => {
    if (canEditGrid) return false
    onError?.(
      readOnlyNotice(caps) ??
        `The beatgrid cannot be edited on ${caps.save.app_name} libraries yet.`,
    )
    return true
  }
  const slotCount = caps.cues.hotcue_slots
  const slotLabel = slotLabeller(caps)
  // Loops are a cue TYPE on some platforms and a separate bank on others; the
  // dropdown only ever offers the point types.
  const pointCueTypes = caps.cues.types.filter((t) => t !== 'loop')

  // ---- hotcues ----------------------------------------------------------
  const hotcueAt = (slot: number) =>
    cueData?.cues.find((c) => c.role === 'hotcue' && c.slot === slot) ?? null

  const applyCueEdit = (fresh: TrackCues) => {
    setCueData(fresh)
    invalidateTrackLists(qc)
  }

  // Map a loop length (seconds) back to a preset beat count for the size
  // highlight, snapping to the nearest preset when it's close (float tolerance).
  // Needs the start as well as the length: under a marker list a duration
  // alone no longer identifies a beat count.
  const beatsForLoop = (start: number, length: number): number | null => {
    if (!grid) return null
    const beats = grid.beatsBetween(start, start + length)
    let best: number | null = null
    let bestDiff = Infinity
    for (const s of LOOP_SIZES) {
      const diff = Math.abs(s - beats)
      if (diff < bestDiff) {
        bestDiff = diff
        best = s
      }
    }
    return best != null && bestDiff <= beats * 0.05 ? best : null
  }

  // Hotcue slot pressed (mouse/touch down, or key down). Empty slots create a
  // hotcue; assigned slots jump to their point. When jumping while PAUSED we
  // also begin a momentary "cue preview" — play from the point for as long as
  // the button is held (released in onSlotRelease). While already playing, a
  // press just jumps, as before.
  const onSlotPress = async (slot: number) => {
    if (!track) return
    const cue = hotcueAt(slot)
    if (!cue) {
      if (refuseCueEdit()) return
      try {
        // Setting a hotcue while a loop is active stores it as a loop hotcue.
        if (loopActive && loopRegion) {
          const len = loopRegion.end - loopRegion.start
          applyCueEdit(await api.createCue(track.id, slot, loopRegion.start, 'loop', len))
        } else {
          const t = snapTime(playbackRef.current?.getPosition() ?? current)
          applyCueEdit(await api.createCue(track.id, slot, t, 'cue'))
        }
        setSelectedSlot(slot)
      } catch (e) {
        onError?.((e as Error).message)
      }
      return
    }
    setSelectedSlot(slot)
    const eng = playbackRef.current
    if (!eng || !eng.ready) return
    resumeAudio()
    enterCue(cue)
    seek(cue.start)
    // Paused at press → momentary preview: play while held.
    if (!eng.playing) beginPreview(`hc:${slot}`, cue.start)
  }

  // Before jumping to a cue: a loop cue engages a loop of its length — the new
  // loop FIRST, so the seek is not wrapped back into the old one. The loop
  // controls follow it: a beat-sized loop takes over the shared size (so −/+
  // resize THIS loop), and one of no preset size shows in MAN, where its toggle
  // is. A point cue outside an engaged loop leaves it.
  const enterCue = (cue: CuePoint) => {
    if (cue.type === 'loop' && cue.length > 0) {
      const beats = beatsForLoop(cue.start, cue.length)
      engagLoop(cue.start, cue.start + cue.length, beats)
      if (beats != null) {
        setBeatSize(beats)
        setLoopMode('beat')
      } else {
        setLoopMode('manual')
      }
    } else {
      leaveLoopFor(cue.start)
    }
  }

  // Hotcue slot released (mouse/touch up, or key up). Ends its momentary preview.
  const onSlotRelease = (slot: number) => endPreview(`hc:${slot}`)

  // Auto Hotcues: the button opens the slot-template dialog; the backend finds
  // the track's structure on its beatgrid and places the template. Requires a
  // beatgrid (button disabled otherwise) — every event is a bar on it.
  const canAutoCue = grid != null
  const [autoOpen, setAutoOpen] = useState(false)
  const openAutoHotcues = () => {
    if (!track || !canAutoCue) return
    if (refuseCueEdit()) return
    setAutoOpen(true)
  }
  const existingHotcues = useMemo(() => {
    const m = new Map<number, CuePoint>()
    for (const c of cueData?.cues ?? []) if (c.role === 'hotcue' && c.slot != null) m.set(c.slot, c)
    return m
  }, [cueData])
  const autoHotcuesDone = (result: AutoHotcuesResult) => {
    applyCueEdit(result.cues)
    const by = (st: string) => result.outcomes.filter((o) => o.status === st)
    const placed = by('placed').length
    const missing = [...by('not_found'), ...by('out_of_range')].map((o) => eventLabel(o.event))
    const kept = by('occupied').length + by('protected').length
    const dupes = by('duplicate').map(
      (o) => `${slotLabel(o.slot)} (same beat as ${slotLabel(o.duplicate_of ?? o.slot)})`,
    )
    const parts = [`Placed ${placed} hotcue${placed === 1 ? '' : 's'}`]
    if (missing.length) parts.push(`not in this track: ${missing.join(', ')}`)
    if (dupes.length) parts.push(`skipped ${dupes.join(', ')}`)
    if (kept) parts.push(`${kept} occupied slot${kept === 1 ? '' : 's'} kept`)
    onNotify?.(placed ? 'success' : 'error', parts.join(' · '))
  }

  const setSlotType = async (slot: number, type: CueType) => {
    if (!track) return
    if (refuseCueEdit()) return
    try {
      applyCueEdit(await api.setCueType(track.id, slot, type))
    } catch (e) {
      onError?.((e as Error).message)
    }
  }

  // Renaming re-sets the slot with its own position, type and length plus the
  // new name — the adapter's set_cue replaces a slot, and keeps a name only
  // when called without one. An empty name clears it.
  const renameSlot = async (slot: number, name: string) => {
    setRenamingSlot(null)
    const cue = hotcueAt(slot)
    if (!track || !cue) return
    if (refuseCueEdit()) return
    try {
      applyCueEdit(await api.createCue(track.id, slot, cue.start, cue.type, cue.length, name))
    } catch (e) {
      onError?.((e as Error).message)
    }
  }

  // Only a fixed palette is offered: a platform with free RGB would need a
  // picker of its own, and none that stores free RGB is writable yet.
  const cuePalette = caps.cues.color === 'palette' ? caps.cues.palette : []
  const setSlotColor = async (slot: number, color: string | null) => {
    if (!track) return
    if (refuseCueEdit()) return
    try {
      applyCueEdit(await api.setCueColor(track.id, slot, color))
    } catch (e) {
      onError?.((e as Error).message)
    }
  }

  const deleteHotcueSlot = async (slot: number) => {
    const existing = hotcueAt(slot)
    if (!track || !existing) return // nothing to remove in an empty slot
    if (!existing.editable) return // the adapter would refuse it
    if (refuseCueEdit()) return
    try {
      applyCueEdit(await api.deleteCue(track.id, slot))
      if (selectedSlot === slot) setSelectedSlot(null)
    } catch (e) {
      onError?.((e as Error).message)
    }
  }

  // ---- memory cues --------------------------------------------------------
  // Cues on no pad, listed in time order (MemoryCueStrip). Calling one is
  // CDJ-style: jump there and make it the CUE point — paused stays paused,
  // playing keeps playing; a memory loop also engages (decided 2026-10-16).
  const showMemory = caps.cues.memory_cues && bankMode === 'memory'
  const memoryPalette = caps.cues.memory_color === 'palette' ? caps.cues.memory_palette : []
  const memoryCount = cueData?.cues.filter((c) => c.role === 'memory' && c.editable).length ?? 0
  const memoryCap = caps.cues.max_memory_cues
  const memoryAddRefusal = !canEditCues
    ? (readOnlyNotice(caps) ?? `Cues cannot be edited on ${caps.save.app_name} libraries yet.`)
    : memoryCap != null && memoryCount >= memoryCap
      ? `${caps.save.app_name} keeps at most ${memoryCap} memory cues per track`
      : null

  const callMemoryCue = (cue: CuePoint) => {
    const eng = playbackRef.current
    if (!eng || !eng.ready) return
    resumeAudio()
    enterCue(cue)
    seek(cue.start)
    setCuePoint(cue.start)
  }

  // + Memory: at the (snapped) playhead, or the engaged loop as a memory loop.
  const addMemoryCue = async () => {
    if (!track) return
    if (memoryAddRefusal) {
      onError?.(memoryAddRefusal)
      return
    }
    try {
      if (loopActive && loopRegion) {
        const len = loopRegion.end - loopRegion.start
        applyCueEdit(await api.addMemoryCue(track.id, loopRegion.start, 'loop', len))
      } else {
        const t = snapTime(playbackRef.current?.getPosition() ?? current)
        applyCueEdit(await api.addMemoryCue(track.id, t, 'cue'))
      }
    } catch (e) {
      onError?.((e as Error).message)
    }
  }

  const editMemoryCue = async (edit: () => Promise<TrackCues>) => {
    if (!track || refuseCueEdit()) return
    try {
      applyCueEdit(await edit())
    } catch (e) {
      onError?.((e as Error).message)
    }
  }

  const memoryMenuItems = (cue: CuePoint): MenuItem[] => {
    const id = cue.id
    const locked = !cue.editable || id == null
    const items: MenuItem[] = []
    // Its type as one-click choices, as on a pad — where the platform has more
    // than one point type (Traktor); a loop stays a loop.
    if (cue.type === 'loop') {
      items.push({ heading: 'Memory loop' })
    } else if (pointCueTypes.length > 1) {
      items.push({ heading: 'Memory cue · Type' })
      for (const t of pointCueTypes) {
        const color = cueTypeColor(t)
        items.push({
          label: CUE_TYPE_LABELS[t],
          icon: (
            <span
              className="block h-2 w-2 rounded-full"
              style={{ background: color, boxShadow: `0 0 6px ${color}` }}
            />
          ),
          hint: t === cue.type ? '✓' : undefined,
          disabled: locked,
          onClick: () =>
            t !== cue.type &&
            id != null &&
            void editMemoryCue(() => api.setMemoryCueType(track!.id, id, t)),
        })
      }
    } else {
      items.push({ heading: 'Memory cue' })
    }
    if (memoryPalette.length > 0) {
      items.push({ separator: true })
      items.push({ heading: 'Colour' })
      items.push({
        swatches: memoryPalette,
        selected: cue.color,
        disabled: locked,
        onPick: (color) =>
          color !== cue.color &&
          id != null &&
          void editMemoryCue(() => api.setMemoryCueColor(track!.id, id, color)),
      })
    }
    if (locked) items.push({ heading: 'Managed by the DJ app — not editable here' })
    items.push({ separator: true })
    items.push({
      label: 'Rename…',
      disabled: locked,
      onClick: () => !refuseCueEdit() && setRenamingMemory(id),
    })
    items.push({
      label: 'Delete',
      danger: true,
      disabled: locked,
      onClick: () => id != null && void editMemoryCue(() => api.deleteMemoryCue(track!.id, id)),
    })
    return items
  }

  const renameMemoryCue = (cue: CuePoint, name: string) => {
    setRenamingMemory(null)
    if (cue.id == null) return
    const id = cue.id
    void editMemoryCue(() => api.renameMemoryCue(track!.id, id, name || null))
  }

  // ---- beatgrid ---------------------------------------------------------
  // Which marker the grid controls act on is derived from the playhead — there
  // is no separate selection state to keep in sync.
  const activeMarkerIndex = grid ? grid.markerIndexAt(current) : -1
  const activeMarker = activeMarkerIndex >= 0 ? grid!.markers[activeMarkerIndex] : null
  // ~4px of the current zoom, so "on the marker" stays usable at 2s and 64s/view.
  // ~4 px of the view either side, so "at a marker" stays a marker-sized
  // target at deep zoom instead of 4% of a 0.25 s view.
  const markerHitSec = Math.max(0.001, secPerView / 250)
  const atMarker = !!grid && grid.isOnMarker(current, markerHitSec)
  const beforeFirstMarker = !!grid && current < grid.markers[0].start - GRID_EPS

  const playheadNow = () => playbackRef.current?.getPosition() ?? current

  const editMarker = async (index: number, patch: { bpm?: number; start?: number }) => {
    if (!track || index < 0) return
    if (refuseGridEdit()) return
    try {
      applyCueEdit(await api.setGridMarker(track.id, index, patch))
    } catch (e) {
      onError?.((e as Error).message)
    }
  }
  const setBpm = (bpm: number) =>
    editMarker(activeMarkerIndex, { bpm: Math.round(bpm * 1000) / 1000 })
  const nudgeBpm = (delta: number) => activeMarker && setBpm(activeMarker.bpm + delta)
  // /2 and x2 retempo the governing marker only, like every other tempo control
  // in this panel — a section can be octave-wrong on its own.
  const halveBpm = () => activeMarker && setBpm(activeMarker.bpm / 2)
  const doubleBpm = () => activeMarker && setBpm(activeMarker.bpm * 2)
  // A beat is the governing marker's own beat: moving a marker by whole beats
  // shifts which beat is the downbeat without changing the phase.
  const nudgeGridBeats = (beats: number) => {
    if (activeMarker) nudgeGrid((beats * 60000) / activeMarker.bpm)
  }
  const nudgeGrid = (deltaMs: number) => {
    if (!activeMarker) return
    editMarker(activeMarkerIndex, { start: Math.max(0, activeMarker.start + deltaMs / 1000) })
  }
  // With no grid, a tempo from TAP or the typed readout CREATES one, with its
  // first marker at an anchor captured when the gesture began: the first tap
  // (a beat, when tapping along), or where the playhead stood when Set grid
  // asked for a BPM. The Grid-mode nudges fix the phase. While that create is
  // in flight, later tempos wait for it rather than creating a second grid.
  const tapAnchorRef = useRef(0)
  const gridAnchorRef = useRef<number | null>(null) // Set grid's, while the readout asks
  const gridCreateRef = useRef<Promise<boolean> | null>(null)
  const [bpmEntryRequest, setBpmEntryRequest] = useState(0) // bump → readout opens for typing
  const tapStart = () => {
    tapAnchorRef.current = playheadNow()
  }
  const commitBpm = async (bpm: number, anchor: number) => {
    if (!track) return
    // This closure predates the grid it is waiting for, so it cannot use
    // activeMarkerIndex — but a grid created here has exactly one marker.
    if (gridCreateRef.current) {
      if (await gridCreateRef.current) editMarker(0, { bpm })
      return
    }
    if (markerCount > 0) return setBpm(bpm)
    if (refuseGridEdit()) return
    const create = api
      .addGridMarker(track.id, anchor, bpm)
      .then((cues) => (applyCueEdit(cues), true))
      .catch((e) => (onError?.((e as Error).message), false))
      .finally(() => (gridCreateRef.current = null))
    gridCreateRef.current = create
    await create
  }
  // Adds a marker at the playhead — and creates the grid when there is none.
  // Uses the raw playhead, not snapTime: a marker defines where beats are.
  // A first marker needs a tempo: the track's own BPM when it has one (passed
  // explicitly — Rekordbox does not fall back to it), else the readout opens
  // to ask, and the marker lands where the playhead is NOW once it is typed.
  const addMarkerHere = async () => {
    if (!track) return
    if (refuseGridEdit()) return
    if (markerCount === 0 && !track.bpm) {
      gridAnchorRef.current = playheadNow()
      setBpmEntryRequest((n) => n + 1)
      return
    }
    try {
      applyCueEdit(
        await api.addGridMarker(track.id, playheadNow(), markerCount === 0 ? (track.bpm ?? undefined) : undefined),
      )
    } catch (e) {
      onError?.((e as Error).message)
    }
  }
  const deleteMarkerHere = async () => {
    if (!track || activeMarkerIndex < 0) return
    if (refuseGridEdit()) return
    try {
      applyCueEdit(await api.deleteGridMarker(track.id, activeMarkerIndex))
    } catch (e) {
      onError?.((e as Error).message)
    }
  }
  // Seeking between markers IS how the user changes which marker is active.
  const seekToMarker = (index: number) => {
    if (!grid || index < 0 || index >= grid.count) return
    seekManual(grid.markers[index].start)
  }
  const prevMarker = () => seekToMarker(grid ? grid.prevMarkerIndex(current, markerHitSec) : -1)
  const nextMarker = () => seekToMarker(grid ? grid.nextMarkerIndex(current, markerHitSec) : -1)
  const resetGrid = async () => {
    const o = originalGridRef.current
    if (!track || !o) return
    if (refuseGridEdit()) return
    try {
      applyCueEdit(await api.replaceGridMarkers(track.id, o))
    } catch (e) {
      onError?.((e as Error).message)
    }
  }
  const toggleLock = async () => {
    if (!track) return
    if (refuseGridEdit()) return
    try {
      applyCueEdit(await api.setGridLock(track.id, !cueData?.grid_locked))
    } catch (e) {
      onError?.((e as Error).message)
    }
  }
  const deleteGrid = async () => {
    if (!track) return
    if (refuseGridEdit()) return
    try {
      applyCueEdit(await api.deleteGrid(track.id))
    } catch (e) {
      onError?.((e as Error).message)
    }
  }
  // Analyze: the backend detects BPM + first beat (setting the grid anchor and,
  // on Traktor, hotcue 1) and the key where the library stores one. The key
  // lands on the track: `applyCueEdit` refetches the lists, and App hands the
  // deck the fresh copy.
  const runAnalyzeGrid = async () => {
    if (!track || gridBusy) return
    setGridBusy(true)
    try {
      if (canEditGrid) {
        applyCueEdit(await api.autoGrid(track.id))
        onNotify?.('success', caps.tracks.key_writable
          ? 'Analyzed — set BPM, grid and key, measured loudness'
          : 'Analyzed — set BPM and grid, measured loudness')
      } else {
        // Nothing here can take a grid (a read-only library, a device, a loose
        // file) — but loudness goes to Konduktor's own store, so measure that.
        const l = await api.measureLoudness(origin, track.id)
        onNotify?.('success', `Measured loudness — ${formatLoudness(l.lufs, 'LUFS')}, ${formatLoudness(l.true_peak, 'dBTP', true)}`)
      }
      qc.invalidateQueries({ queryKey: ['loudness'] })
    } catch (e) {
      onError?.((e as Error).message)
    } finally {
      setGridBusy(false)
    }
  }

  // ---- stems: mute / solo ------------------------------------------------------
  // What each stem plays at: a muted stem is 0. Both engines follow it (the
  // scratch engine re-sums).
  const stemLevels = useMemo(() => (stems ?? []).map((_, k) => (stemMuted[k] ? 0 : 1)), [stems, stemMuted])
  const stemLevelKey = stemLevels.join(',')
  useEffect(() => {
    if (!stems) return
    playbackRef.current?.setGains(stemLevels)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stemLevelKey])
  // Solo = "mute every stem but this one". Soloing a stem that is already the
  // only one playing brings them all back (so solo twice undoes it).
  const toggleStem = (k: number, solo: boolean) => {
    if (!stems || k >= stems.length) return
    setStemMuted((m) => {
      if (!solo) return stems.map((_, i) => (i === k ? !m[i] : !!m[i]))
      const alone = stems.every((_, i) => (i === k ? !m[i] : !!m[i]))
      return stems.map((_, i) => (alone ? false : i !== k))
    })
  }
  // The ring on a lane button: that stem is the only one playing.
  const soloed = stemLevels.filter((l) => l > 0).length === 1 && stemLevels.length > 1
    ? stemLevels.findIndex((l) => l > 0)
    : null
  const lanes: StemLane[] | null =
    stems && stemLanes
      ? stems.map((st, k) => ({
          data: stemLanes[k],
          raw: stemRaws?.[k] ?? null,
          name: st.name,
          color: st.color,
          audible: stemLevels[k] > 0,
          solo: soloed === k,
          shortcut: STEM_KEYS[k] ? STEM_KEYS[k].slice(3) : '',
        }))
      : null

  // The keyboard's handle on this deck (routed by lib/deckKeys.ts).
  const shortcuts: DeckShortcuts = {
    toggle,
    onSlotPress,
    onSlotRelease,
    deleteHotcueSlot,
    onCuePress,
    onCueRelease,
    moveOrJump,
    stepSize,
    prevMarker,
    nextMarker,
    slotCount,
    toggleStem,
  }

  const showWaves = track && cols && waveStatus === 'ready'
  const activeLoop = loopActive && loopRegion ? loopRegion : null
  const markerCount = grid?.count ?? 0
  const subtitle = track
    ? [track.artist ?? 'Unknown artist', track.album, track.genre].filter(Boolean).join(' · ')
    : ''

  // The pad's right-click menu: its type (as one-click choices, the current one
  // ticked), its colour where the platform has a palette, Rename, Delete. A cue the adapter will not edit gets the same menu
  // disabled, so the reason is visible instead of the menu silently not opening.
  const padMenuItems = (slot: number): MenuItem[] => {
    const cue = hotcueAt(slot)
    if (!cue) return []
    const locked = !cue.editable
    const items: MenuItem[] = []
    if (cue.type === 'loop') {
      items.push({ heading: `Hotcue ${slotLabel(slot)} · Loop` })
    } else if (pointCueTypes.length > 1) {
      items.push({ heading: `Hotcue ${slotLabel(slot)} · Type` })
      for (const t of pointCueTypes) {
        const color = cueTypeColor(t)
        items.push({
          label: CUE_TYPE_LABELS[t],
          icon: (
            <span
              className="block h-2 w-2 rounded-full"
              style={{ background: color, boxShadow: `0 0 6px ${color}` }}
            />
          ),
          hint: t === cue.type ? '✓' : undefined,
          disabled: locked,
          onClick: () => t !== cue.type && void setSlotType(slot, t),
        })
      }
    } else {
      items.push({ heading: `Hotcue ${slotLabel(slot)}` })
    }
    if (cuePalette.length > 0) {
      items.push({ separator: true })
      items.push({ heading: 'Colour' })
      items.push({
        swatches: cuePalette,
        selected: cue.color,
        disabled: locked,
        onPick: (color) => color !== cue.color && void setSlotColor(slot, color),
      })
    }
    if (locked) items.push({ heading: 'Managed by the DJ app — not editable here' })
    items.push({ separator: true })
    items.push({
      label: 'Rename…',
      disabled: locked,
      onClick: () => !refuseCueEdit() && setRenamingSlot(slot),
    })
    items.push({
      label: 'Delete',
      danger: true,
      disabled: locked,
      onClick: () => void deleteHotcueSlot(slot),
    })
    return items
  }

  // Rendered twice — inline on a wide row, inside TempoFold on a narrow one.
  const tempoProps = {
    bpm: activeMarker?.bpm ?? null,
    locked: cueData?.grid_locked ?? false,
    lockable: caps.grid.lockable,
    flexible: markerCount > 1,
    markerIndex: activeMarkerIndex,
    onTapStart: tapStart,
    onTapBpm: (bpm: number) => commitBpm(bpm, tapAnchorRef.current),
    onNudgeBpm: nudgeBpm,
    onHalve: halveBpm,
    onDouble: doubleBpm,
    onToggleLock: toggleLock,
  }

  return {
    caps,
    track,
    origin,
    onError,
    onEditTags,
    subtitle,
    playing,
    previewing,
    cueHeld,
    current,
    duration,
    ready,
    cols,
    raw,
    stems,
    lanes,
    waveStatus,
    showWaves,
    cueData,
    grid,
    markerCount,
    activeMarker,
    activeMarkerIndex,
    atMarker,
    beforeFirstMarker,
    originalGridRef,
    snap,
    setSnap,
    selectedSlot,
    setSelectedSlot,
    cuePoint,
    infoOpen,
    setInfoOpen,
    beatSize,
    loopMode,
    setLoopMode,
    gridMode,
    setGridMode,
    padMenu,
    setPadMenu,
    renamingSlot,
    setRenamingSlot,
    bankMode,
    setBankMode,
    memoryMenu,
    setMemoryMenu,
    renamingMemory,
    setRenamingMemory,
    loopRegion,
    loopActive,
    activeBeats,
    loopInPoint,
    activeLoop,
    gridBusy,
    canEditCues,
    canEditGrid,
    slotCount,
    slotLabel,
    toggle,
    pause,
    onCuePress,
    onCueRelease,
    seekManual,
    moveOrJump,
    onScratchStart,
    onScratchMove,
    onScratchEnd,
    stepSize,
    toggleSizeLoop,
    loopIn,
    loopOut,
    toggleLoop,
    hotcueAt,
    onSlotPress,
    onSlotRelease,
    renameSlot,
    padMenuItems,
    canAutoCue,
    autoOpen,
    setAutoOpen,
    openAutoHotcues,
    existingHotcues,
    autoHotcuesDone,
    showMemory,
    memoryAddRefusal,
    callMemoryCue,
    addMemoryCue,
    memoryMenuItems,
    renameMemoryCue,
    playheadNow,
    commitBpm,
    gridAnchorRef,
    bpmEntryRequest,
    tempoProps,
    prevMarker,
    nextMarker,
    nudgeGrid,
    nudgeGridBeats,
    addMarkerHere,
    deleteMarkerHere,
    resetGrid,
    deleteGrid,
    runAnalyzeGrid,
    toggleStem,
    shortcuts,
  }
}

export type Deck = ReturnType<typeof useDeck>
