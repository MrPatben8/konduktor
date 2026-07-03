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

// Traktor stores keys like "10m" / "8d" (Open Key notation) or classic "8A".
// Give each of the 12 pitch classes a stable hue so harmonic groups read at a
// glance — adjacent/compatible keys land near each other on the color wheel.
export function keyColor(key: string | null): string | undefined {
  if (!key) return undefined
  const m = key.match(/(\d+)/)
  if (!m) return undefined
  const n = parseInt(m[1], 10) % 12
  const hue = (n / 12) * 360
  return `hsl(${hue} 65% 62%)`
}
