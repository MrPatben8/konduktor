import type { QueryClient } from '@tanstack/react-query'
import { api } from '../api'
import { askChoice } from './confirm'

const plural = (n: number) => `${n} track${n === 1 ? '' : 's'}`

/**
 * Add tracks to a playlist — the context menu's "Add to" and a drop on a
 * sidebar playlist alike, so the two cannot drift.
 *
 * Tracks the playlist already holds are ASKED about (decided 2026-10-06), but
 * only where the platform can hold a track twice (`playlists.duplicates`);
 * elsewhere they are skipped and the message says so. Nothing is asked when
 * there are none.
 *
 * `moveFrom` is an Option-drag out of another (editable) playlist: once the
 * tracks are in the target they leave the source. Cancelling the duplicates
 * question cancels the move too.
 *
 * Resolves the message to show, or null when the user cancelled.
 */
export async function addTracksToPlaylist(opts: {
  qc: QueryClient
  playlist: { id: string; name: string }
  ids: string[]
  duplicatesAllowed: boolean
  moveFrom?: string
}): Promise<string | null> {
  const { qc, playlist, ids, duplicatesAllowed, moveFrom } = opts
  let res = await api.addEntries(playlist.id, ids, duplicatesAllowed ? 'ask' : 'skip')
  if (res.status === 'duplicates') {
    const fresh = ids.length - res.duplicates
    const all = fresh === 0
    const choice = await askChoice({
      title: all
        ? `${ids.length === 1 ? 'This track is' : `All ${ids.length} tracks are`} already in “${playlist.name}”`
        : `${plural(res.duplicates)} ${res.duplicates === 1 ? 'is' : 'are'} already in “${playlist.name}”`,
      body: all
        ? ids.length === 1
          ? 'Add it again, so the playlist holds it twice?'
          : 'Add them again, so the playlist holds them twice?'
        : `Add only the ${plural(fresh)} not there yet, or add every track — the ${res.duplicates === 1 ? 'one already there' : `${res.duplicates} already there`} a second time?`,
      // All of them there: "skip" would add nothing, which is just Cancel —
      // so the question is only whether to add them again.
      confirmLabel: all ? 'Add again' : `Add ${plural(fresh)}`,
      altLabel: all ? undefined : `Add all ${ids.length}`,
      tone: 'primary',
    })
    if (choice === null) return null
    res = await api.addEntries(playlist.id, ids, choice === 'alt' || all ? 'add' : 'skip')
  }

  let moved = false
  if (moveFrom && moveFrom !== playlist.id) {
    const gone = new Set(ids)
    const current = await api.playlistTracks(moveFrom)
    await api.setEntries(moveFrom, current.map((t) => t.id).filter((id) => !gone.has(id)))
    qc.invalidateQueries({ queryKey: ['playlist', moveFrom] })
    moved = true
  }
  qc.invalidateQueries({ queryKey: ['state'] })
  qc.invalidateQueries({ queryKey: ['playlists'] })
  qc.invalidateQueries({ queryKey: ['playlist', playlist.id] })

  const skipped = ids.length - res.added
  if (moved) return `Moved ${plural(ids.length)} to ${playlist.name}`
  if (res.added === 0) return `Nothing added — ${ids.length === 1 ? 'it is' : 'they are all'} already in ${playlist.name}`
  return `Added ${plural(res.added)} to ${playlist.name}${skipped > 0 ? ` · ${skipped} already there` : ''}`
}
