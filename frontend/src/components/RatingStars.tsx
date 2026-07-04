import { useState } from 'react'

interface Props {
  value: number
  /** When provided, the stars become clickable to set the rating (0–5). */
  onChange?: (value: number) => void
}

// Compact 5-star rating. Read-only by default (shows a dash when unrated). When
// `onChange` is given it's interactive: always shows five stars — hollow (☆) up
// to the rating, solid gold (★) below it — with hover preview. Clicking a star
// sets that rating; clicking the current top star again clears it to 0.
export function RatingStars({ value, onChange }: Props) {
  const [hover, setHover] = useState(0)

  if (!onChange) {
    if (!value) return <span className="text-faint">—</span>
    return (
      <span className="whitespace-nowrap tracking-tight" title={`${value} / 5`}>
        {[1, 2, 3, 4, 5].map((i) => (
          <span key={i} className={i <= value ? 'text-gold' : 'text-ink-600'}>
            {i <= value ? '★' : '☆'}
          </span>
        ))}
      </span>
    )
  }

  const shown = hover || value
  return (
    <span className="whitespace-nowrap tracking-tight" title={`${value} / 5`}>
      {[1, 2, 3, 4, 5].map((i) => (
        <button
          key={i}
          onClick={(e) => {
            e.stopPropagation()
            onChange(i === value ? 0 : i)
          }}
          onMouseEnter={() => setHover(i)}
          onMouseLeave={() => setHover(0)}
          className={`transition-colors ${i <= shown ? 'text-gold' : 'text-ink-600 hover:text-gold/60'}`}
          title={i === value ? 'Clear rating' : `Set ${i} star${i > 1 ? 's' : ''}`}
        >
          {i <= shown ? '★' : '☆'}
        </button>
      ))}
    </span>
  )
}
