// Display helpers for track data.

export function formatDuration(seconds: number | null): string {
  if (seconds == null) return '—'
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

export function formatBpm(bpm: number | null): string {
  if (bpm == null) return '—'
  return bpm.toFixed(1)
}

/**
 * Hue by wheel position, so harmonically adjacent keys sit adjacent in colour.
 *
 * Takes the parsed Camelot position rather than the key string: "10m" vs "Am"
 * vs "8A" is platform notation, and parsing it is the adapter's job. Doing it
 * here used to mean every non-Traktor key lost its colour.
 */
export function keyColor(wheel: number | null): string | undefined {
  if (wheel == null) return undefined
  // Light and saturated: it is drawn as text on a tint of itself, on glass.
  return `hsl(${((wheel % 12) / 12) * 360} 90% 76%)`
}
