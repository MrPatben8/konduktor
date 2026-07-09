import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'

/**
 * Startup update check against GitHub Releases.
 *
 * Compares the running version (`__APP_VERSION__`, baked from package.json at
 * build time) with the latest release's tag (`v<version>-b<N>` — only the
 * semver part counts, so a new build of the same version is NOT an update).
 * Failures are silent: an update check must never nag about itself.
 * "Skip this version" persists `skippedUpdateVersion` to userprefs.json via
 * the prefs API, keyed by the semver part, so a later version re-triggers.
 */

const REPO = 'MrPatben8/konduktor'

interface GithubRelease {
  tag_name: string
  html_url: string
  body: string | null
}

async function fetchLatestRelease(): Promise<GithubRelease> {
  const res = await fetch(`https://api.github.com/repos/${REPO}/releases/latest`, {
    headers: { Accept: 'application/vnd.github+json' },
  })
  if (!res.ok) throw new Error(`GitHub responded ${res.status}`)
  return res.json()
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
  for (const raw of lines.slice(start + 1)) {
    const line = raw.trim()
    if (start !== -1 && (/^#{1,4}\s/.test(line) || line.startsWith('**Full Changelog**'))) break
    if (line) {
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

export function UpdateCheck() {
  const qc = useQueryClient()
  const [dismissed, setDismissed] = useState(false)
  const [skipVersion, setSkipVersion] = useState(false)

  const release = useQuery({
    queryKey: ['latestRelease'],
    queryFn: fetchLatestRelease,
    staleTime: Infinity,
    retry: false,
  })
  // Shares App's ['prefs'] cache entry.
  const prefs = useQuery({ queryKey: ['prefs'], queryFn: api.getPrefs })

  const latest = release.data
  const latestVer = latest ? parseVersion(latest.tag_name) : null
  const currentVer = parseVersion(__APP_VERSION__)
  const latestSemver = latestVer?.join('.')
  const updateAvailable = !!(latestVer && currentVer && isNewer(latestVer, currentVer))
  const skipped = !!latestSemver && prefs.data?.skippedUpdateVersion === latestSemver

  // Wait for prefs before showing so a skipped version never flashes the dialog.
  if (dismissed || !updateAvailable || !latest || !prefs.isSuccess || skipped) return null

  const close = () => {
    if (skipVersion && latestSemver) {
      api
        .patchPrefs({ skippedUpdateVersion: latestSemver })
        .then((p) => qc.setQueryData(['prefs'], p))
        .catch(() => {})
    }
    setDismissed(true)
  }

  const download = () => {
    openExternal(latest.html_url)
    setDismissed(true)
  }

  const changes = whatsChanged(latest.body)

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6"
      onClick={close}
    >
      <div
        className="flex max-h-[80vh] w-full max-w-md flex-col overflow-hidden rounded-xl border border-line bg-ink-900 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="border-b border-line px-5 py-4">
          <div className="text-[15px] font-semibold tracking-tight">Update available</div>
          <div className="text-xs text-muted">
            Konduktor <span className="text-accent">{latest.tag_name}</span> is out — you have v
            {__APP_VERSION__}.
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          <div className="mb-2 text-xs font-medium text-muted">What's changed</div>
          {changes.length ? (
            <ul className="space-y-1.5">
              {changes.map((line, i) => (
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
        </div>

        <div className="flex items-center gap-3 border-t border-line px-5 py-3">
          <label className="flex cursor-pointer items-center gap-2 text-xs text-muted">
            <input
              type="checkbox"
              checked={skipVersion}
              onChange={(e) => setSkipVersion(e.target.checked)}
              className="accent-accent"
            />
            Skip this version
          </label>
          <div className="flex-1" />
          <button
            onClick={close}
            className="rounded-md px-3 py-1.5 text-sm text-muted hover:bg-ink-800 hover:text-text"
          >
            Skip
          </button>
          <button
            onClick={download}
            className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-ink-950 hover:brightness-110"
          >
            Download
          </button>
        </div>
      </div>
    </div>
  )
}
