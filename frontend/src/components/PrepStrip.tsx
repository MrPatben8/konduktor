import { useEffect, useRef, useState } from 'react'
import { api, type Track } from '../api'
import { analyzeWaveform, type WaveColumn } from '../lib/waveform'
import { ScratchEngine } from '../lib/scratchEngine'
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

/**
 * The DJ-style "prep strip" across the top of the window: transport controls on
 * the left, waveforms on the right (a scrolling zoomable main view above a
 * whole-track overview). Playback is driven by a single hidden <audio> element
 * streamed from the backend (range-enabled, so seeking works). The frequency-
 * coloured waveform is analysed once here and shared by both views.
 */
export function PrepStrip({ track, onError }: Props) {
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const [playing, setPlaying] = useState(false)
  const [current, setCurrent] = useState(0)
  const [duration, setDuration] = useState(0)
  const [loadError, setLoadError] = useState(false)
  const [cols, setCols] = useState<WaveColumn[] | null>(null)
  const [waveStatus, setWaveStatus] = useState<'loading' | 'ready' | 'error'>('loading')

  // Scratch engine (Web Audio) — holds the decoded buffer for the loaded track.
  const audioCtxRef = useRef<AudioContext | null>(null)
  const engineRef = useRef<ScratchEngine | null>(null)
  const wasPlayingRef = useRef(false)
  const scratchRafRef = useRef(0)
  const engagedRef = useRef(false) // engine is dragging or coasting

  const trackId = track?.id ?? null

  // When the loaded track changes, reset transport state. The <audio> src is
  // bound declaratively below, so changing trackId swaps the source.
  useEffect(() => {
    setPlaying(false)
    setCurrent(0)
    setDuration(0)
    setLoadError(false)
  }, [trackId])

  // Analyse the waveform once per track; both waveform views share the result,
  // and the decoded buffer is handed to the scratch engine (no second decode).
  useEffect(() => {
    engineRef.current = null
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
        const eng = new ScratchEngine()
        eng.load(res.buffer)
        engineRef.current = eng
      })
      .catch(() => {
        if (!cancelled) setWaveStatus('error')
      })
    return () => {
      cancelled = true
    }
  }, [trackId])

  const toggle = () => {
    const el = audioRef.current
    if (!el || !track) return
    if (el.paused) {
      el.play().catch(() => {
        setLoadError(true)
        setPlaying(false)
      })
    } else {
      el.pause()
    }
  }

  // Smoothly advance the playhead/time while playing (timeupdate is only ~4Hz).
  useEffect(() => {
    if (!playing) return
    let raf = 0
    const tick = () => {
      const el = audioRef.current
      if (el) setCurrent(el.currentTime)
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [playing])

  const seek = (t: number) => {
    const el = audioRef.current
    if (!el) return
    el.currentTime = t
    setCurrent(t)
  }

  // ---- scratch (Web Audio) ----------------------------------------------
  const getCtx = (): AudioContext => {
    if (!audioCtxRef.current) {
      audioCtxRef.current = new (window.AudioContext ??
        (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)()
    }
    void audioCtxRef.current.resume()
    return audioCtxRef.current
  }

  // Once the platter has coasted to a stop (or up to normal speed), hand the
  // position back to the <audio> element and resume if it had been playing.
  const finalizeScratch = () => {
    cancelAnimationFrame(scratchRafRef.current)
    engagedRef.current = false
    const eng = engineRef.current
    if (!eng) return
    const finalSec = eng.end()
    const el = audioRef.current
    if (el) el.currentTime = finalSec
    setCurrent(finalSec)
    if (wasPlayingRef.current && el) void el.play().catch(() => {})
  }

  // Follow the engine's actual position each frame; finalize when it settles.
  const runScratchRaf = () => {
    cancelAnimationFrame(scratchRafRef.current)
    const eng = engineRef.current!
    const tick = () => {
      setCurrent(eng.getPositionSec())
      if (eng.finished) {
        finalizeScratch()
        return
      }
      scratchRafRef.current = requestAnimationFrame(tick)
    }
    scratchRafRef.current = requestAnimationFrame(tick)
  }

  const onScratchStart = () => {
    const eng = engineRef.current
    // Re-grab while still coasting: keep the current position, resume dragging.
    if (engagedRef.current && eng) {
      eng.regrab()
      return
    }
    const el = audioRef.current
    wasPlayingRef.current = !!el && !el.paused
    if (el && !el.paused) el.pause()
    if (eng && eng.loaded) {
      eng.begin(getCtx(), current)
      engagedRef.current = true
      runScratchRaf()
    }
  }

  const onScratchMove = (t: number) => {
    if (engagedRef.current) engineRef.current!.setTargetSec(t)
    else setCurrent(t) // no decoded buffer yet → silent visual scrub
  }

  const onScratchEnd = (t: number) => {
    if (engagedRef.current) {
      // Coast; runScratchRaf finalizes once the platter settles.
      engineRef.current!.release(wasPlayingRef.current)
    } else {
      const el = audioRef.current
      if (el) el.currentTime = t
      setCurrent(t)
      if (wasPlayingRef.current && el) void el.play().catch(() => {})
    }
  }

  const showWaves = track && !loadError && cols && waveStatus === 'ready'

  return (
    <div className="flex h-52 shrink-0 items-stretch gap-px border-b border-line bg-ink-950">
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
            disabled={!track || loadError}
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

      {/* Waveforms: scrolling main view above a whole-track overview. */}
      <div className="relative flex min-w-0 flex-1 flex-col bg-ink-900">
        {!track ? (
          <div className="flex flex-1 items-center justify-center text-xs text-faint">
            Load a track to prep it
          </div>
        ) : loadError ? (
          <div className="flex flex-1 items-center justify-center text-xs text-pink">
            Could not load audio — file may be missing or unsupported by the browser.
          </div>
        ) : (
          <>
            <div className="relative min-h-0 flex-1">
              {showWaves ? (
                <MainWaveform
                  cols={cols}
                  currentTime={current}
                  duration={duration}
                  onSeek={seek}
                  onScratchStart={onScratchStart}
                  onScratchMove={onScratchMove}
                  onScratchEnd={onScratchEnd}
                />
              ) : (
                <div className="flex h-full items-center justify-center text-xs text-faint">
                  {waveStatus === 'error' ? 'Waveform unavailable' : 'Analysing waveform…'}
                </div>
              )}
            </div>
            <div className="h-10 shrink-0 border-t border-line">
              {showWaves && (
                <OverviewWaveform
                  cols={cols}
                  currentTime={current}
                  duration={duration}
                  onSeek={seek}
                />
              )}
            </div>
          </>
        )}
      </div>

      {track && (
        <audio
          ref={audioRef}
          src={api.audioUrl(track.id)}
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onEnded={() => setPlaying(false)}
          onTimeUpdate={(e) => setCurrent(e.currentTarget.currentTime)}
          onLoadedMetadata={(e) => setDuration(e.currentTarget.duration)}
          onError={() => {
            setLoadError(true)
            onError?.('Could not load audio for this track')
          }}
        />
      )}
    </div>
  )
}
