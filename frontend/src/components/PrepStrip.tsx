import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  api,
  type AutoHotcuesResult,
  type CuePoint,
  type CueType,
  type GridMarker,
  type Track,
  type TrackCues,
} from '../api'
import { buildBeatGrid, GRID_EPS } from '../lib/beatgrid'
import { slotLabeller, useCaps } from '../lib/capabilities'
import { CUE_TYPE_LABELS } from '../lib/cues'
import { readOnlyShort, readOnlyNotice } from '../lib/platformCopy'
import { analyzeWaveform, type WaveColumn } from '../lib/waveform'
import { ScratchEngine } from '../lib/scratchEngine'
import { PlaybackEngine } from '../lib/playbackEngine'
import { AutoCueDialog, eventLabel } from './AutoCueDialog'
import { BeatJumpControls, BEAT_JUMP_SIZES } from './BeatJumpControls'
import { GridControls } from './GridControls'
import { HotcueBar } from './HotcueBar'
import { LoopControls } from './LoopControls'
import { MainWaveform, MIN_SEC, MAX_SEC, DEFAULT_SEC } from './MainWaveform'
import { OverviewWaveform } from './OverviewWaveform'

interface Props {
  /** The track currently loaded into the prep deck, if any. */
  track: Track | null
  /** Bumped by the library's per-row play button to load + auto-play. */
  playRequest?: number
  onError?: (msg: string) => void
  /** Neutral/success feedback (e.g. Auto Hotcues result). */
  onNotify?: (kind: 'success' | 'error', msg: string) => void
  /**
   * The track came from a browsed DEVICE, not the loaded library, so its audio
   * and cues must be read from the source endpoints. Every EDIT control is
   * already inert here — a device's capabilities say `writable: false`, and the
   * deck gates on those — so this only needs to redirect the two READS.
   */
  fromDevice?: boolean
}

function fmt(secs: number): string {
  if (!isFinite(secs) || secs < 0) return '0:00'
  const m = Math.floor(secs / 60)
  const s = Math.floor(secs % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

/**
 * Digit key -> bank slot: 1..9 map to slots 0..8 and 0 maps to slot 10.
 *
 * Ten digits is the physical ceiling, so a bank larger than that simply has no
 * shortcut for its tail. Returns null when the key is not a digit or the slot is
 * beyond this platform's bank — an 8-slot bank behaves exactly as before.
 */
const DIGIT_RE = /^(?:Digit|Numpad)([0-9])$/

function slotForDigit(code: string, slotCount: number): number | null {
  const m = code.match(DIGIT_RE)
  if (!m) return null
  const slot = m[1] === '0' ? 9 : Number(m[1]) - 1
  return slot < slotCount ? slot : null
}

const LOOP_SIZES = [1 / 32, 1 / 16, 1 / 8, 1 / 4, 1 / 2, 1, 2, 4, 8, 16, 32]

/**
 * The DJ-style "prep strip" across the top of the window: transport controls on
 * the left, waveforms on the right (a scrolling zoomable main view above a
 * whole-track overview), plus loop + hotcue controls. Playback runs through the
 * Web Audio PlaybackEngine (seamless loops); the same decoded buffer feeds the
 * scratch engine and both waveform views.
 */
export function PrepStrip({ track, playRequest = 0, onError, onNotify, fromDevice = false }: Props) {
  const qc = useQueryClient()
  const [playing, setPlaying] = useState(false)
  const [previewing, setPreviewing] = useState(false) // momentary hold-to-play active
  const [current, setCurrent] = useState(0)
  const [duration, setDuration] = useState(0)
  const [ready, setReady] = useState(false)
  const [cols, setCols] = useState<WaveColumn[] | null>(null)
  const [waveStatus, setWaveStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [cueData, setCueData] = useState<TrackCues | null>(null)
  // The beatgrid, rebuilt whenever the cue data is replaced (every edit returns
  // a fresh TrackCues, so this stays in step automatically). Null = no grid.
  const grid = useMemo(() => buildBeatGrid(cueData?.grid_markers), [cueData?.grid_markers])
  const [snap, setSnap] = useState(true)
  const [selectedSlot, setSelectedSlot] = useState<number | null>(null)
  const [cuePoint, setCuePoint] = useState(0) // floating "CUE" point (frontend-only)
  const [secPerView, setSecPerView] = useState(DEFAULT_SEC) // main-waveform zoom (persisted)
  const [jumpBeats, setJumpBeats] = useState(4) // beat-jump size in beats (persisted)

  // Loop state (transient — persisted only when saved as a hotcue later).
  const [loopRegion, setLoopRegion] = useState<{ start: number; end: number } | null>(null)
  const [loopActive, setLoopActive] = useState(false)
  const [activeBeats, setActiveBeats] = useState<number | null>(null)
  const loopInRef = useRef<number | null>(null) // armed manual loop-in point
  const originalGridRef = useRef<GridMarker[] | null>(null)

  const audioCtxRef = useRef<AudioContext | null>(null) // playback
  const scratchCtxRef = useRef<AudioContext | null>(null) // scratch (kept separate!)
  const playbackRef = useRef<PlaybackEngine | null>(null)
  const scratchRef = useRef<ScratchEngine | null>(null)
  const wasPlayingRef = useRef(false)
  const scratchRafRef = useRef(0)
  const engagedRef = useRef(false) // scratch engine is dragging or coasting
  const pendingPlayRef = useRef(false) // a play was requested; start once ready
  const loadedIdRef = useRef<string | null>(null) // trackId whose buffer is loaded
  const [gridBusy, setGridBusy] = useState(false)
  // Active momentary "cue preview": a hotcue OR the CUE button held while paused
  // plays from its point and stops on release, unless `latched` (play pressed
  // during the hold). `id` identifies the holder so its release matches.
  const previewRef = useRef<{ id: string; start: number; latched: boolean } | null>(null)

  const trackId = track?.id ?? null

  // Main-waveform zoom persists to userprefs.json so it survives track switches
  // (MainWaveform unmounts during each load) and app restarts. Hydrate once from
  // the shared ['prefs'] query, then persist changes debounced (mirrors the
  // column-layout prefs flow in App).
  const prefsQuery = useQuery({ queryKey: ['prefs'], queryFn: api.getPrefs })
  const zoomHydratedRef = useRef(false)
  const zoomSaveTimer = useRef<number | null>(null)
  useEffect(() => {
    if (zoomHydratedRef.current || !prefsQuery.data) return
    zoomHydratedRef.current = true
    const z = (prefsQuery.data as { mainZoomSec?: unknown }).mainZoomSec
    if (typeof z === 'number' && isFinite(z)) {
      setSecPerView(Math.min(MAX_SEC, Math.max(MIN_SEC, z)))
    }
  }, [prefsQuery.data])
  useEffect(() => {
    if (!zoomHydratedRef.current) return // don't clobber saved prefs pre-hydration
    if (zoomSaveTimer.current) window.clearTimeout(zoomSaveTimer.current)
    zoomSaveTimer.current = window.setTimeout(() => {
      api.patchPrefs({ mainZoomSec: secPerView }).catch(() => {})
    }, 500)
    return () => {
      if (zoomSaveTimer.current) window.clearTimeout(zoomSaveTimer.current)
    }
  }, [secPerView])

  // Beat-jump size persists the same way (survives track switches + restarts).
  const jumpHydratedRef = useRef(false)
  const jumpSaveTimer = useRef<number | null>(null)
  useEffect(() => {
    if (jumpHydratedRef.current || !prefsQuery.data) return
    jumpHydratedRef.current = true
    const b = (prefsQuery.data as { beatJumpBeats?: unknown }).beatJumpBeats
    if (typeof b === 'number' && BEAT_JUMP_SIZES.includes(b)) setJumpBeats(b)
  }, [prefsQuery.data])
  useEffect(() => {
    if (!jumpHydratedRef.current) return // don't clobber saved prefs pre-hydration
    if (jumpSaveTimer.current) window.clearTimeout(jumpSaveTimer.current)
    jumpSaveTimer.current = window.setTimeout(() => {
      api.patchPrefs({ beatJumpBeats: jumpBeats }).catch(() => {})
    }, 500)
    return () => {
      if (jumpSaveTimer.current) window.clearTimeout(jumpSaveTimer.current)
    }
  }, [jumpBeats])

  const getCtx = (): AudioContext => {
    if (!audioCtxRef.current) {
      audioCtxRef.current = new (window.AudioContext ??
        (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)()
    }
    void audioCtxRef.current.resume()
    return audioCtxRef.current
  }

  // Tear everything down when the strip itself unmounts (e.g. the collection
  // picker early-returns and replaces the whole UI). Without this, the playback
  // source keeps running in an AudioContext that's never closed, so a fresh
  // remount plays a SECOND track on top of the orphaned one. Closing both
  // contexts is the surefire kill; runs once, on unmount.
  useEffect(() => {
    return () => {
      playbackRef.current?.pause()
      scratchRef.current?.dispose()
      void audioCtxRef.current?.close()
      void scratchCtxRef.current?.close()
      audioCtxRef.current = null
      scratchCtxRef.current = null
    }
  }, [])

  // The scratch engine gets its OWN AudioContext: a ScriptProcessorNode is
  // unreliable sharing a context with the playback source node (it goes silent
  // once a source has played), so we keep them fully separate.
  const getScratchCtx = (): AudioContext => {
    if (!scratchCtxRef.current) {
      scratchCtxRef.current = new (window.AudioContext ??
        (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)()
    }
    void scratchCtxRef.current.resume()
    return scratchCtxRef.current
  }

  // Reset transport + loop state when the loaded track changes.
  useEffect(() => {
    playbackRef.current?.pause()
    setPlaying(false)
    setCurrent(0)
    setDuration(0)
    setReady(false)
    setSelectedSlot(null)
    setLoopRegion(null)
    setLoopActive(false)
    setActiveBeats(null)
    setCuePoint(0)
    loopInRef.current = null
    previewRef.current = null
    setPreviewing(false)
  }, [trackId])

  // Analyse once per track; the decoded buffer feeds both the playback and
  // scratch engines (no re-decode) and both waveform views share the columns.
  useEffect(() => {
    scratchRef.current?.dispose()
    scratchRef.current = null
    if (!trackId) {
      setCols(null)
      setWaveStatus('loading')
      return
    }
    let cancelled = false
    setCols(null)
    setWaveStatus('loading')
    analyzeWaveform(fromDevice ? api.sourceAudioUrl(trackId) : api.audioUrl(trackId))
      .then((res) => {
        if (cancelled) return
        setCols(res.cols)
        setWaveStatus('ready')
        const ctx = getCtx()
        const sc = new ScratchEngine()
        sc.load(res.buffer)
        sc.attach(getScratchCtx()) // own context; warm up so the first scratch isn't silent
        scratchRef.current = sc
        if (!playbackRef.current) {
          playbackRef.current = new PlaybackEngine()
          playbackRef.current.setOnEnded(() => setPlaying(false))
        }
        playbackRef.current.load(ctx, res.buffer)
        setDuration(res.buffer.duration)
        setReady(true)
        loadedIdRef.current = trackId
        // A play was requested (library row button) → start now that it's ready.
        if (pendingPlayRef.current) {
          pendingPlayRef.current = false
          playbackRef.current.play()
          setPlaying(true)
        }
      })
      .catch(() => {
        if (!cancelled) setWaveStatus('error')
      })
    return () => {
      cancelled = true
    }
  }, [trackId, fromDevice])

  // Fetch beatgrid + cue markers for the loaded track.
  useEffect(() => {
    if (!trackId) {
      setCueData(null)
      return
    }
    let cancelled = false
    setCueData(null)
    ;(fromDevice ? api.sourceTrackCues(trackId) : api.trackCues(trackId))
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
  }, [trackId, fromDevice])

  const toggle = () => {
    const eng = playbackRef.current
    if (!eng || !eng.ready) return
    getCtx() // ensure the context resumes on this user gesture
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
    getCtx()
    if (eng.playing) {
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
      beginPreview('cue', cuePoint)
    }
  }
  const onCueRelease = () => endPreview('cue')

  // A library row's play button bumps `playRequest`: load (if needed) + play.
  // If the requested track is already loaded and ready, start immediately;
  // otherwise flag it and the analysis effect starts playback once ready.
  useEffect(() => {
    if (playRequest === 0) return
    pendingPlayRef.current = true
    const eng = playbackRef.current
    if (loadedIdRef.current === trackId && eng?.ready) {
      pendingPlayRef.current = false
      getCtx()
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

  // User-driven seek from the waveforms: navigating away drops the active loop
  // (otherwise it would just pull playback back into the loop region).
  const seekManual = (t: number) => {
    if (loopActive) {
      playbackRef.current?.setLoopEnabled(false)
      setLoopActive(false)
    }
    seek(t)
  }

  // ---- beat jump --------------------------------------------------------
  // Step the jump size through BEAT_JUMP_SIZES, clamping at both ends (no wrap).
  const stepJumpSize = (dir: -1 | 1) => {
    setJumpBeats((b) => {
      const i = BEAT_JUMP_SIZES.indexOf(b)
      const next = Math.min(BEAT_JUMP_SIZES.length - 1, Math.max(0, (i < 0 ? 4 : i) + dir))
      return BEAT_JUMP_SIZES[next]
    })
  }

  // Move the playhead exactly `jumpBeats` beats fwd/back, preserving sub-beat
  // phase (pure translation, no grid snap). Like seekManual, jumping escapes an
  // active loop. seek() clamps to [0, duration], preserves play/pause state.
  const beatJump = (dir: -1 | 1) => {
    const eng = playbackRef.current
    if (!eng || !grid) return
    if (loopActive) {
      eng.setLoopEnabled(false)
      setLoopActive(false)
    }
    // Phase is preserved in BEAT space, so a jump crossing a tempo change lands
    // on the musically right beat rather than a fixed number of seconds away.
    eng.seek(grid.advanceBeats(eng.getPosition(), dir * jumpBeats))
    setCurrent(eng.getPosition())
  }

  // ---- scratch ----------------------------------------------------------
  const finalizeScratch = () => {
    cancelAnimationFrame(scratchRafRef.current)
    engagedRef.current = false
    const sc = scratchRef.current
    if (!sc) return
    const finalSec = sc.end()
    const pb = playbackRef.current
    if (pb) pb.seek(finalSec)
    setCurrent(finalSec)
    if (wasPlayingRef.current && pb) {
      pb.play()
      setPlaying(true)
    }
  }

  const runScratchRaf = () => {
    cancelAnimationFrame(scratchRafRef.current)
    const sc = scratchRef.current!
    const tick = () => {
      setCurrent(sc.getPositionSec())
      if (sc.finished) {
        finalizeScratch()
        return
      }
      scratchRafRef.current = requestAnimationFrame(tick)
    }
    scratchRafRef.current = requestAnimationFrame(tick)
  }

  const onScratchStart = () => {
    const sc = scratchRef.current
    if (engagedRef.current && sc) {
      sc.regrab() // re-grab while coasting
      return
    }
    const pb = playbackRef.current
    wasPlayingRef.current = !!pb && pb.playing
    if (pb && pb.playing) {
      pb.pause()
      setPlaying(false)
    }
    if (sc && sc.loaded) {
      sc.begin(getScratchCtx(), current)
      engagedRef.current = true
      runScratchRaf()
    }
  }

  const onScratchMove = (t: number) => {
    if (engagedRef.current) scratchRef.current!.setTargetSec(t)
    else setCurrent(t)
  }

  const onScratchEnd = (t: number) => {
    if (engagedRef.current) {
      scratchRef.current!.release(wasPlayingRef.current)
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
    loopInRef.current = null
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
    loopInRef.current = snapTime(eng.getPosition())
  }

  const loopOut = () => {
    const eng = playbackRef.current
    if (!eng || !eng.ready || loopInRef.current == null) return
    const end = snapTime(eng.getPosition())
    if (end <= loopInRef.current) return
    engagLoop(loopInRef.current, end, null)
  }

  const toggleLoop = () => {
    const eng = playbackRef.current
    if (!eng || !loopRegion) return
    const next = !loopActive
    eng.setLoopEnabled(next)
    setLoopActive(next)
    setCurrent(eng.getPosition())
  }

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
  const caps = useCaps()
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
    qc.invalidateQueries({ queryKey: ['state'] })
    qc.invalidateQueries({ queryKey: ['tracks'] })
    qc.invalidateQueries({ queryKey: ['playlist'] })
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
    // A grid marker's companion cue holds this slot. It belongs to the beatgrid,
    // so pressing it only seeks — never create, retype, select or preview.
    if (cue?.grid_marker != null) {
      seekManual(cue.start)
      return
    }
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
    getCtx()
    // A loop hotcue jumps to its start AND re-engages a loop of its length.
    if (cue.type === 'loop' && cue.length > 0) {
      seek(cue.start)
      engagLoop(cue.start, cue.start + cue.length, beatsForLoop(cue.start, cue.length))
    } else {
      seek(cue.start)
    }
    // Paused at press → momentary preview: play while held.
    if (!eng.playing) beginPreview(`hc:${slot}`, cue.start)
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
    const parts = [`Placed ${placed} hotcue${placed === 1 ? '' : 's'}`]
    if (missing.length) parts.push(`not in this track: ${missing.join(', ')}`)
    if (kept) parts.push(`${kept} occupied slot${kept === 1 ? '' : 's'} kept`)
    onNotify?.(placed ? 'success' : 'error', parts.join(' · '))
  }

  const changeSelectedType = async (type: CueType) => {
    if (!track || selectedSlot == null) return
    if (refuseCueEdit()) return
    try {
      applyCueEdit(await api.setCueType(track.id, selectedSlot, type))
    } catch (e) {
      onError?.((e as Error).message)
    }
  }

  const deleteSelected = async () => {
    if (!track || selectedSlot == null) return
    if (refuseCueEdit()) return
    try {
      applyCueEdit(await api.deleteCue(track.id, selectedSlot))
      setSelectedSlot(null)
    } catch (e) {
      onError?.((e as Error).message)
    }
  }

  const deleteHotcueSlot = async (slot: number) => {
    const existing = hotcueAt(slot)
    if (!track || !existing) return // nothing to remove in an empty slot
    if (existing.grid_marker != null) return // beatgrid-owned: delete the marker instead
    if (refuseCueEdit()) return
    try {
      applyCueEdit(await api.deleteCue(track.id, slot))
      if (selectedSlot === slot) setSelectedSlot(null)
    } catch (e) {
      onError?.((e as Error).message)
    }
  }

  // ---- beatgrid ---------------------------------------------------------
  // Which marker the grid controls act on is derived from the playhead — there
  // is no separate selection state to keep in sync.
  const activeMarkerIndex = grid ? grid.markerIndexAt(current) : -1
  const activeMarker = activeMarkerIndex >= 0 ? grid!.markers[activeMarkerIndex] : null
  // ~4px of the current zoom, so "on the marker" stays usable at 2s and 64s/view.
  const markerHitSec = Math.max(0.01, secPerView / 250)
  const atMarker = !!grid && grid.isOnMarker(current, markerHitSec)
  const beforeFirstMarker = !!grid && current < grid.markers[0].start - GRID_EPS
  // Beat jump needs a grid to size beats; disabled on ungridded tracks.
  const canBeatJump = ready && grid != null

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
  const nudgeGrid = (deltaMs: number) => {
    if (!activeMarker) return
    editMarker(activeMarkerIndex, { start: Math.max(0, activeMarker.start + deltaMs / 1000) })
  }
  // Adds a marker at the playhead — and creates the grid when there is none.
  // Uses the raw playhead, not snapTime: a marker defines where beats are.
  const addMarkerHere = async () => {
    if (!track) return
    if (refuseGridEdit()) return
    try {
      applyCueEdit(await api.addGridMarker(track.id, playheadNow()))
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
  // Analyze: backend detects BPM + first beat, sets the grid anchor and hotcue 1.
  const runAnalyzeGrid = async () => {
    if (!track || gridBusy) return
    if (refuseGridEdit()) return
    setGridBusy(true)
    try {
      applyCueEdit(await api.autoGrid(track.id))
      onNotify?.('success', 'Analyzed — set BPM, grid, and hotcue 1')
    } catch (e) {
      onError?.((e as Error).message)
    } finally {
      setGridBusy(false)
    }
  }

  // ---- keyboard shortcuts ----------------------------------------------
  // Space → play/pause; 1–8 → the matching hotcue slot; Shift+1–8 → delete it.
  // ←/→ → beat jump; Shift+←/→ → step between grid markers.
  // Digits are read from e.code (layout-/Shift-independent) and a ref holds the
  // latest handlers so the listener attaches once and never goes stale.
  const shortcutsRef = useRef({
    toggle,
    onSlotPress,
    onSlotRelease,
    deleteHotcueSlot,
    onCuePress,
    onCueRelease,
    beatJump,
    prevMarker,
    nextMarker,
    slotCount,
  })
  shortcutsRef.current = {
    toggle,
    onSlotPress,
    onSlotRelease,
    deleteHotcueSlot,
    onCuePress,
    onCueRelease,
    beatJump,
    prevMarker,
    nextMarker,
    slotCount,
  }
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Ignore while typing in a field or with a non-Shift modifier held.
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
      if (e.metaKey || e.ctrlKey || e.altKey) return
      if (e.repeat) return // held key auto-repeats — treat as one press+hold
      if (e.code === 'Space') {
        e.preventDefault()
        shortcutsRef.current.toggle()
        return
      }
      if (e.code === 'KeyC') {
        e.preventDefault()
        shortcutsRef.current.onCuePress()
        return
      }
      if (e.code === 'ArrowLeft') {
        e.preventDefault()
        if (e.shiftKey) shortcutsRef.current.prevMarker()
        else shortcutsRef.current.beatJump(-1)
        return
      }
      if (e.code === 'ArrowRight') {
        e.preventDefault()
        if (e.shiftKey) shortcutsRef.current.nextMarker()
        else shortcutsRef.current.beatJump(1)
        return
      }
      const slot = slotForDigit(e.code, shortcutsRef.current.slotCount)
      if (slot != null) {
        e.preventDefault()
        if (e.shiftKey) void shortcutsRef.current.deleteHotcueSlot(slot)
        else void shortcutsRef.current.onSlotPress(slot)
      }
    }
    // keyup ends a held hotcue's momentary preview (release).
    const onKeyRelease = (e: KeyboardEvent) => {
      if (e.code === 'KeyC') shortcutsRef.current.onCueRelease()
      const slot = slotForDigit(e.code, shortcutsRef.current.slotCount)
      if (slot != null) shortcutsRef.current.onSlotRelease(slot)
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('keyup', onKeyRelease)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('keyup', onKeyRelease)
    }
  }, [])

  const selectedCue = selectedSlot != null ? hotcueAt(selectedSlot) : null
  const showWaves = track && cols && waveStatus === 'ready'
  const activeLoop = loopActive && loopRegion ? loopRegion : null

  return (
    <div className="flex h-[25.5rem] shrink-0 items-stretch gap-px border-b border-line bg-ink-950">
      {/* Controls */}
      <div className="flex w-72 shrink-0 flex-col gap-3 bg-ink-900 px-4 py-3">
        <div className="min-w-0">
          {track ? (
            <>
              <div className="truncate text-sm font-semibold text-text" title={track.title ?? ''}>
                {track.title ?? 'Untitled'}
              </div>
              <div className="truncate text-xs text-muted" title={track.artist ?? ''}>
                {track.artist ?? 'Unknown artist'}
              </div>
              {/* The deck stays usable for listening on a read-only library, so
                  say which it is rather than leaving the edit controls looking
                  live. The controls themselves report the reason when pressed. */}
              {readOnlyShort(caps) && (
                <div
                  className="mt-1 inline-block rounded bg-ink-800 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-faint"
                  title={readOnlyNotice(caps) ?? ''}
                >
                  {readOnlyShort(caps)}
                </div>
              )}
            </>
          ) : (
            <div className="text-xs uppercase tracking-wider text-faint">No track loaded</div>
          )}
        </div>

        {track && (
          <GridControls
            bpm={activeMarker?.bpm ?? null}
            markerIndex={activeMarkerIndex}
            markerCount={grid?.count ?? 0}
            markerStart={activeMarker?.start ?? null}
            atMarker={atMarker}
            beforeFirst={beforeFirstMarker}
            locked={cueData?.grid_locked ?? false}
            canReset={originalGridRef.current != null}
            onSetBpm={setBpm}
            onNudgeBpm={nudgeBpm}
            onHalve={halveBpm}
            onDouble={doubleBpm}
            onNudgeMarker={nudgeGrid}
            onAddMarker={addMarkerHere}
            onDeleteMarker={deleteMarkerHere}
            onPrevMarker={prevMarker}
            onNextMarker={nextMarker}
            onReset={resetGrid}
            onToggleLock={toggleLock}
            onDeleteGrid={deleteGrid}
            onAnalyze={runAnalyzeGrid}
            analyzing={gridBusy}
          />
        )}

        <div className="mt-auto flex items-center gap-2">
          <button
            onPointerDown={(e) => {
              e.preventDefault()
              e.currentTarget.setPointerCapture(e.pointerId)
              onCuePress()
            }}
            onPointerUp={onCueRelease}
            onPointerCancel={onCueRelease}
            disabled={!ready}
            className="flex h-11 items-center justify-center rounded-xl border border-line bg-ink-850 px-4 text-sm font-bold tracking-wide text-gold transition-colors hover:border-gold disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:border-line"
            title="Cue — set the cue point (paused), hold to preview from it, or jump back to it (playing)"
          >
            CUE
          </button>
          <button
            onClick={toggle}
            disabled={!ready}
            className={
              'flex h-11 w-11 items-center justify-center rounded-xl transition-all active:scale-95 disabled:cursor-not-allowed disabled:opacity-40 ' +
              (previewing
                ? 'bg-gold text-ink-950 shadow-lg shadow-gold/20 hover:brightness-110'
                : playing
                  ? 'bg-mint text-ink-950 shadow-lg shadow-mint/20 hover:brightness-110'
                  : 'border border-line bg-ink-850 text-text hover:border-accent disabled:hover:border-line')
            }
            title={playing ? 'Pause' : 'Play'}
          >
            {playing ? (
              <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
                <rect x="6" y="5" width="4" height="14" rx="1" />
                <rect x="14" y="5" width="4" height="14" rx="1" />
              </svg>
            ) : (
              <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
                <path d="M8 5.14v13.72a1 1 0 0 0 1.54.84l10.29-6.86a1 1 0 0 0 0-1.68L9.54 4.3A1 1 0 0 0 8 5.14z" />
              </svg>
            )}
          </button>
          <div className="tabular-nums text-lg font-medium text-muted">
            {fmt(current)} / {fmt(duration)}
          </div>
        </div>

        <BeatJumpControls
          beats={jumpBeats}
          onStep={stepJumpSize}
          onJump={beatJump}
          disabled={!canBeatJump}
        />
      </div>

      {/* Waveforms + loop/hotcue controls. */}
      <div className="relative flex min-w-0 flex-1 flex-col bg-ink-900">
        {!track ? (
          <div className="flex flex-1 items-center justify-center text-xs text-faint">
            Load a track to prep it
          </div>
        ) : waveStatus === 'error' ? (
          <div className="flex flex-1 items-center justify-center text-xs text-pink">
            Could not load audio — file may be missing or an unsupported format.
          </div>
        ) : (
          <>
            <div className="relative min-h-0 flex-1">
              {showWaves ? (
                <MainWaveform
                  cols={cols}
                  currentTime={current}
                  duration={duration}
                  cues={cueData?.cues ?? []}
                  cuePoint={cuePoint}
                  grid={grid}
                  activeMarker={activeMarkerIndex}
                  loop={activeLoop}
                  secPerView={secPerView}
                  onZoomChange={setSecPerView}
                  onSeek={seekManual}
                  onScratchStart={onScratchStart}
                  onScratchMove={onScratchMove}
                  onScratchEnd={onScratchEnd}
                />
              ) : (
                <div className="flex h-full items-center justify-center text-xs text-faint">
                  Analysing waveform…
                </div>
              )}
            </div>
            <div className="h-10 shrink-0 border-t border-line">
              {showWaves && (
                <OverviewWaveform
                  cols={cols}
                  currentTime={current}
                  duration={duration}
                  cues={cueData?.cues ?? []}
                  cuePoint={cuePoint}
                  loop={activeLoop}
                  onSeek={seekManual}
                />
              )}
            </div>

            <LoopControls
              hasGrid={grid != null}
              active={loopActive}
              activeBeats={activeBeats}
              canToggle={loopRegion != null}
              snap={snap}
              onToggleSnap={() => setSnap((s) => !s)}
              onSetLoop={setBeatLoop}
              onLoopIn={loopIn}
              onLoopOut={loopOut}
              onToggleActive={toggleLoop}
            />

            {/* Hotcue row: label · 8 slots · type of selected cue · delete. */}
            <div className="flex h-10 shrink-0 items-stretch gap-px border-t border-line bg-ink-950">
              <span className="flex w-16 items-center justify-center bg-ink-900 text-[10px] font-semibold uppercase tracking-wider text-faint">
                Cues
              </span>
              <HotcueBar
                cues={cueData?.cues ?? []}
                slotCount={slotCount}
                slotLabel={slotLabel}
                selectedSlot={selectedSlot}
                onSlotPress={onSlotPress}
                onSlotRelease={onSlotRelease}
              />
              <div className="flex w-28 items-center justify-center bg-ink-900 px-1">
                {selectedCue && selectedCue.type === 'loop' ? (
                  <span className="text-sm font-semibold text-mint">Loop</span>
                ) : pointCueTypes.length < 2 ? (
                  // One point type means there is nothing to choose between —
                  // show it rather than a dropdown that cannot change anything.
                  <span className="text-sm font-semibold text-text">
                    {selectedCue ? CUE_TYPE_LABELS[selectedCue.type] : '—'}
                  </span>
                ) : (
                  <select
                    value={selectedCue ? selectedCue.type : ''}
                    disabled={!selectedCue}
                    onChange={(e) => changeSelectedType(e.target.value as CueType)}
                    className="w-full bg-transparent text-center text-sm font-semibold text-text outline-none disabled:opacity-40"
                  >
                    {!selectedCue && <option value="">—</option>}
                    {pointCueTypes.map((t) => (
                      <option key={t} value={t}>
                        {CUE_TYPE_LABELS[t]}
                      </option>
                    ))}
                  </select>
                )}
              </div>
              <button
                onClick={openAutoHotcues}
                disabled={!canAutoCue}
                title={
                  canAutoCue
                    ? 'Place hotcues on the track\'s drops, breakdowns and other sections'
                    : 'Set a beatgrid first'
                }
                className="flex w-16 items-center justify-center gap-1 bg-ink-900 text-[11px] font-semibold uppercase tracking-wider text-muted transition-colors hover:bg-ink-800 hover:text-accent disabled:opacity-30 disabled:hover:bg-ink-900 disabled:hover:text-muted"
              >
                ✨ Auto
              </button>
              <button
                onClick={deleteSelected}
                disabled={!selectedCue}
                title="Delete selected hotcue"
                className="flex w-12 items-center justify-center bg-ink-900 text-muted transition-colors hover:bg-ink-800 hover:text-pink disabled:opacity-30 disabled:hover:bg-ink-900 disabled:hover:text-muted"
              >
                🗑
              </button>
            </div>
          </>
        )}
      </div>
      {autoOpen && track && (
        <AutoCueDialog
          track={track}
          slotCount={slotCount}
          slotLabel={slotLabel}
          existing={existingHotcues}
          onClose={() => setAutoOpen(false)}
          onDone={autoHotcuesDone}
          onError={(msg) => onError?.(msg)}
        />
      )}
    </div>
  )
}
