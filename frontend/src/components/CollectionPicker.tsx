import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api, type CollectionStatus } from '../api'

interface Props {
  onOpened: (status: CollectionStatus) => void
  onCancel?: () => void // shown only when switching from an already-loaded collection
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
  const [mode, setMode] = useState<'choose' | 'browse'>('choose')
  const [dir, setDir] = useState<string | undefined>(undefined) // undefined => home
  const [manual, setManual] = useState('')

  const options = useQuery({ queryKey: ['collectionOptions'], queryFn: api.collectionOptions })
  const listing = useQuery({
    queryKey: ['fs', dir ?? '~'],
    queryFn: () => api.listDir(dir),
    enabled: mode === 'browse',
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

  return (
    <div className="flex h-screen w-screen items-center justify-center bg-ink-950 p-6">
      <div className="flex h-[600px] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-line bg-ink-900 shadow-2xl">
        {/* Header */}
        <div className="flex items-center gap-3 border-b border-line px-5 py-4">
          <div className="flex-1">
            <div className="text-[15px] font-semibold tracking-tight">
              Select your Traktor collection
            </div>
            <div className="text-xs text-muted">
              {mode === 'choose'
                ? 'Open automatically, resume your last one, or browse.'
                : 'Choose a collection.nml file to open.'}
            </div>
          </div>
          {mode === 'browse' && (
            <button
              onClick={() => setMode('choose')}
              className="rounded-md px-2.5 py-1.5 text-sm text-muted hover:bg-ink-800 hover:text-text"
            >
              ‹ Back
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

        {mode === 'choose' ? (
          /* ---- Chooser ---- */
          <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-5">
            <OptionCard
              icon="✨"
              title="Automatic"
              subtitle={
                auto
                  ? `${auto.label} · modified ${formatWhen(auto.modified)}`
                  : options.isLoading
                    ? 'Searching for a Traktor collection…'
                    : 'No Traktor collection found in the default location'
              }
              disabled={!auto}
              busy={open.isPending}
              onClick={() => auto && open.mutate(auto.path)}
            />
            <OptionCard
              icon="🕘"
              title="Open last collection"
              subtitle={
                recent
                  ? recent.exists
                    ? recent.path
                    : `${recent.path} · no longer exists`
                  : 'No previously opened collection'
              }
              disabled={!recentAvailable}
              busy={open.isPending}
              onClick={() => recent && open.mutate(recent.path)}
            />
            <OptionCard
              icon="📂"
              title="Find collection manually"
              subtitle="Browse for a collection.nml file"
              busy={open.isPending}
              onClick={() => setMode('browse')}
            />

            {open.isError && (
              <div className="rounded-md border border-pink/40 bg-ink-850 px-3 py-2 text-xs text-pink">
                {(open.error as Error).message}
              </div>
            )}
          </div>
        ) : (
          /* ---- Manual browser ---- */
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

            {/* Listing */}
            <div className="min-h-0 flex-1 overflow-y-auto p-2">
              {listing.isLoading && <div className="p-4 text-sm text-faint">Loading…</div>}
              {data && data.dirs.length === 0 && data.files.length === 0 && (
                <div className="p-4 text-sm text-faint">No folders or .nml files here.</div>
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

            {/* Manual path + errors */}
            <div className="border-t border-line px-4 py-3">
              {open.isError && (
                <div className="mb-2 rounded-md border border-pink/40 bg-ink-850 px-3 py-2 text-xs text-pink">
                  {(open.error as Error).message}
                </div>
              )}
              <div className="flex items-center gap-2">
                <input
                  value={manual}
                  onChange={(e) => setManual(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && manual.trim()) open.mutate(manual.trim())
                  }}
                  placeholder="…or paste a full path to collection.nml"
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
