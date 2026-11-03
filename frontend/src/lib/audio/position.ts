// The deck's playhead between the worklet's reports (see deckEngine.ts).

export interface Model {
  pos: number
  /** Context time `pos` was true at. */
  time: number
  /** Track seconds per second. */
  speed: number
  playing: boolean
  scratching: boolean
}

export interface Loop {
  start: number
  end: number
  on: boolean
}

/** The playhead now, from a model taken at `m.time`. Pure (tests). */
export function extrapolate(m: Model, now: number, loop: Loop, duration: number): number {
  if (!m.playing) return m.pos
  let p = m.pos + Math.max(0, now - m.time) * m.speed
  if (loop.on && loop.end > loop.start && p >= loop.end && m.pos < loop.end) {
    p = loop.start + ((p - loop.start) % (loop.end - loop.start))
  }
  // The end — or an engaged loop's, where it reaches past the track.
  return Math.min(p, loop.on ? Math.max(duration, loop.end) : duration)
}
