import { useState, type ReactNode } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api, type CollectionCandidate, type CollectionStatus, type PlatformOption } from '../api'
import { FileBrowser, useFsListing } from './FileBrowser'
import { browsePrompt, platformAvailability } from '../lib/platformCopy'
import { Icon } from '../lib/icons'
import { PlatformIcon } from '../lib/platformIcons'

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
  lit,
  title,
  subtitle,
  disabled,
  busy,
  onClick,
}: {
  icon: ReactNode
  /** A small lit dot on the icon tile: "something is here". */
  lit?: boolean
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
      className="btn-glass group flex w-full items-center gap-3.5 rounded-2xl px-3 py-3 text-left disabled:cursor-not-allowed disabled:opacity-40"
    >
      <span className="well relative flex h-11 w-11 shrink-0 items-center justify-center rounded-xl text-text">
        {icon}
        {lit && (
          <span className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 rounded-full bg-mint shadow-[0_0_8px_var(--color-mint)] ring-2 ring-[rgb(12_14_24)]" />
        )}
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-sm font-semibold text-text">{title}</div>
        <div className="truncate text-xs text-faint">{subtitle}</div>
      </div>
      <Icon
        name="chevronRight"
        size={14}
        strokeWidth={2.2}
        className="text-faint transition-colors group-hover:text-text"
      />
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

  // Shares FileBrowser's query, so the confirm row below cannot disagree with
  // what is on screen about where the user is standing.
  const here = useFsListing(dir, platform?.platform).data?.path ?? ''

  const open = useMutation({
    mutationFn: (path: string) => api.openCollection(path),
    onSuccess: (status) => onOpened(status),
  })

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
    <div className="flex h-screen w-screen items-center justify-center p-6">
      <div className="glass flex h-[600px] w-full max-w-2xl flex-col overflow-hidden">
        {/* Header */}
        <div className="flex items-center gap-3 border-b border-line px-5 py-4">
          {platform && (
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-ink-800 text-text">
              <PlatformIcon platform={platform.platform} size={22} />
            </span>
          )}
          <div className="min-w-0 flex-1">
            <div className="text-[15px] font-semibold tracking-tight">
              {platform ? `Select your ${platform.name} library` : 'Select your DJ platform'}
            </div>
            <div className="truncate text-xs text-muted">{subtitle}</div>
          </div>
          {step === 'browse' && (
            <button
              onClick={() => setStep('library')}
              className="flex items-center gap-1 rounded-full px-3 py-1.5 text-sm text-muted hover:bg-ink-800 hover:text-text"
            >
              <Icon name="chevronLeft" size={13} strokeWidth={2.2} />
              Back
            </button>
          )}
          {step === 'library' && (
            <button
              onClick={goPlatform}
              className="flex items-center gap-1 rounded-full px-3 py-1.5 text-sm text-muted hover:bg-ink-800 hover:text-text"
            >
              <Icon name="chevronLeft" size={13} strokeWidth={2.2} />
              Platforms
            </button>
          )}
          {onCancel && (
            <button
              onClick={onCancel}
              className="rounded-full px-3 py-1.5 text-sm text-muted hover:bg-ink-800 hover:text-text"
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
                icon={<PlatformIcon platform={p.platform} size={26} />}
                lit={p.found > 0}
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
              icon={<Icon name="sparkle" size={20} />}
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
              icon={<Icon name="history" size={20} />}
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
              icon={<Icon name="folderOpen" size={20} />}
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
                      className="btn-glass flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left text-sm text-muted hover:text-text disabled:opacity-40"
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
              <div className="rounded-xl bg-pink/10 px-3 py-2 text-xs text-pink shadow-[inset_0_0_0_1px_rgb(255_122_154/0.35)]">
                {openError}
              </div>
            )}
          </div>
        ) : (
          /* ---- Step 3: browse for it ---- */
          <>
            <FileBrowser
              mode={picksDirectory ? 'directory' : 'file'}
              platform={platform!.platform}
              path={dir}
              onNavigate={setDir}
              onPickFile={(p) => open.mutate(p)}
              footer={
                /* Confirm-the-folder bar, for a platform whose library IS a
                   folder. There is nothing in the listing to click, so the
                   target is the place you have navigated to. */
                picksDirectory ? (
                  <div className="flex items-center gap-2 border-t border-line px-4 py-2">
                    <span className="min-w-0 flex-1 truncate text-xs text-faint">
                      Open <span className="font-mono text-muted">{here || '…'}</span> as a{' '}
                      {platform!.name} library
                    </span>
                    <button
                      disabled={!here || open.isPending}
                      onClick={() => here && open.mutate(here)}
                      className="shrink-0 rounded-full btn-primary px-3 py-1.5 text-sm font-semibold disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      {open.isPending ? 'Opening…' : 'Open this folder'}
                    </button>
                  </div>
                ) : null
              }
            />

            {/* Manual path + errors */}
            <div className="border-t border-line px-4 py-3">
              {openError && (
                <div className="mb-2 rounded-xl bg-pink/10 px-3 py-2 text-xs text-pink shadow-[inset_0_0_0_1px_rgb(255_122_154/0.35)]">
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
                  className="min-w-0 flex-1 rounded-lg well px-3 py-2 font-mono text-xs text-text outline-none placeholder:text-faint focus:ring-1 focus:ring-accent"
                />
                <button
                  disabled={!manual.trim() || open.isPending}
                  onClick={() => open.mutate(manual.trim())}
                  className="rounded-full btn-primary px-4 py-2 text-sm font-semibold disabled:cursor-not-allowed disabled:opacity-40"
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
