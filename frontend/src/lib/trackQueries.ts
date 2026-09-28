import type { QueryClient } from '@tanstack/react-query'

// Every query that lists COLLECTION tracks, i.e. everything a track edit can
// leave stale. One list, because each edit path used to name its own and the
// export views (added later) were missing from most of them — a rating set in
// an export's playlist did not show until the view was left and re-entered.
// A new view over collection tracks belongs here.
const TRACK_LIST_KEYS = [
  ['tracks'],
  ['playlist'],
  ['export-tracks'],
  ['export-playlist-tracks'],
  ['export-loose'],
] as const

/** Refetch every collection track list, plus the unsaved-edits state. */
export function invalidateTrackLists(qc: QueryClient) {
  for (const queryKey of TRACK_LIST_KEYS) qc.invalidateQueries({ queryKey })
  qc.invalidateQueries({ queryKey: ['state'] })
}
