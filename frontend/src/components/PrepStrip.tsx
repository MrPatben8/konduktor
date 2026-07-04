import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api, type Track, type TrackCues } from '../api'
import { analyzeWaveform, type WaveColumn } from '../lib/waveform'
import { ScratchEngine } from '../lib/scratchEngine'
import { PlaybackEngine } from '../lib/playbackEngine'
import { HotcueBar } from './HotcueBar'
import { LoopControls } from './LoopControls'
import { MainWaveform } from './MainWaveform'
import { OverviewWaveform } from './OverviewWaveform'

interface Props {
  /** The track currently loaded into the prep deck, if any. */
  track: Track | null
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
export function PrepStrip({ track, onError }: Props) {
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

  // Loop state (transient — persisted only when saved as a hotcue later).
  const [loopRegion, setLoopRegion] = useState<{ start: number; end: number } | null>(null)
  const [loopActive, setLoopActive] = useState(false)
  const [activeBeats, setActiveBeats] = useState<number | null>(null)
  const loopInRef = useRef<number | null>(null) // armed manual loop-in point

  const audioCtxRef = useRef<AudioContext | null>(null)
  const playbackRef = useRef<PlaybackEngine | null>(null)
  const scratchRef = useRef<ScratchEngine | null>(null)
  const wasPlayingRef = useRef(false)
  const scratchRafRef = useRef(0)
  const engagedRef = useRef(false) // scratch engine is dragging or coasting

  const trackId = track?.id ?? null

  const getCtx = (): AudioContext => {
    if (!audioCtxRef.current) {
      audioCtxRef.current = new (window.AudioContext ??
        (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)()
    }
    void audioCtxRef.current.resume()
    return audioCtxRef.current
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
    loopInRef.current = null
  }, [trackId])

  // Analyse once per track; the decoded buffer feeds both the playback and
  // scratch engines (no re-decode) and both waveform views share the columns.
  useEffect(() => {
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
        const sc = new ScratchEngine()
        sc.load(res.buffer)
        scratchRef.current = sc
        const ctx = getCtx()
        if (!playbackRef.current) {
          playbackRef.current = new PlaybackEngine()
          playbackRef.current.setOnEnded(() => setPlaying(false))
        }
        playbackRef.current.load(ctx, res.buffer)
        setDuration(res.buffer.duration)
        setReady(true)
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
        if (!cancelled) setCueData(d)
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
      sc.begin(getCtx(), current)
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

  const selectedCue = selectedSlot != null ? hotcueAt(selectedSlot) : null
  const showWaves = track && cols && waveStatus === 'ready'
  const activeLoop = loopActive && loopRegion ? loopRegion : null

  return (
    <div className="flex h-72 shrink-0 items-stretch gap-px border-b border-line bg-ink-950">
      {/* Controls */}
      <div className="flex w-72 shrink-0 flex-col justify-between bg-ink-900 px-4 py-3">
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

        <div className="flex items-center gap-3">
          <button
            onClick={toggle}
            disabled={!ready}
            className="flex h-9 w-9 items-center justify-center rounded-full border border-line bg-ink-850 text-text hover:border-accent disabled:opacity-40 disabled:hover:border-line"
            title={playing ? 'Pause' : 'Play'}
          >
            {playing ? '❚❚' : '▶'}
          </button>
          <div className="tabular-nums text-xs text-faint">
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
                  bpm={cueData?.bpm ?? null}
                  gridAnchor={cueData?.grid_anchor ?? null}
                  loop={activeLoop}
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
