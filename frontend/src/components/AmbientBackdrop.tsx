/**
 * The drifting wash of colour every glass surface sits on. Its colours are CSS
 * custom properties set by `lib/ambient.ts` from the loaded track's cover art,
 * so this renders once and never re-renders on a track change.
 *
 * Fixed behind the whole app (and pointer-transparent). The drift is transform
 * only, so it stays on the compositor; reduced motion stops it.
 */
export function AmbientBackdrop() {
  return (
    <div aria-hidden className="ambient pointer-events-none fixed inset-0 -z-10 overflow-hidden bg-ink-950">
      <div className="ambient-wash" style={{ left: '-12%', top: '-30%', width: '58vw', height: '58vw', background: 'radial-gradient(circle, var(--amb-1) 0%, transparent 64%)', opacity: 0.75, animation: 'drift-a 28s ease-in-out infinite alternate' }} />
      <div className="ambient-wash" style={{ left: '42%', top: '-38%', width: '64vw', height: '54vw', background: 'radial-gradient(circle, var(--amb-2) 0%, transparent 62%)', opacity: 0.55, animation: 'drift-b 34s ease-in-out infinite alternate' }} />
      <div className="ambient-wash" style={{ left: '66%', top: '40%', width: '54vw', height: '54vw', background: 'radial-gradient(circle, var(--amb-3) 0%, transparent 62%)', opacity: 0.5, animation: 'drift-c 30s ease-in-out infinite alternate' }} />
      <div className="ambient-wash" style={{ left: '8%', top: '56%', width: '50vw', height: '44vw', background: 'radial-gradient(circle, var(--amb-4) 0%, transparent 64%)', opacity: 0.45, animation: 'drift-b 40s ease-in-out infinite alternate' }} />
      {/* Vignette: keeps the edges and the table's lower rows calm. */}
      <div className="absolute inset-0" style={{ background: 'radial-gradient(120% 90% at 50% 0%, rgb(5 6 10 / 0) 0%, rgb(5 6 10 / 0.55) 70%, rgb(5 6 10 / 0.85) 100%)' }} />
    </div>
  )
}
