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

/** A Mastering column's value: one decimal and its unit, the sign shown where
 *  it carries meaning (true peak above 0 dBTP clips): `-8.2 LUFS`, `+0.3 dBTP`,
 *  `5.1 LU`. Rounds to the decimal (never to "-0.0"). */
export function formatLoudness(value: number | undefined, unit: string, signed = false): string {
  if (value == null) return '—'
  let fixed = value.toFixed(1)
  if (fixed === '-0.0') fixed = '0.0'
  if (signed && value > 0 && fixed !== '0.0') fixed = `+${fixed}`
  return `${fixed} ${unit}`
}
