import { createPortal } from 'react-dom'
import { useEffect, useState, type ReactNode } from 'react'
import type { Track } from '../api'
import { formatBpm, formatDuration, keyColor } from '../lib/format'
import { useCaps } from '../lib/capabilities'
import { Icon } from '../lib/icons'
import { RatingStars } from './RatingStars'

interface Props {
  track: Track
  /** The cover's URL, or null where this track's library serves none (a
   *  device or a loose file) — the ambient swatch stands in, as in the deck. */
  artUrl: string | null
  onClose: () => void
  /** Offered only where the library will accept the edit; closes this first. */
  onEditTags?: () => void
}

/**
 * The deck cover's popup: the art large, beside everything the library knows
 * about the track. View-only apart from the one way out to Edit Tags. Rows with
 * no value are left out rather than shown as dashes, so a sparse track reads
 * as short rather than broken.
 */
export function TrackInfoDialog({ track, artUrl, onClose, onEditTags }: Props) {
  const caps = useCaps()
  const [artFailed, setArtFailed] = useState(false)
  const showArt = artUrl != null && !artFailed

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const grid =
    track.grid_marker_count === 0
      ? null
      : `${track.grid_marker_count > 1 ? `Flexible · ${track.grid_marker_count} markers` : 'Constant tempo'}${
          track.grid_locked ? ' · locked' : ''
        }`
  const details: [string, ReactNode][] = (
    [
      ['Album', track.album],
      ['Label', track.label],
      ['Genre', track.genre],
      ['Remixer', track.remixer],
      ['Producer', track.producer],
      ['Mix', track.mix],
      ['Released', track.release_date],
      ['Added', track.import_date],
      ['Last played', track.last_played],
      ['Plays', track.playcount ? String(track.playcount) : null],
      // The generic bitrate is bits per second.
      ['Bitrate', track.bitrate ? `${Math.round(track.bitrate / 1000)} kbps` : null],
      ['Grid', grid],
      ['Hotcues', track.hotcue_count ? String(track.hotcue_count) : null],
      ['Comment', track.comment],
      ['Comment 2', track.comment2],
    ] as [string, ReactNode][]
  ).filter(([, v]) => v != null && v !== '')

  return createPortal(
    <div
      aria-modal="true"
      role="dialog"
      aria-label={track.title ?? 'Track info'}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 backdrop-blur-[3px]"
      onClick={onClose}
    >
      <div
        className="track-info-pop glass-overlay flex max-h-[90vh] w-full max-w-[860px] flex-col overflow-hidden sm:flex-row"
        onClick={(e) => e.stopPropagation()}
      >
        {/* The sleeve, bled into the card behind everything: a soft wash of the
            art's own colours. `filter` on this leaf is safe — nothing can open
            inside it. */}
        {showArt && (
          <img
            aria-hidden
            src={artUrl}
            alt=""
            // Between the tint (::after, z -1, earlier) and the rim (::before).
            style={{ zIndex: -1 }}
            className="pointer-events-none absolute inset-0 h-full w-full scale-125 object-cover opacity-30 blur-3xl saturate-150"
          />
        )}

        <div className="relative flex shrink-0 flex-col items-center gap-4 p-6 sm:pr-3">
          <div
            className="relative aspect-square w-[min(340px,70vw)] overflow-hidden rounded-2xl shadow-[inset_0_1px_0_rgb(255_255_255/0.35),0_24px_60px_-12px_var(--amb-2)]"
            style={{ background: 'linear-gradient(135deg, var(--amb-1), var(--amb-2) 55%, var(--amb-3))' }}
          >
            {showArt ? (
              <img
                src={artUrl}
                alt={`Cover of ${track.title ?? 'this track'}`}
                className="absolute inset-0 h-full w-full object-cover"
                onError={() => setArtFailed(true)}
              />
            ) : (
              <div className="absolute inset-0 flex items-center justify-center text-white/40">
                <Icon name="music" size={72} strokeWidth={1.2} />
              </div>
            )}
          </div>
          {track.rating > 0 && <RatingStars value={track.rating} max={caps.tracks.rating_max} />}
        </div>

        <div className="relative flex min-h-0 min-w-0 flex-1 flex-col">
          <div className="min-h-0 flex-1 overflow-y-auto p-6 sm:pl-3">
            <div className="flex items-start gap-3">
              <div className="min-w-0 flex-1">
                <h2 className="text-2xl font-semibold leading-tight tracking-tight text-text select-text">
                  {track.title ?? 'Untitled'}
                </h2>
                <div className="mt-1 text-[15px] text-muted select-text">{track.artist ?? 'Unknown artist'}</div>
              </div>
              <button
                onClick={onClose}
                aria-label="Close"
                className="btn-glass -mr-2 -mt-2 flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-muted hover:text-text"
              >
                <Icon name="close" size={14} />
              </button>
            </div>

            <div className="mt-5 flex flex-wrap gap-2">
              <Stat label="KEY" value={track.key || '—'} color={keyColor(track.key_wheel)} />
              <Stat label="BPM" value={formatBpm(track.bpm)} />
              <Stat label="LENGTH" value={formatDuration(track.length)} />
              {track.media_kind !== 'audio' && <Stat label="TYPE" value={track.media_kind.toUpperCase()} />}
            </div>

            {details.length > 0 && (
              <dl className="mt-5 grid grid-cols-[auto_1fr] gap-x-5 gap-y-1.5 border-t border-line pt-4 text-[13px]">
                {details.map(([label, value]) => (
                  <div key={label} className="contents">
                    <dt className="text-faint">{label}</dt>
                    <dd className="min-w-0 whitespace-pre-wrap break-words text-text select-text">{value}</dd>
                  </div>
                ))}
              </dl>
            )}

            {track.filepath && (
              <div
                className="well mt-5 rounded-xl px-3 py-2 font-mono text-[11px] leading-relaxed text-muted break-all select-text"
                title={track.filepath}
              >
                {track.filepath}
              </div>
            )}
          </div>

          {onEditTags && (
            <div className="flex justify-end px-6 pb-6 sm:pl-3">
              <button
                onClick={onEditTags}
                className="btn-glass flex h-9 items-center gap-2 rounded-full px-4 text-[13px] font-medium"
              >
                <Icon name="pencil" size={15} />
                Edit Tags…
              </button>
            </div>
          )}
        </div>
      </div>
    </div>,
    document.body,
  )
}

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="well flex flex-col gap-px rounded-xl px-4 py-2">
      <span className="text-[10px] tracking-[0.08em] text-faint">{label}</span>
      <span className="font-mono text-[17px] font-semibold" style={{ color }}>
        {value}
      </span>
    </div>
  )
}
