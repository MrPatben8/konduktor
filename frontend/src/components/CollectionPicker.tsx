import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api, type CollectionCandidate, type CollectionStatus, type PlatformOption } from '../api'
import { browsePrompt, platformAvailability } from '../lib/platformCopy'

/**
 * Choose a library to open, in two steps: WHICH PLATFORM, then which library.
 *
 * The platform comes first because it is the question every later answer
 * depends on. Asked the other way round — one flat list of everything found —
 * "Automatic" had to guess across platforms, and it guessed by adapter
 * REGISTRATION ORDER: a plugged-in USB stick could be offered as the user's
 * collection because its driver happened to be imported first. Scoping the
 * shortcuts to a chosen platform removes that class of mistake by construction
 * rather than by ranking candidates more cleverly.
 *
 * Nothing is remembered between launches. The platform list is where every
 * session starts, so the choice is always visible and never inherited from a
 * decision made days ago.
 */

interface Props {
  onOpened: (status: CollectionStatus) => void
  onCancel?: () => void // shown only when switching from an already-loaded library
}

function formatWhen(sec: number | null): string {
  if (!sec) return 'unknown'
  return new Date(sec * 1000).toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

function OptionCard({
  icon,
  title,
  subtitle,
  disabled,
  busy,
  onClick,
}: {
  icon: string
  title: string
  subtitle: string
  disabled?: boolean
  busy?: boolean
  onClick: () => void
}) {
  return (
    <button
      disabled={disabled || busy}
      onClick={onClick}
      className="flex w-full items-center gap-3 rounded-lg border border-line bg-ink-850 px-4 py-3 text-left transition-colors hover:border-accent hover:bg-ink-800 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:border-line disabled:hover:bg-ink-850"
    >
      <span className="text-xl">{icon}</span>
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium text-text">{title}</div>
        <div className="truncate text-xs text-faint">{subtitle}</div>
      </div>
      <span className="text-faint">›</span>
    </button>
  )
}

export function CollectionPicker({ onOpened, onCancel }: Props) {
  const [step, setStep] = useState<'platform' | 'library' | 'browse'>('platform')
  const [platform, setPlatform] = useState<PlatformOption | null>(null)
  const [dir, setDir] = useState<string | undefined>(undefined) // undefined => home
  const [manual, setManual] = useState('')

  // The picker runs BEFORE a library is open, so capabilities do not exist yet —
  // /api/platforms is the pre-load equivalent, and it is the whole of step one.
  const platforms = useQuery({ queryKey: ['platforms'], queryFn: api.platforms })

  const options = useQuery({
    queryKey: ['collectionOptions', platform?.platform],
    queryFn: () => api.collectionOptions(platform!.platform),
    enabled: !!platform,
  })

  const listing = useQuery({
    queryKey: ['fs', dir ?? '~', platform?.platform],
    queryFn: () => api.listDir(dir, platform?.platform),
    enabled: step === 'browse',
  })

  const open = useMutation({
    mutationFn: (path: string) => api.openCollection(path),
    onSuccess: (status) => onOpened(status),
  })

  const data = listing.data
  const opt = options.data
  const auto = opt?.auto ?? null
  const recent = opt?.recent ?? null
  const recentAvailable = !!recent && recent.exists
  // `auto` is the first of these, so the rest are the ones a single "Automatic"
  // card would have thrown away. Two Traktor versions, or two sticks, is normal.
  const alsoFound = (opt?.detected ?? []).slice(1)
  // A drive is not a file: its browse lists folders and confirms the one you are
  // standing in, the way the import folder picker does.
  const picksDirectory = platform?.selects === 'directory'

  const goPlatform = () => {
    setStep('platform')
    setPlatform(null)
    setDir(undefined)
    setManual('')
    open.reset()
  }

  const subtitle =
    step === 'platform'
      ? 'Konduktor opens libraries from each of these.'
      : step === 'library'
        ? 'Open automatically, resume your last one, or browse.'
        : platform
          ? browsePrompt(platform)
          : ''

  const openError = open.isError ? (open.error as Error).message : null

  return (
    <div className="flex h-screen w-screen items-center justify-center bg-ink-950 p-6">
      <div className="flex h-[600px] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-line bg-ink-900 shadow-2xl">
        {/* Header */}
        <div className="flex items-center gap-3 border-b border-line px-5 py-4">
          <div className="min-w-0 flex-1">
            <div className="text-[15px] font-semibold tracking-tight">
              {platform ? `Select your ${platform.name} library` : 'Select your DJ platform'}
            </div>
            <div className="truncate text-xs text-muted">{subtitle}</div>
          </div>
          {step === 'browse' && (
            <button
              onClick={() => setStep('library')}
              className="rounded-md px-2.5 py-1.5 text-sm text-muted hover:bg-ink-800 hover:text-text"
            >
              ‹ Back
            </button>
          )}
          {step === 'library' && (
            <button
              onClick={goPlatform}
              className="rounded-md px-2.5 py-1.5 text-sm text-muted hover:bg-ink-800 hover:text-text"
            >
              ‹ Platforms
            </button>
          )}
          {onCancel && (
            <button
              onClick={onCancel}
              className="rounded-md px-2.5 py-1.5 text-sm text-muted hover:bg-ink-800 hover:text-text"
            >
              Cancel
            </button>
          )}
        </div>

        {step === 'platform' ? (
          /* ---- Step 1: which platform ---- */
          <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-5">
            {platforms.isLoading && <div className="text-sm text-faint">Loading…</div>}
            {/* Every platform stays clickable, including the ones with nothing
                found: a library kept somewhere unusual is still openable by
                browsing, and a disabled row would say otherwise. */}
            {platforms.data?.map((p) => (
              <OptionCard
                key={p.platform}
                icon={p.removable ? '⬒' : p.found > 0 ? '●' : '○'}
                title={p.name}
                subtitle={platformAvailability(p)}
                onClick={() => {
                  setPlatform(p)
                  setStep('library')
                }}
              />
            ))}
          </div>
        ) : step === 'library' ? (
          /* ---- Step 2: which library of that platform ---- */
          <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-5">
            <OptionCard
              icon="✨"
              title="Automatic"
              subtitle={
                auto
                  ? `${auto.label} · modified ${formatWhen(auto.modified)}`
                  : options.isLoading
                    ? `Searching for a ${platform!.name} library…`
                    : platform!.removable
                      ? `No ${platform!.name} drive is connected`
                      : `No ${platform!.name} library in the default location`
              }
              disabled={!auto}
              busy={open.isPending}
              onClick={() => auto && open.mutate(auto.path)}
            />
            <OptionCard
              icon="🕘"
              title="Open last library"
              subtitle={
                recent
                  ? recent.exists
                    ? recent.path
                    : `${recent.path} · no longer exists`
                  : `No previously opened ${platform!.name} library`
              }
              disabled={!recentAvailable}
              busy={open.isPending}
              onClick={() => recent && open.mutate(recent.path)}
            />
            <OptionCard
              icon="📂"
              title="Find library manually"
              subtitle={
                picksDirectory
                  ? 'Browse for the drive'
                  : `Browse for a ${platform!.library_label}`
              }
              busy={open.isPending}
              onClick={() => setStep('browse')}
            />

            {/* Anything else detected. Listed rather than ranked away, because
                "the newest one" is a guess and two of these are a real setup. */}
            {alsoFound.length > 0 && (
              <div className="mt-1">
                <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-faint">
                  Also found
                </div>
                <div className="flex flex-col gap-2">
                  {alsoFound.map((c: CollectionCandidate) => (
                    <button
                      key={c.path}
                      disabled={open.isPending}
                      onClick={() => open.mutate(c.path)}
                      className="flex w-full items-center gap-2 rounded-md border border-line bg-ink-850 px-3 py-2 text-left text-sm text-muted hover:border-accent hover:text-text disabled:opacity-40"
                    >
                      <span className="truncate">{c.label}</span>
                      <span className="ml-auto shrink-0 text-[11px] text-faint">
                        {formatWhen(c.modified)}
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            )}

            {openError && (
              <div className="rounded-md border border-pink/40 bg-ink-850 px-3 py-2 text-xs text-pink">
                {openError}
              </div>
            )}
          </div>
        ) : (
          /* ---- Step 3: browse for it ---- */
          <>
            {/* Path bar */}
            <div className="flex items-center gap-2 border-b border-line bg-ink-850 px-4 py-2">
              <button
                title="Home"
                onClick={() => setDir(data?.home)}
                className="rounded px-2 py-1 text-sm text-muted hover:bg-ink-800 hover:text-text"
              >
                ⌂
              </button>
              <button
                title="Up one level"
                disabled={!data?.parent}
                onClick={() => data?.parent && setDir(data.parent)}
                className="rounded px-2 py-1 text-sm text-muted hover:bg-ink-800 hover:text-text disabled:cursor-not-allowed disabled:opacity-40"
              >
                ↑
              </button>
              <span className="truncate font-mono text-xs text-faint" dir="rtl">
                {data?.path ?? 'Loading…'}
              </span>
            </div>

            {/* Listing. Files are filtered server-side to this platform's own,
                so a Traktor browse never offers a master.db it cannot open — and
                a drive-selecting platform gets no files at all. */}
            <div className="min-h-0 flex-1 overflow-y-auto p-2">
              {listing.isLoading && <div className="p-4 text-sm text-faint">Loading…</div>}
              {data && data.dirs.length === 0 && data.files.length === 0 && (
                <div className="p-4 text-sm text-faint">
                  {picksDirectory ? 'No folders here.' : 'Nothing openable here.'}
                </div>
              )}
              {data?.dirs.map((d) => (
                <button
                  key={d.path}
                  onClick={() => setDir(d.path)}
                  className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm text-muted hover:bg-ink-800 hover:text-text"
                >
                  <span className="text-faint">📁</span>
                  <span className="truncate">{d.name}</span>
                </button>
              ))}
              {data?.files.map((f) => (
                <button
                  key={f.path}
                  onClick={() => open.mutate(f.path)}
                  disabled={open.isPending}
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

            {/* Confirm-the-folder bar, for a platform whose library IS a folder.
                There is nothing in the listing to click, so the target is the
                place you have navigated to. */}
            {picksDirectory && (
              <div className="flex items-center gap-2 border-t border-line px-4 py-2">
                <span className="min-w-0 flex-1 truncate text-xs text-faint">
                  Open <span className="font-mono text-muted">{data?.path ?? '…'}</span> as a{' '}
                  {platform!.name} library
                </span>
                <button
                  disabled={!data?.path || open.isPending}
                  onClick={() => data?.path && open.mutate(data.path)}
                  className="shrink-0 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-ink-950 hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {open.isPending ? 'Opening…' : 'Open this folder'}
                </button>
              </div>
            )}

            {/* Manual path + errors */}
            <div className="border-t border-line px-4 py-3">
              {openError && (
                <div className="mb-2 rounded-md border border-pink/40 bg-ink-850 px-3 py-2 text-xs text-pink">
                  {openError}
                </div>
              )}
              <div className="flex items-center gap-2">
                <input
                  value={manual}
                  onChange={(e) => setManual(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && manual.trim()) open.mutate(manual.trim())
                  }}
                  placeholder={
                    picksDirectory
                      ? '…or paste a full path to the drive'
                      : `…or paste a full path to ${platform!.library_label}`
                  }
                  className="min-w-0 flex-1 rounded-md border border-line bg-ink-850 px-3 py-2 font-mono text-xs text-text outline-none placeholder:text-faint focus:border-accent"
                />
                <button
                  disabled={!manual.trim() || open.isPending}
                  onClick={() => open.mutate(manual.trim())}
                  className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-ink-950 hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {open.isPending ? 'Opening…' : 'Open'}
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
