import { useState, type ReactNode } from 'react'
import { api } from '../../api'
import { keyColor } from '../../lib/format'
import { Icon } from '../../lib/icons'
import { readOnlyNotice, readOnlyShort } from '../../lib/platformCopy'
import { useAppSetting } from '../AppSettings'
import { BpmReadout } from '../GridControls'
import type { Deck } from './useDeck'

/** mm:ss.f — tenths by default, as a CDJ does; `places` is the
 *  `timeReadoutDecimals` setting (0 drops the fraction). Truncates, never
 *  rounds, so the readout never shows a second before it has been reached. */
function fmtTime(secs: number, places: number): string {
  if (!isFinite(secs) || secs < 0) secs = 0
  const scale = 10 ** places
  // The epsilon keeps e.g. 0.3 s from reading 0.299 at 3 places.
  const units = Math.floor(secs * scale + 1e-6)
  const whole = Math.floor(units / scale)
  const m = Math.floor(whole / 60)
  const s = whole % 60
  const frac = places > 0 ? `.${(units % scale).toString().padStart(places, '0')}` : ''
  return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}${frac}`
}

/** The deck's header: the track, its readouts, the analysis actions — and
 *  `actions`, anything the layout adds after them. `compact` (half-width, in
 *  performance mode) tightens the readouts and drops the action buttons'
 *  words; `badge` leads the row (the deck's letter). */
export function DeckHeader({
  deck,
  actions,
  compact = false,
  badge,
}: {
  deck: Deck
  actions?: ReactNode
  compact?: boolean
  badge?: ReactNode
}) {
  const {
    caps,
    track,
    origin,
    subtitle,
    setInfoOpen,
    current,
    duration,
    activeMarker,
    canEditGrid,
    commitBpm,
    gridAnchorRef,
    playheadNow,
    bpmEntryRequest,
    snap,
    setSnap,
    runAnalyzeGrid,
    gridBusy,
    openAutoHotcues,
    canAutoCue,
  } = deck
  const timeDecimals = useAppSetting('timeReadoutDecimals')
  const cell = `flex flex-col justify-center gap-px ${compact ? 'px-2.5' : 'px-4'}`
  const action = `btn-glass flex h-9 shrink-0 items-center justify-center gap-2 rounded-full text-[13px] font-medium disabled:opacity-40 ${
    compact ? 'w-9' : 'px-3.5'
  }`
  return (
    <div className={`flex h-12 min-w-0 shrink-0 items-center ${compact ? 'gap-2' : 'gap-3'}`}>
      {badge}
      <CoverThumb
        trackId={track && origin === 'collection' ? track.id : null}
        onOpen={track ? () => setInfoOpen(true) : undefined}
      />
      <div className="min-w-0 flex-1">
        {track ? (
          <>
            <div className="flex min-w-0 items-center gap-2">
              <span
                className={`truncate font-semibold tracking-tight text-text ${compact ? 'text-[15px]' : 'text-[17px]'}`}
                title={track.title ?? ''}
              >
                {track.title ?? 'Untitled'}
              </span>
              {/* The deck stays usable for listening on a read-only library,
                  so say which it is rather than leaving the edit controls
                  looking live. The controls report the reason when pressed. */}
              {readOnlyShort(caps) && (
                <span
                  className="shrink-0 rounded-md bg-ink-800 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-muted"
                  title={readOnlyNotice(caps) ?? ''}
                >
                  {readOnlyShort(caps)}
                </span>
              )}
            </div>
            <div className={`truncate text-muted ${compact ? 'text-[12px]' : 'text-[13px]'}`} title={subtitle}>
              {subtitle}
            </div>
          </>
        ) : (
          <div className="text-xs uppercase tracking-wider text-faint">No track loaded</div>
        )}
      </div>

      <div className="well flex h-11 shrink-0 items-stretch rounded-xl">
        <div className={cell}>
          <span className="text-[10px] tracking-[0.08em] text-faint">ELAPSED</span>
          <span className={`font-mono font-medium ${compact ? 'text-[15px]' : 'text-[17px]'}`}>{fmtTime(current, timeDecimals)}</span>
        </div>
        <span aria-hidden className="my-2 w-px bg-line" />
        <div className={cell}>
          <span className="text-[10px] tracking-[0.08em] text-faint">REMAIN</span>
          <span className={`font-mono font-medium ${compact ? 'text-[15px]' : 'text-[17px]'}`}>−{fmtTime(Math.max(0, duration - current), timeDecimals)}</span>
        </div>
        <span aria-hidden className="my-2 w-px bg-line" />
        <div className={cell}>
          <span className="text-[10px] tracking-[0.08em] text-faint">KEY</span>
          <span
            className={`font-mono font-semibold ${compact ? 'text-[15px]' : 'text-[17px]'}`}
            style={{ color: keyColor(track?.key_wheel ?? null) }}
          >
            {track?.key || '—'}
          </span>
        </div>
        <span aria-hidden className="my-2 w-px bg-line" />
        <BpmReadout
          bpm={activeMarker?.bpm ?? null}
          editable={canEditGrid}
          onSetBpm={(bpm) => commitBpm(bpm, gridAnchorRef.current ?? playheadNow())}
          onClose={() => (gridAnchorRef.current = null)}
          openRequest={bpmEntryRequest}
        />
      </div>

      <button
        onClick={() => setSnap((v) => !v)}
        aria-pressed={snap}
        title="Snap cues and loops to the nearest beat"
        className={`h-9 shrink-0 rounded-full px-3.5 text-xs font-semibold tracking-[0.08em] ${
          snap ? 'is-selected text-text' : 'btn-glass text-muted'
        }`}
      >
        SNAP
      </button>
      <button
        onClick={runAnalyzeGrid}
        disabled={!track || gridBusy}
        title={canEditGrid
          ? 'Detect BPM and first beat, then set the grid + hotcue 1 (fix the octave with ÷2 / ×2); measure loudness'
          : 'Measure loudness (this track’s grid cannot be edited here)'}
        aria-label="Analyze"
        className={action}
      >
        <Icon name="wave" size={16} />
        {!compact && (gridBusy ? 'Analyzing…' : 'Analyze')}
      </button>
      <button
        onClick={openAutoHotcues}
        disabled={!track || !canAutoCue}
        title={
          canAutoCue
            ? "Place hotcues on the track's drops, breakdowns and other sections"
            : 'Set a beatgrid first'
        }
        aria-label="Auto hotcues"
        className={action}
      >
        <Icon name="sparkle" size={16} />
        {!compact && 'Auto hotcues'}
      </button>
      {actions}
    </div>
  )
}

/**
 * The loaded track's sleeve. Until it loads — and for a track with no art — a
 * swatch of the ambient colours stands in, so the header never has a hole in it.
 * With a track loaded it is a button that opens the track-info popup.
 */
function CoverThumb({ trackId, onOpen }: { trackId: string | null; onOpen?: () => void }) {
  const [failed, setFailed] = useState<string | null>(null)
  const showArt = trackId != null && failed !== trackId
  return (
    <button
      type="button"
      onClick={onOpen}
      disabled={!onOpen}
      title={onOpen ? 'Track info' : undefined}
      aria-label="Track info"
      className="relative h-11 w-11 shrink-0 overflow-hidden rounded-xl shadow-[inset_0_1px_0_rgb(255_255_255/0.35),0_8px_24px_-6px_var(--amb-2)] transition-transform enabled:cursor-zoom-in enabled:hover:scale-105 enabled:active:scale-95 disabled:cursor-default"
      style={{ background: 'linear-gradient(135deg, var(--amb-1), var(--amb-2) 55%, var(--amb-3))' }}
    >
      {showArt && (
        <img
          key={trackId}
          src={api.artUrl(trackId)}
          alt=""
          className="absolute inset-0 h-full w-full object-cover"
          onError={() => setFailed(trackId)}
        />
      )}
    </button>
  )
}
