import { createPortal } from 'react-dom'
import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'

/**
 * Startup update check against GitHub Releases.
 *
 * Compares the running version (`__APP_VERSION__`, baked from package.json at
 * build time) with release tags (`v<version>-b<N>` — only the semver part
 * counts, so a new build of the same version is NOT an update). The dialog
 * lists the notes of EVERY release newer than the running one, not just the
 * latest's.
 * Failures are silent: an update check must never nag about itself.
 * "Skip this version" persists `skippedUpdateVersion` to userprefs.json via
 * the prefs API, keyed by the semver part, so a later version re-triggers.
 *
 * In the desktop app, Update installs it (decided 2026-10-16,
 * .claude/discussions/discuss-self-update-2026-10-16.md): the shell
 * (src-tauri/src/lib.rs) downloads THIS release's `latest.json` package with
 * progress, verifies and stages it, then quits through the ordinary quit
 * request — so unsaved edits are asked about by QuitGuard — and installs at
 * exit, after the backend is dead, and relaunches. Cancel in that prompt
 * leaves it staged for the next quit. A failure says why and offers the
 * release page (what Download did before); an install that failed at exit is
 * reported by the next launch. A plain browser keeps Download.
 */

const IN_TAURI = '__TAURI_INTERNALS__' in window

const REPO = 'MrPatben8/konduktor'

interface GithubRelease {
  tag_name: string
  html_url: string
  body: string | null
  draft?: boolean
  prerelease?: boolean
}

const MAX_PAGES = 5

/**
 * Every APP release newer than the running version, newest first — the first
 * is the one offered, and all of their notes are shown, so someone several
 * versions behind sees everything they are getting. Not `/releases/latest`:
 * that is whichever release GitHub flags "latest", and the same repo also
 * publishes the stem engine on its own `engine-v*` channel — one flagged
 * latest by mistake would make this check go quiet (its tag does not parse as
 * an app version) with no error. So list releases (newest first) and keep the
 * app tags, paging until one is not newer than the running version.
 */
async function fetchNewerReleases(): Promise<GithubRelease[]> {
  const current = parseVersion(__APP_VERSION__)
  if (!current) return []
  const newer: GithubRelease[] = []
  for (let page = 1; page <= MAX_PAGES; page++) {
    const res = await fetch(
      `https://api.github.com/repos/${REPO}/releases?per_page=100&page=${page}`,
      { headers: { Accept: 'application/vnd.github+json' } },
    )
    if (!res.ok) throw new Error(`GitHub responded ${res.status}`)
    const releases: GithubRelease[] = await res.json()
    for (const r of releases) {
      if (r.draft || r.prerelease) continue
      const v = parseVersion(r.tag_name)
      if (!v || !r.tag_name.startsWith('v')) continue
      if (!isNewer(v, current)) return newer
      newer.push(r)
    }
    if (releases.length < 100) break
  }
  return newer
}

/** The semver triple at the start of a version/tag string ("v0.1.2-b7" → [0,1,2]). */
function parseVersion(s: string): [number, number, number] | null {
  const m = /^v?(\d+)\.(\d+)\.(\d+)/.exec(s.trim())
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null
}

function isNewer(a: [number, number, number], b: [number, number, number]): boolean {
  for (let i = 0; i < 3; i++) {
    if (a[i] !== b[i]) return a[i] > b[i]
  }
  return false
}

/**
 * The "What's Changed" bullet lines from an auto-generated release body
 * (section ends at the next heading or the Full Changelog link). If the
 * section is missing (hand-written notes), fall back to the whole body.
 * The auto-generated "by @user in <PR url>" credit suffix is stripped.
 */
function whatsChanged(body: string | null): string[] {
  if (!body) return []
  const lines = body.split(/\r?\n/)
  const start = lines.findIndex((l) => /^#{2,4}\s+what'?s changed/i.test(l.trim()))
  const out: string[] = []
  // Every release body opens with a "Download" section (build.yml): the
  // fallback must not list the installer links as changes.
  let inDownload = false
  for (const raw of lines.slice(start + 1)) {
    const line = raw.trim()
    if (start !== -1 && (/^#{1,4}\s/.test(line) || line.startsWith('**Full Changelog**'))) break
    if (/^#{1,4}\s/.test(line)) {
      inDownload = /^#{1,4}\s+download/i.test(line)
      continue
    }
    if (line && !inDownload) {
      out.push(
        line.replace(/^[*-]\s+/, '').replace(/\s+by\s+@[\w-]+\s+in\s+https?:\/\/\S+$/i, ''),
      )
    }
  }
  return out
}

/** Open a URL in the system browser (Tauri shell-open in the desktop app). */
async function openExternal(url: string) {
  if ('__TAURI_INTERNALS__' in window) {
    try {
      const { open } = await import('@tauri-apps/plugin-shell')
      await open(url)
      return
    } catch {
      // fall through to window.open
    }
  }
  window.open(url, '_blank', 'noopener')
}

/** Render a changelog line, turning bare URLs into links (PR URLs as #N). */
function ChangelogLine({ text }: { text: string }) {
  const parts = text.split(/(https?:\/\/\S+)/g)
  return (
    <>
      {parts.map((part, i) => {
        if (!/^https?:\/\//.test(part)) return <span key={i}>{part}</span>
        const pr = /\/pull\/(\d+)/.exec(part)
        return (
          <a
            key={i}
            href={part}
            onClick={(e) => {
              e.preventDefault()
              openExternal(part)
            }}
            className="text-accent hover:underline"
          >
            {pr ? `#${pr[1]}` : part}
          </a>
        )
      })}
    </>
  )
}

type Phase =
  | { kind: 'offer' }
  | { kind: 'downloading'; downloaded: number; total: number | null }
  | { kind: 'restarting' }
  | { kind: 'deferred' }
  | { kind: 'failed'; error: string }

interface UpdateFailure {
  version: string
  error: string
}

const mb = (bytes: number) => (bytes / 1e6).toFixed(0)

async function lastInstallFailure(): Promise<UpdateFailure | null> {
  if (!IN_TAURI) return null
  const { invoke } = await import('@tauri-apps/api/core')
  return (await invoke<UpdateFailure | null>('update_take_failure')) ?? null
}

export function UpdateCheck() {
  const qc = useQueryClient()
  const [dismissed, setDismissed] = useState(false)
  const [skipVersion, setSkipVersion] = useState(false)
  const [phase, setPhase] = useState<Phase>({ kind: 'offer' })

  const releases = useQuery({
    queryKey: ['newerReleases'],
    queryFn: fetchNewerReleases,
    staleTime: Infinity,
    retry: false,
  })
  // Read (and cleared) once: the shell writes it when an install fails at exit.
  const lastFailure = useQuery({
    queryKey: ['updateFailure'],
    queryFn: lastInstallFailure,
    staleTime: Infinity,
    retry: false,
  })
  // Shares App's ['prefs'] cache entry.
  const prefs = useQuery({ queryKey: ['prefs'], queryFn: api.getPrefs })

  // Cancel in QuitGuard's Save / Discard prompt: the update waits for the next quit.
  useEffect(() => {
    if (phase.kind !== 'restarting') return
    let unlisten: (() => void) | undefined
    let cancelled = false
    void import('@tauri-apps/api/event').then(async ({ listen }) => {
      const off = await listen('konduktor://update-deferred', () => setPhase({ kind: 'deferred' }))
      if (cancelled) off()
      else unlisten = off
    })
    return () => {
      cancelled = true
      unlisten?.()
    }
  }, [phase.kind])

  const newer = releases.data ?? []
  const latest = newer[0]
  const latestSemver = latest ? parseVersion(latest.tag_name)?.join('.') : undefined
  const updateAvailable = !!latest
  const skipped = !!latestSemver && prefs.data?.skippedUpdateVersion === latestSemver
  const failedBefore = lastFailure.data ?? null
  const releasePage = latest?.html_url ?? `https://github.com/${REPO}/releases`

  if (dismissed) return null
  // Wait for prefs before showing so a skipped version never flashes the dialog.
  const offering = updateAvailable && !!latest && prefs.isSuccess && !skipped
  if (!offering && !failedBefore) return null

  const busy = phase.kind === 'downloading' || phase.kind === 'restarting'

  const close = () => {
    if (busy) return
    if (skipVersion && latestSemver) {
      api
        .patchPrefs({ skippedUpdateVersion: latestSemver })
        .then((p) => qc.setQueryData(['prefs'], p))
        .catch(() => {})
    }
    setDismissed(true)
  }

  const openReleasePage = () => {
    openExternal(releasePage)
    setDismissed(true)
  }

  const update = async () => {
    if (!latest) return
    setPhase({ kind: 'downloading', downloaded: 0, total: null })
    try {
      const { invoke, Channel } = await import('@tauri-apps/api/core')
      const onProgress = new Channel<{ downloaded: number; total: number | null }>()
      onProgress.onmessage = (p) => setPhase({ kind: 'downloading', ...p })
      await invoke('update_download', { tag: latest.tag_name, onProgress })
      setPhase({ kind: 'restarting' })
      await invoke('update_restart')
    } catch (e) {
      setPhase({ kind: 'failed', error: e instanceof Error ? e.message : String(e) })
    }
  }

  // An install that failed at the last exit, with no newer offer to make.
  const reportOnly = !offering && !!failedBefore
  const failure =
    phase.kind === 'failed'
      ? phase.error
      : reportOnly || (failedBefore && phase.kind === 'offer')
        ? `The update to v${failedBefore!.version} didn't install: ${failedBefore!.error}`
        : null

  const title =
    phase.kind === 'downloading'
      ? 'Downloading update…'
      : phase.kind === 'restarting'
        ? 'Installing update…'
        : phase.kind === 'deferred'
          ? 'Update ready'
          : phase.kind === 'failed' || reportOnly
            ? "Update didn't install"
            : 'Update available'

  // One section per release the user is skipping past, newest first; a heading
  // only when there is more than one, so a one-step update reads as before.
  const sections = newer.map((r) => ({ tag: r.tag_name, changes: whatsChanged(r.body) }))
  const grouped = sections.length > 1

  return createPortal(
    <div
      aria-modal="true" className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-[3px] p-6"
      onClick={close}
    >
      <div
        className="flex max-h-[80vh] w-full max-w-md flex-col overflow-hidden glass-overlay"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="border-b border-line px-5 py-4">
          <div className="text-[15px] font-semibold tracking-tight">{title}</div>
          {latest && !reportOnly && (
            <div className="text-xs text-muted">
              Konduktor <span className="text-accent">{latest.tag_name}</span> is out — you have v
              {__APP_VERSION__}.
            </div>
          )}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {failure && <p className="mb-3 text-sm text-pink">{failure}</p>}
          {phase.kind === 'downloading' ? (
            <div className="space-y-2">
              <div className="well h-2 overflow-hidden rounded-full">
                <div
                  className="h-full rounded-full bg-accent transition-[width] duration-150"
                  style={{
                    width: phase.total ? `${Math.min(100, (phase.downloaded / phase.total) * 100)}%` : '0%',
                  }}
                />
              </div>
              <div className="text-xs tabular-nums text-muted">
                {mb(phase.downloaded)}
                {phase.total ? ` / ${mb(phase.total)}` : ''} MB
              </div>
            </div>
          ) : phase.kind === 'restarting' ? (
            <div className="text-sm text-muted">Konduktor will quit, install the update and reopen.</div>
          ) : phase.kind === 'deferred' ? (
            <div className="text-sm text-muted">
              The update will install the next time you quit Konduktor.
            </div>
          ) : reportOnly ? (
            <div className="text-sm text-muted">
              You can download the installer from the release page instead.
            </div>
          ) : (
            <>
              <div className="mb-2 text-xs font-medium text-muted">
                What's changed{grouped ? ` since v${__APP_VERSION__}` : ''}
              </div>
              <div className="space-y-4">
                {sections.map((s) => (
                  <section key={s.tag}>
                    {grouped && (
                      <div className="mb-1.5 text-xs font-semibold text-accent">{s.tag}</div>
                    )}
                    {s.changes.length ? (
                      <ul className="space-y-1.5">
                        {s.changes.map((line, i) => (
                          <li key={i} className="flex gap-2 text-sm text-text">
                            <span className="text-faint">•</span>
                            <span className="min-w-0 break-words">
                              <ChangelogLine text={line} />
                            </span>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <div className="text-sm text-faint">No release notes.</div>
                    )}
                  </section>
                ))}
              </div>
            </>
          )}
        </div>

        <div className="flex items-center gap-3 border-t border-line px-5 py-3">
          {phase.kind === 'offer' && !reportOnly && (
            <label className="flex cursor-pointer items-center gap-2 text-xs text-muted">
              <input
                type="checkbox"
                checked={skipVersion}
                onChange={(e) => setSkipVersion(e.target.checked)}
                className="accent-accent"
              />
              Skip this version
            </label>
          )}
          <div className="flex-1" />
          {phase.kind === 'offer' && !reportOnly && (
            <>
              <button
                onClick={close}
                className="rounded-md px-3 py-1.5 text-sm text-muted hover:bg-ink-800 hover:text-text"
              >
                Skip
              </button>
              <button
                onClick={IN_TAURI ? () => void update() : openReleasePage}
                className="rounded-full btn-primary px-3 py-1.5 text-sm font-semibold"
              >
                {IN_TAURI ? 'Update' : 'Download'}
              </button>
            </>
          )}
          {(phase.kind === 'failed' || reportOnly) && (
            <>
              <button
                onClick={close}
                className="rounded-md px-3 py-1.5 text-sm text-muted hover:bg-ink-800 hover:text-text"
              >
                Close
              </button>
              <button
                onClick={openReleasePage}
                className="rounded-full btn-primary px-3 py-1.5 text-sm font-semibold"
              >
                Open release page
              </button>
            </>
          )}
          {phase.kind === 'deferred' && (
            <button
              onClick={() => setDismissed(true)}
              className="rounded-full btn-primary px-3 py-1.5 text-sm font-semibold"
            >
              OK
            </button>
          )}
        </div>
      </div>
    </div>,
    document.body,
  )
}
