import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type Track, type TrackCues } from '../api'
import { analyzeWaveform, type WaveColumn } from '../lib/waveform'
import { ScratchEngine } from '../lib/scratchEngine'
import { PlaybackEngine } from '../lib/playbackEngine'
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
}

function fmt(secs: number): string {
  if (!isFinite(secs) || secs < 0) return '0:00'
  const m = Math.floor(secs / 60)
  const s = Math.floor(secs % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

const CUE_TYPES: { value: number; label: string }[] = [
  { value: 0, label: 'Cue' },
  { value: 1, label: 'Fade-In' },
  { value: 2, label: 'Fade-Out' },
  { value: 3, label: 'Load' },
]
const LOOP_TYPE = 5
const LOOP_SIZES = [1 / 32, 1 / 16, 1 / 8, 1 / 4, 1 / 2, 1, 2, 4, 8, 16, 32]

/**
 * The DJ-style "prep strip" across the top of the window: transport controls on
 * the left, waveforms on the right (a scrolling zoomable main view above a
 * whole-track overview), plus loop + hotcue controls. Playback runs through the
 * Web Audio PlaybackEngine (seamless loops); the same decoded buffer feeds the
 * scratch engine and both waveform views.
 */
export function PrepStrip({ track, playRequest = 0, onError }: Props) {
  const qc = useQueryClient()
  const [playing, setPlaying] = useState(false)
  const [current, setCurrent] = useState(0)
  const [duration, setDuration] = useState(0)
  const [ready, setReady] = useState(false)
  const [cols, setCols] = useState<WaveColumn[] | null>(null)
  const [waveStatus, setWaveStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [cueData, setCueData] = useState<TrackCues | null>(null)
  const [snap, setSnap] = useState(true)
  const [selectedSlot, setSelectedSlot] = useState<number | null>(null)
  const [cuePoint, setCuePoint] = useState(0) // floating "CUE" point (frontend-only)
  const [secPerView, setSecPerView] = useState(DEFAULT_SEC) // main-waveform zoom (persisted)

  // Loop state (transient — persisted only when saved as a hotcue later).
  const [loopRegion, setLoopRegion] = useState<{ start: number; end: number } | null>(null)
  const [loopActive, setLoopActive] = useState(false)
  const [activeBeats, setActiveBeats] = useState<number | null>(null)
  const loopInRef = useRef<number | null>(null) // armed manual loop-in point
  const originalGridRef = useRef<{ bpm: number | null; anchor: number | null } | null>(null)

  const audioCtxRef = useRef<AudioContext | null>(null) // playback
  const scratchCtxRef = useRef<AudioContext | null>(null) // scratch (kept separate!)
  const playbackRef = useRef<PlaybackEngine | null>(null)
  const scratchRef = useRef<ScratchEngine | null>(null)
  const wasPlayingRef = useRef(false)
  const scratchRafRef = useRef(0)
  const engagedRef = useRef(false) // scratch engine is dragging or coasting
  const pendingPlayRef = useRef(false) // a play was requested; start once ready
  const loadedIdRef = useRef<string | null>(null) // trackId whose buffer is loaded

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
    analyzeWaveform(api.audioUrl(trackId))
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
  }, [trackId])

  // Fetch beatgrid + cue markers for the loaded track.
  useEffect(() => {
    if (!trackId) {
      setCueData(null)
      return
    }
    let cancelled = false
    setCueData(null)
    api
      .trackCues(trackId)
      .then((d) => {
        if (cancelled) return
        setCueData(d)
        // Remember the loaded grid values for "Reset".
        originalGridRef.current = { bpm: d.bpm, anchor: d.grid_anchor }
      })
      .catch(() => {
        if (!cancelled) setCueData(null)
      })
    return () => {
      cancelled = true
    }
  }, [trackId])

  const toggle = () => {
    const eng = playbackRef.current
    if (!eng || !eng.ready) return
    getCtx() // ensure the context resumes on this user gesture
    if (eng.playing) {
      eng.pause()
      setPlaying(false)
    } else {
      eng.play()
      setPlaying(true)
    }
    setCurrent(eng.getPosition())
  }

  // CUE (Traktor/CDJ-style): while playing, jump back to the cue point and
  // pause; while paused and away from the cue, drop the cue at the playhead;
  // while paused and already at the cue, play from it.
  const handleCue = () => {
    const eng = playbackRef.current
    if (!eng || !eng.ready) return
    getCtx()
    if (eng.playing) {
      eng.pause()
      eng.seek(cuePoint)
      setPlaying(false)
      setCurrent(cuePoint)
    } else if (Math.abs(current - cuePoint) > 0.03) {
      setCuePoint(current)
    } else {
      eng.play()
      setPlaying(true)
    }
  }

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
  const snapTime = (t: number) => {
    const bpm = cueData?.bpm ?? null
    const anchor = cueData?.grid_anchor ?? null
    if (!snap || !bpm || bpm <= 0 || anchor == null) return Math.max(0, t)
    const beat = 60 / bpm
    return Math.max(0, anchor + Math.round((t - anchor) / beat) * beat)
  }

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
    const bpm = cueData?.bpm ?? null
    if (!eng || !eng.ready || !bpm || bpm <= 0) return
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
    engagLoop(start, start + beats * (60 / bpm), beats)
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

  // ---- hotcues ----------------------------------------------------------
  const hotcueAt = (slot: number) => cueData?.cues.find((c) => c.hotcue === slot) ?? null

  const applyCueEdit = (fresh: TrackCues) => {
    setCueData(fresh)
    qc.invalidateQueries({ queryKey: ['state'] })
    qc.invalidateQueries({ queryKey: ['tracks'] })
    qc.invalidateQueries({ queryKey: ['playlist'] })
  }

  // Map a loop length (seconds) back to a preset beat count for the size
  // highlight, snapping to the nearest preset when it's close (float tolerance).
  const beatsForLength = (length: number): number | null => {
    const bpm = cueData?.bpm ?? null
    if (!bpm || bpm <= 0) return null
    const beats = length / (60 / bpm)
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

  const onSlotClick = async (slot: number) => {
    if (!track) return
    const cue = hotcueAt(slot)
    if (cue) {
      // A loop hotcue jumps to its start AND re-engages a loop of its length.
      if (cue.type === LOOP_TYPE && cue.length > 0) {
        seek(cue.start)
        engagLoop(cue.start, cue.start + cue.length, beatsForLength(cue.length))
      } else {
        seek(cue.start)
      }
      setSelectedSlot(slot)
      return
    }
    try {
      // Setting a hotcue while a loop is active stores it as a loop hotcue.
      if (loopActive && loopRegion) {
        const len = loopRegion.end - loopRegion.start
        applyCueEdit(await api.createHotcue(track.id, slot, loopRegion.start, LOOP_TYPE, len))
      } else {
        const t = snapTime(playbackRef.current?.getPosition() ?? current)
        applyCueEdit(await api.createHotcue(track.id, slot, t, 0))
      }
      setSelectedSlot(slot)
    } catch (e) {
      onError?.((e as Error).message)
    }
  }

  const changeSelectedType = async (type: number) => {
    if (!track || selectedSlot == null) return
    try {
      applyCueEdit(await api.setHotcueType(track.id, selectedSlot, type))
    } catch (e) {
      onError?.((e as Error).message)
    }
  }

  const deleteSelected = async () => {
    if (!track || selectedSlot == null) return
    try {
      applyCueEdit(await api.deleteHotcue(track.id, selectedSlot))
      setSelectedSlot(null)
    } catch (e) {
      onError?.((e as Error).message)
    }
  }

  const deleteHotcueSlot = async (slot: number) => {
    if (!track || !hotcueAt(slot)) return // nothing to remove in an empty slot
    try {
      applyCueEdit(await api.deleteHotcue(track.id, slot))
      if (selectedSlot === slot) setSelectedSlot(null)
    } catch (e) {
      onError?.((e as Error).message)
    }
  }

  // ---- keyboard shortcuts ----------------------------------------------
  // Space → play/pause; 1–8 → the matching hotcue slot; Shift+1–8 → delete it.
  // Digits are read from e.code (layout-/Shift-independent) and a ref holds the
  // latest handlers so the listener attaches once and never goes stale.
  const shortcutsRef = useRef({ toggle, onSlotClick, deleteHotcueSlot })
  shortcutsRef.current = { toggle, onSlotClick, deleteHotcueSlot }
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Ignore while typing in a field or with a non-Shift modifier held.
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
      if (e.metaKey || e.ctrlKey || e.altKey) return
      if (e.code === 'Space') {
        e.preventDefault()
        shortcutsRef.current.toggle()
        return
      }
      const digit = e.code.match(/^(?:Digit|Numpad)([1-8])$/)
      if (digit) {
        e.preventDefault()
        const slot = Number(digit[1]) - 1
        if (e.shiftKey) void shortcutsRef.current.deleteHotcueSlot(slot)
        else void shortcutsRef.current.onSlotClick(slot)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // ---- beatgrid ---------------------------------------------------------
  const gridBpm = cueData?.bpm ?? null
  const gridAnchor = cueData?.grid_anchor ?? null

  const editGrid = async (patch: { bpm?: number; anchor?: number }) => {
    if (!track) return
    try {
      applyCueEdit(await api.setGrid(track.id, patch))
    } catch (e) {
      onError?.((e as Error).message)
    }
  }
  const setBpm = (bpm: number) => editGrid({ bpm })
  const nudgeBpm = (delta: number) =>
    gridBpm && editGrid({ bpm: Math.round((gridBpm + delta) * 1000) / 1000 })
  const halveBpm = () => gridBpm && editGrid({ bpm: gridBpm / 2 })
  const doubleBpm = () => gridBpm && editGrid({ bpm: gridBpm * 2 })
  const nudgeGrid = (deltaMs: number) => {
    if (gridAnchor == null) return
    editGrid({ anchor: Math.max(0, gridAnchor + deltaMs / 1000) })
  }
  const setGridHere = () => editGrid({ anchor: playbackRef.current?.getPosition() ?? current })
  const resetGrid = () => {
    const o = originalGridRef.current
    if (!o) return
    editGrid({ bpm: o.bpm ?? undefined, anchor: o.anchor ?? undefined })
  }
  const toggleLock = async () => {
    if (!track) return
    try {
      applyCueEdit(await api.setLock(track.id, !cueData?.locked))
    } catch (e) {
      onError?.((e as Error).message)
    }
  }
  const deleteGrid = async () => {
    if (!track) return
    try {
      applyCueEdit(await api.deleteGrid(track.id))
    } catch (e) {
      onError?.((e as Error).message)
    }
  }

  const selectedCue = selectedSlot != null ? hotcueAt(selectedSlot) : null
  const showWaves = track && cols && waveStatus === 'ready'
  const activeLoop = loopActive && loopRegion ? loopRegion : null

  return (
    <div className="flex h-[21rem] shrink-0 items-stretch gap-px border-b border-line bg-ink-950">
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
            </>
          ) : (
            <div className="text-xs uppercase tracking-wider text-faint">No track loaded</div>
          )}
        </div>

        {track && (
          <GridControls
            bpm={gridBpm}
            locked={cueData?.locked ?? false}
            hasGrid={gridAnchor != null}
            onSetBpm={setBpm}
            onNudgeBpm={nudgeBpm}
            onHalve={halveBpm}
            onDouble={doubleBpm}
            onNudge={nudgeGrid}
            onSetHere={setGridHere}
            onReset={resetGrid}
            onToggleLock={toggleLock}
            onDeleteGrid={deleteGrid}
          />
        )}

        <div className="mt-auto flex items-center gap-2">
          <button
            onClick={handleCue}
            disabled={!ready}
            className="flex h-11 items-center justify-center rounded-xl border border-line bg-ink-850 px-4 text-sm font-bold tracking-wide text-gold transition-colors hover:border-gold disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:border-line"
            title="Cue — set the cue point (paused) or jump back to it (playing)"
          >
            CUE
          </button>
          <button
            onClick={toggle}
            disabled={!ready}
            className={
              'flex h-11 w-11 items-center justify-center rounded-xl transition-all active:scale-95 disabled:cursor-not-allowed disabled:opacity-40 ' +
              (playing
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
                  bpm={cueData?.bpm ?? null}
                  gridAnchor={cueData?.grid_anchor ?? null}
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
              bpm={cueData?.bpm ?? null}
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
                selectedSlot={selectedSlot}
                onSlotClick={onSlotClick}
              />
              <div className="flex w-28 items-center justify-center bg-ink-900 px-1">
                {selectedCue && selectedCue.type === LOOP_TYPE ? (
                  <span className="text-sm font-semibold text-mint">Loop</span>
                ) : (
                  <select
                    value={selectedCue ? selectedCue.type : ''}
                    disabled={!selectedCue}
                    onChange={(e) => changeSelectedType(Number(e.target.value))}
                    className="w-full bg-transparent text-center text-sm font-semibold text-text outline-none disabled:opacity-40"
                  >
                    {!selectedCue && <option value="">—</option>}
                    {CUE_TYPES.map((t) => (
                      <option key={t.value} value={t.value}>
                        {t.label}
                      </option>
                    ))}
                  </select>
                )}
              </div>
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
    </div>
  )
}
