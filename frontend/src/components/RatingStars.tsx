interface Props {
  value: number
}

// Compact 5-star rating. Filled stars use the gold accent; empties are faint.
export function RatingStars({ value }: Props) {
  if (!value) return <span className="text-faint">—</span>
  return (
    <span className="whitespace-nowrap tracking-tight" title={`${value} / 5`}>
      {[1, 2, 3, 4, 5].map((i) => (
        <span
          key={i}
          className={i <= value ? 'text-gold' : 'text-ink-600'}
        >
          ★
        </span>
      ))}
    </span>
  )
}
