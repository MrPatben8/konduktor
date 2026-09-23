import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type FsListing, type FsPlace } from '../api'

/**
 * The one file browser. A places rail, a path bar, a listing.
 *
 * Deliberately NOT the surrounding chrome. Its two callers frame it very
 * differently — the collection picker shows it as a step inside an existing
 * full-screen card, the folder picker as a modal stacked on the import dialog —
 * and a shared wrapper would put a modal inside a modal. So each caller keeps
 * its own frame and passes its own confirm affordance as `footer`.
 *
 * `path` is controlled for the same reason: the caller has to know where you
 * are standing to label that footer ("Open this folder", "Use this folder"), so
 * the current directory belongs to the caller and not to this component.
 */

/**
 * The listing, keyed once.
 *
 * Exported because a caller needs the RESOLVED path — the server turns
 * `undefined` into home — and reading it through the same query is honest
 * sharing: TanStack dedupes the request, and there is exactly one definition of
 * the cache key. Passing it back up through a callback would mean an effect
 * firing on every navigation to tell the parent what it already asked for.
 */
export function useFsListing(path: string | undefined, platform?: string) {
  return useQuery({
    queryKey: ['fs', path ?? '~', platform ?? null],
    queryFn: () => api.listDir(path, platform),
  })
}

// Volumes come and go while a dialog is open, so the rail polls. Slower than
// the sidebar's device poll: this is a shortcut list, not the thing you came for.
const PLACES_POLL_MS = 5000

const ICONS: Record<FsPlace['kind'], string> = {
  home: '⌂',
  music: '♫',
  desktop: '🖥',
  documents: '🗎',
  downloads: '⬇',
  volume: '⬒',
  library: '◈',
}

interface Props {
  /** Whether files are listed at all. `directory` shows folders only. */
  mode: 'file' | 'directory'
  /** Narrows which files are openable, and adds that platform's own location. */
  platform?: string
  /** Current directory; `undefined` means "wherever the server calls home". */
  path: string | undefined
  onNavigate: (path: string) => void
  /** Called when a listed FILE is chosen. Unused in `directory` mode. */
  onPickFile?: (path: string) => void
  /** The caller's confirm row, pinned under the listing. */
  footer?: ReactNode
}

export function FileBrowser({ mode, platform, path, onNavigate, onPickFile, footer }: Props) {
  const listing = useFsListing(path, platform)
  const places = useQuery({
    queryKey: ['fs-places', platform ?? null],
    queryFn: () => api.places(platform),
    refetchInterval: PLACES_POLL_MS,
  })

  const data: FsListing | undefined = listing.data
  const here = data?.path ?? ''
  const rows = places.data ?? []

  // Volatile section LAST: a drive appearing or vanishing must not shift the
  // rows above it under a cursor that is already moving towards one.
  const groups: { label: string; rows: FsPlace[] }[] = [
    { label: 'Places', rows: rows.filter((p) => p.kind !== 'volume' && p.kind !== 'library') },
    { label: 'Library', rows: rows.filter((p) => p.kind === 'library') },
    { label: 'Drives', rows: rows.filter((p) => p.kind === 'volume') },
  ].filter((g) => g.rows.length > 0)

  return (
    <div className="flex min-h-0 flex-1">
      {/* ---- Places rail. Navigates, never selects: one click here must not be
              able to open a library or commit an import destination, because a
              sidebar does not look like something that does that. ---- */}
      <div className="w-44 shrink-0 overflow-y-auto border-r border-line bg-ink-950/40 py-2">
        {groups.map((g) => (
          <div key={g.label} className="mb-2">
            <div className="px-3 pb-1 text-[10px] font-semibold uppercase tracking-wider text-faint">
              {g.label}
            </div>
            {g.rows.map((p) => {
              const active = here === p.path
              return (
                <button
                  key={p.path}
                  onClick={() => onNavigate(p.path)}
                  title={p.path}
                  className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors ${
                    active
                      ? 'bg-accent-soft text-text'
                      : 'text-muted hover:bg-ink-800 hover:text-text'
                  }`}
                >
                  <span className="w-3 shrink-0 text-center text-faint">{ICONS[p.kind]}</span>
                  <span className="truncate">{p.name}</span>
                </button>
              )
            })}
          </div>
        ))}
      </div>

      {/* ---- Path bar + listing ---- */}
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-2 border-b border-line bg-ink-850 px-4 py-2">
          <button
            title="Home"
            onClick={() => data?.home && onNavigate(data.home)}
            className="rounded px-2 py-1 text-sm text-muted hover:bg-ink-800 hover:text-text"
          >
            ⌂
          </button>
          <button
            title="Up one level"
            disabled={!data?.parent}
            onClick={() => data?.parent && onNavigate(data.parent)}
            className="rounded px-2 py-1 text-sm text-muted hover:bg-ink-800 hover:text-text disabled:cursor-not-allowed disabled:opacity-40"
          >
            ↑
          </button>
          <span className="truncate font-mono text-xs text-faint" dir="rtl">
            {here || 'Loading…'}
          </span>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          {listing.isLoading && <div className="p-4 text-sm text-faint">Loading…</div>}
          {data && data.dirs.length === 0 && data.files.length === 0 && (
            <div className="p-4 text-sm text-faint">
              {mode === 'directory' ? 'No folders here.' : 'Nothing openable here.'}
            </div>
          )}
          {data?.dirs.map((d) => (
            <button
              key={d.path}
              onClick={() => onNavigate(d.path)}
              className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm text-muted hover:bg-ink-800 hover:text-text"
            >
              <span className="text-faint">📁</span>
              <span className="truncate">{d.name}</span>
            </button>
          ))}
          {/* The server already filtered these to the chosen platform's own, so
              a Traktor browse never offers a master.db it cannot open — and a
              directory-selecting platform is sent no files at all. */}
          {mode === 'file' &&
            data?.files.map((f) => (
              <button
                key={f.path}
                onClick={() => onPickFile?.(f.path)}
                className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm text-text hover:bg-accent-soft"
              >
                <span className="text-accent">♫</span>
                <span className="truncate">{f.name}</span>
                <span className="ml-auto text-[10px] uppercase tracking-wider text-faint">
                  open
                </span>
              </button>
            ))}
        </div>

        {footer}
      </div>
    </div>
  )
}
