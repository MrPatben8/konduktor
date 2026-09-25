import { useState } from 'react'

interface Props {
  value: number
  /** Scale length, from capabilities.tracks.rating_max. */
  max?: number
  /** When provided, the stars become clickable to set the rating. */
  onChange?: (value: number) => void
}

function Star({ className }: { className?: string }) {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 24 24"
      aria-hidden
      className={className}
      fill="currentColor"
    >
      <path d="M12 3.5l2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8-4.3-4.1 5.9-.9z" />
    </svg>
  )
}

// Compact star rating. Read-only by default (shows a dash when unrated). When
// `onChange` is given it's interactive: hollow (☆) above the rating, solid gold
// (★) up to it, with hover preview. Clicking a star sets that rating; clicking
// the current top star again clears it to 0.
export function RatingStars({ value, max = 5, onChange }: Props) {
  const [hover, setHover] = useState(0)
  const stars = Array.from({ length: max }, (_, i) => i + 1)

  if (!onChange) {
    if (!value) return <span className="text-faint">—</span>
    return (
      <span className="flex items-center gap-0.5" title={`${value} / ${max}`}>
        {stars.map((i) => (
          <Star key={i} className={i <= value ? 'text-gold' : 'text-white/15'} />
        ))}
      </span>
    )
  }

  const shown = hover || value
  return (
    <span className="flex items-center gap-0.5" title={`${value} / ${max}`}>
      {stars.map((i) => (
        <button
          key={i}
          onClick={(e) => {
            e.stopPropagation()
            onChange(i === value ? 0 : i)
          }}
          onMouseEnter={() => setHover(i)}
          onMouseLeave={() => setHover(0)}
          className={`transition-colors ${i <= shown ? 'text-gold' : 'text-white/15 hover:text-gold/60'}`}
          title={i === value ? 'Clear rating' : `Set ${i} star${i > 1 ? 's' : ''}`}
        >
          <Star />
        </button>
      ))}
    </span>
  )
}
