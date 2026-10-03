import { useCallback } from 'react'
import { useQueryClient, type QueryClient } from '@tanstack/react-query'
import { api } from '../api'

/**
 * The one debounced writer for prefs that change continuously (column layout,
 * deck zoom, loop size).
 *
 * Each of those used to own a 500 ms timer. All of them hydrate from the same
 * ['prefs'] fetch on launch, so their timers fired together and sent several
 * PATCHes at once, which is the burst that once wiped userprefs.json (the
 * backend now serialises them too). Here every change is merged into one
 * pending patch, sent as ONE request after a quiet 500 ms, and requests go out
 * strictly in order, so an older response can never overwrite a newer cache.
 *
 * The queue is module-level, so a component unmounting does not cancel its
 * pending write; `flushPrefs()` sends it at once (QuitGuard calls it before
 * quitting).
 *
 * One-off writes from a dialog may still call `api.patchPrefs` directly.
 */
const DEBOUNCE_MS = 500

let pending: Record<string, unknown> = {}
let timer: number | null = null
let client: QueryClient | null = null
let chain: Promise<void> = Promise.resolve()

export function queuePrefs(qc: QueryClient, patch: Record<string, unknown>): void {
  client = qc
  Object.assign(pending, patch)
  if (timer !== null) window.clearTimeout(timer)
  timer = window.setTimeout(() => void flushPrefs(), DEBOUNCE_MS)
}

/**
 * Send anything queued now. Resolves once every write so far has landed, or
 * after `timeoutMs` if given — quitting must not wait on a hung backend.
 */
export function flushPrefs(timeoutMs?: number): Promise<void> {
  if (timer !== null) {
    window.clearTimeout(timer)
    timer = null
  }
  if (Object.keys(pending).length) {
    const patch = pending
    pending = {}
    chain = chain.then(() =>
      api
        .patchPrefs(patch)
        .then((p) => {
          client?.setQueryData(['prefs'], p)
        })
        .catch(() => {}),
    )
  }
  if (timeoutMs === undefined) return chain
  return Promise.race([chain, new Promise<void>((r) => window.setTimeout(r, timeoutMs))])
}

/** `queuePrefs` bound to this tree's QueryClient. */
export function usePrefsWriter(): (patch: Record<string, unknown>) => void {
  const qc = useQueryClient()
  return useCallback((patch) => queuePrefs(qc, patch), [qc])
}
