/**
 * The beatgrid as an ordered list of tempo markers.
 *
 * Traktor stores a beatgrid as a list of markers, each carrying its own BPM;
 * marker `i`'s tempo governs from its position until marker `i+1`. A
 * constant-tempo track is simply a list of length one, so this model covers
 * every track rather than being a special case for flexible grids.
 *
 * The consequence that matters: **beat duration is position-dependent**, so
 * `60 / bpm` is wrong the moment a track has more than one marker. Everything
 * that needs beats — drawing, cue snapping, beat jump, beat loops, loop-size
 * labelling — goes through this module instead.
 *
 * Two distinct notions of "beat", deliberately kept apart:
 *
 *  - **Beat index** is global and continuous across markers. Beat jump needs a
 *    single monotonic beat space, or "forward 4 beats" across a tempo change is
 *    undefined.
 *  - **Bar phase** restarts at each marker: a marker is beat 0 *and* a downbeat
 *    of its own segment. With continuous bar numbering, nudging an early
 *    marker's BPM would renumber every downbeat after it, so the bar lines
 *    would slide around as you turn the fine nudge — which would make the very
 *    workflow this exists for unusable. It also matches Traktor, which pairs
 *    each marker with its own beat-1 cue, and the format cannot express "this
 *    marker is beat 3 of a bar".
 *
 * With a single marker every operation here reduces algebraically to the
 * previous single-BPM arithmetic, which is the acceptance property for the
 * whole model (see beatgrid.test.ts).
 */

/** One beatgrid marker: this tempo governs from `start` until the next. */
export interface GridMarker {
  start: number // seconds
  bpm: number
}

export const BEATS_PER_BAR = 4

/** Positions closer than this are treated as the same instant. Marker times
 *  arrive as `START / 1000` of a 6-decimal millisecond value, so they carry
 *  ~1e-9 of noise. */
export const GRID_EPS = 1e-6

// Guards against a corrupt or hand-edited value producing a zero/absurd beat
// duration and an unbounded draw loop.
const MIN_BPM = 1
const MAX_BPM = 999

/** A constant-tempo span, from one marker up to the next. */
export interface GridSegment {
  /** Index of the marker that opens this segment. */
  index: number
  marker: GridMarker
  /** = marker.start. Segment 0 still extrapolates backwards to -Infinity. */
  start: number
  /** The next marker's start, or Infinity for the final segment. */
  end: number
  /** Seconds per beat: 60 / marker.bpm. */
  beatDur: number
  /** Beats belonging to this segment; Infinity for the final one. */
  beatCount: number
  /** Global beat index of this segment's first beat. */
  baseIndex: number
}

/** One beat line. */
export interface Beat {
  /** Global index: 0 at markers[0], negative before it, +1 per beat. */
  index: number
  time: number
  /** Index of the marker governing it. */
  marker: number
  /** 0..3 within its marker's segment; 0 = downbeat. Restarts at each marker. */
  beatInBar: number
}

export class BeatGrid {
  readonly markers: readonly GridMarker[]
  readonly segments: readonly GridSegment[]

  /** Private: a grid can only be built through `buildBeatGrid`, which is where
   *  the invariants (sorted, deduped, positive finite BPM) are established. */
  private constructor(markers: readonly GridMarker[]) {
    this.markers = markers
    const segs: GridSegment[] = []
    let baseIndex = 0
    for (let i = 0; i < markers.length; i++) {
      const marker = markers[i]
      const end = i + 1 < markers.length ? markers[i + 1].start : Infinity
      const beatDur = 60 / marker.bpm
      // Math.round, not ceil: this makes the gap between a segment's last beat
      // and the next marker 0.5-1.5 beats, so a marker that does not land on an
      // exact beat absorbs the error into one stretched interval instead of
      // producing a beat a few milliseconds before the marker. No two beats can
      // ever be near-duplicates, which is what keeps snapping unambiguous.
      const beatCount =
        end === Infinity ? Infinity : Math.max(1, Math.round((end - marker.start) / beatDur))
      segs.push({ index: i, marker, start: marker.start, end, beatDur, beatCount, baseIndex })
      baseIndex += beatCount
    }
    this.segments = segs
  }

  /** Internal: `markers` must already be sorted, deduped and valid. Use
   *  `buildBeatGrid` instead. */
  static fromNormalized(markers: readonly GridMarker[]): BeatGrid {
    return new BeatGrid(markers)
  }

  get count(): number {
    return this.markers.length
  }

  /** True for a single-marker grid — the constant-tempo case. */
  get isConstant(): boolean {
    return this.markers.length === 1
  }

  get bpmRange(): readonly [number, number] {
    let lo = Infinity
    let hi = -Infinity
    for (const m of this.markers) {
      if (m.bpm < lo) lo = m.bpm
      if (m.bpm > hi) hi = m.bpm
    }
    return [lo, hi]
  }

  // --- which marker is in charge ------------------------------------------

  /**
   * Index of the last marker at or before `t`. Returns 0 when `t` precedes the
   * first marker, because marker 0's tempo extrapolates backwards — and 0 is
   * also the right marker to edit while standing in that region.
   *
   * A time exactly on a marker selects THAT marker, not the previous one, so
   * the BPM readout does not flicker as the playhead crosses.
   */
  markerIndexAt(t: number): number {
    let lo = 0
    let hi = this.markers.length - 1
    let found = 0
    while (lo <= hi) {
      const mid = (lo + hi) >> 1
      if (this.markers[mid].start <= t + GRID_EPS) {
        found = mid
        lo = mid + 1
      } else {
        hi = mid - 1
      }
    }
    return found
  }

  markerAt(t: number): GridMarker {
    return this.markers[this.markerIndexAt(t)]
  }

  bpmAt(t: number): number {
    return this.markerAt(t).bpm
  }

  beatDurationAt(t: number): number {
    return 60 / this.bpmAt(t)
  }

  isOnMarker(t: number, tol: number): boolean {
    return this.markers.some((m) => Math.abs(m.start - t) <= tol)
  }

  /** Index of the nearest marker strictly before `t`, or -1. */
  prevMarkerIndex(t: number, tol: number): number {
    for (let i = this.markers.length - 1; i >= 0; i--) {
      if (this.markers[i].start < t - tol) return i
    }
    return -1
  }

  /** Index of the nearest marker strictly after `t`, or -1. */
  nextMarkerIndex(t: number, tol: number): number {
    for (let i = 0; i < this.markers.length; i++) {
      if (this.markers[i].start > t + tol) return i
    }
    return -1
  }

  // --- beat space ----------------------------------------------------------

  /**
   * Global, real-valued, strictly increasing beat position. Integral exactly on
   * beats. Piecewise-linear with breakpoints at the markers, so `timeOfBeat` is
   * its exact inverse and round-trips are safe.
   */
  beatIndexAt(t: number): number {
    const seg = this.segments[this.markerIndexAt(t)]
    return seg.baseIndex + (t - seg.start) / seg.beatDur
  }

  /** Exact inverse of `beatIndexAt`; accepts fractional and negative `n`. */
  timeOfBeat(n: number): number {
    // Find the segment owning beat n. Segment 0 owns everything below it, the
    // final segment owns everything above (both have unbounded beat ranges).
    let seg = this.segments[0]
    for (const s of this.segments) {
      if (n >= s.baseIndex) seg = s
      else break
    }
    return seg.start + (n - seg.baseIndex) * seg.beatDur
  }

  /** Nearest beat time, clamped to >= 0 (matches the old snapTime). */
  snapToBeat(t: number): number {
    return Math.max(0, this.timeOfBeat(Math.round(this.beatIndexAt(t))))
  }

  /** Full descriptor of the nearest beat. */
  nearestBeat(t: number): Beat {
    const index = Math.round(this.beatIndexAt(t))
    const time = this.timeOfBeat(index)
    const marker = this.markerIndexAt(time)
    const seg = this.segments[marker]
    const withinSegment = index - seg.baseIndex
    return {
      index,
      time,
      marker,
      beatInBar: ((withinSegment % BEATS_PER_BAR) + BEATS_PER_BAR) % BEATS_PER_BAR,
    }
  }

  /**
   * Move `t` by `n` beats. Phase is preserved in BEAT space, not seconds, so a
   * jump crossing a tempo change lands on the musically correct beat rather
   * than a fixed number of seconds away.
   */
  advanceBeats(t: number, n: number): number {
    return this.timeOfBeat(this.beatIndexAt(t) + n)
  }

  /** Signed, fractional beats from `from` to `to`. Inverse of advanceBeats. */
  beatsBetween(from: number, to: number): number {
    return this.beatIndexAt(to) - this.beatIndexAt(from)
  }

  // --- drawing -------------------------------------------------------------

  /** Segments overlapping [from, to], in order. A slice of the prebuilt array,
   *  so this allocates nothing per frame. */
  segmentsIn(from: number, to: number): readonly GridSegment[] {
    let first = 0
    let last = this.segments.length - 1
    for (let i = 0; i < this.segments.length; i++) {
      if (this.segments[i].end <= from) first = i + 1
      if (this.segments[i].start > to) {
        last = i - 1
        break
      }
    }
    if (first > last) return []
    return this.segments.slice(first, last + 1)
  }

  /** Marker indices whose position lies within [from, to]. */
  markersIn(from: number, to: number): number[] {
    const out: number[] = []
    for (let i = 0; i < this.markers.length; i++) {
      const s = this.markers[i].start
      if (s >= from && s <= to) out.push(i)
    }
    return out
  }
}

/**
 * Build a grid from the API's marker list, or `null` when the track has none.
 *
 * Tolerant by design: accepts `undefined` (so a frontend running against an
 * older backend degrades to "no grid" rather than throwing), unsorted input,
 * duplicate positions, negative starts, and non-finite or out-of-range BPMs.
 */
export function buildBeatGrid(markers?: readonly GridMarker[] | null): BeatGrid | null {
  if (!markers || markers.length === 0) return null
  const clean: GridMarker[] = []
  for (const m of markers) {
    if (!m || !Number.isFinite(m.bpm) || !Number.isFinite(m.start)) continue
    if (m.bpm <= 0) continue
    clean.push({
      start: Math.max(0, m.start),
      bpm: Math.min(MAX_BPM, Math.max(MIN_BPM, m.bpm)),
    })
  }
  if (clean.length === 0) return null
  clean.sort((a, b) => a.start - b.start)
  // Collapse markers at the same instant — two segments cannot share a start,
  // or `end - start` would be 0 and the beat count would divide by zero.
  const deduped: GridMarker[] = []
  for (const m of clean) {
    const prev = deduped[deduped.length - 1]
    if (prev && Math.abs(prev.start - m.start) <= GRID_EPS) deduped[deduped.length - 1] = m
    else deduped.push(m)
  }
  return BeatGrid.fromNormalized(deduped)
}
