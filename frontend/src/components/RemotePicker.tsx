import { useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  api,
  ApiError,
  type CollectionStatus,
  type HandshakeResult,
  type PendingEdits,
  type RemoteForm,
  type RemoteInUse,
  type RemoteSettings,
} from '../api'
import { askConfirm } from '../lib/confirm'
import { Icon } from '../lib/icons'
import { remoteHolder } from '../lib/platformCopy'
import { OptionCard } from './OptionCard'

/**
 * The picker's "Remote" step: the Konduktor servers this computer knows, and
 * a form to add one.
 *
 * A remote is saved only once the server has answered — the backend handshakes
 * every address and refuses a wrong password or a version that cannot talk, so
 * a saved remote is one that worked. Connecting can meet two things the user
 * has to decide, and both are asked here, before the library is shown:
 *
 *   * another computer holds the library — take over (its edits stay with the
 *     library), or leave it;
 *   * the library holds edits another computer left unsaved — save them,
 *     discard them, or decide later (they stay unsaved, and Save shows them).
 */

interface Props {
  onOpened: (status: CollectionStatus) => void
}

const input =
  'w-full rounded-lg well px-3 py-2 text-sm text-text outline-none placeholder:text-faint focus:ring-1 focus:ring-accent'

function addressLine(r: RemoteSettings): string {
  const main = r.port ? `${r.host}:${r.port}` : r.host
  if (!r.fallback_host) return main
  const fb = r.fallback_port ? `${r.fallback_host}:${r.fallback_port}` : r.fallback_host
  return `${main} · fallback ${fb}`
}

export function RemotePicker({ onOpened }: Props) {
  const qc = useQueryClient()
  const remotes = useQuery({ queryKey: ['remotes'], queryFn: api.remotes })
  const [editing, setEditing] = useState<RemoteSettings | 'new' | null>(null)
  const [connecting, setConnecting] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState<{ status: CollectionStatus; edits: PendingEdits } | null>(null)

  const connect = async (r: RemoteSettings, takeover = false): Promise<void> => {
    setConnecting(r.id)
    setError(null)
    try {
      const status = await api.openRemote(r.id, takeover)
      if (status.pending_edits) setPending({ status, edits: status.pending_edits })
      else onOpened(status)
    } catch (e) {
      const inUse = e instanceof ApiError && e.status === 409 ? (e.detail as RemoteInUse | null) : null
      if (inUse?.code === 'in_use') {
        const ok = await askConfirm({
          title: `${r.name} is open on another computer`,
          body: (
            <>
              <p>{remoteHolder(inUse.machine, inUse.since, inUse.batch)}</p>
              <p>
                Taking over makes it read-only there. Any edits it has not saved stay with the library,
                and you are asked what to do with them.
              </p>
            </>
          ),
          confirmLabel: 'Take over',
          tone: 'primary',
        })
        if (ok) return connect(r, true)
      } else {
        setError((e as Error).message)
      }
    } finally {
      setConnecting(null)
    }
  }

  const remove = async (r: RemoteSettings) => {
    const ok = await askConfirm({
      title: `Forget ${r.name}?`,
      body: 'Konduktor forgets this server and its saved password. Nothing on the server changes.',
      confirmLabel: 'Forget',
    })
    if (!ok) return
    try {
      await api.deleteRemote(r.id)
      qc.invalidateQueries({ queryKey: ['remotes'] })
      qc.invalidateQueries({ queryKey: ['platforms'] })
    } catch (e) {
      setError((e as Error).message)
    }
  }

  if (editing) {
    return (
      <RemoteFormView
        initial={editing === 'new' ? null : editing}
        onCancel={() => setEditing(null)}
        onSaved={() => {
          setEditing(null)
          qc.invalidateQueries({ queryKey: ['remotes'] })
          qc.invalidateQueries({ queryKey: ['platforms'] })
        }}
      />
    )
  }

  const list = remotes.data ?? []
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-5">
      {remotes.isLoading && <div className="text-sm text-faint">Loading…</div>}
      {list.map((r) => (
        <div key={r.id} className="flex items-center gap-2">
          <div className="min-w-0 flex-1">
            <OptionCard
              icon={<Icon name="server" size={20} />}
              title={r.name}
              subtitle={connecting === r.id ? 'Connecting…' : addressLine(r)}
              busy={connecting !== null}
              onClick={() => void connect(r)}
            />
          </div>
          <button
            title="Edit"
            onClick={() => setEditing(r)}
            className="btn-glass flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-muted hover:text-text"
          >
            <Icon name="pencil" size={14} />
          </button>
          <button
            title="Forget"
            onClick={() => void remove(r)}
            className="btn-glass flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-muted hover:text-pink"
          >
            <Icon name="trash" size={14} />
          </button>
        </div>
      ))}
      <OptionCard
        icon={<Icon name="plus" size={20} />}
        title="Add a remote library"
        subtitle="The address of a Konduktor server, and the account it was set up with"
        onClick={() => setEditing('new')}
      />
      {error && (
        <div className="rounded-xl bg-pink/10 px-3 py-2 text-xs text-pink shadow-[inset_0_0_0_1px_rgb(255_122_154/0.35)]">
          {error}
        </div>
      )}
      {pending && (
        <PendingEditsDialog
          edits={pending.edits}
          onChoose={async (choice) => {
            if (choice === 'save') await api.save()
            if (choice === 'discard') await api.discard()
            onOpened(pending.status)
          }}
          onError={(msg) => setError(msg)}
        />
      )}
    </div>
  )
}

function RemoteFormView({
  initial,
  onCancel,
  onSaved,
}: {
  initial: RemoteSettings | null
  onCancel: () => void
  onSaved: () => void
}) {
  const [name, setName] = useState(initial?.name ?? '')
  const [host, setHost] = useState(initial ? (initial.port ? `${initial.host}:${initial.port}` : initial.host) : '')
  const [fallback, setFallback] = useState(
    initial?.fallback_host
      ? initial.fallback_port
        ? `${initial.fallback_host}:${initial.fallback_port}`
        : initial.fallback_host
      : '',
  )
  const [username, setUsername] = useState(initial?.username ?? '')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [results, setResults] = useState<HandshakeResult[] | null>(null)

  const ready = name.trim() && host.trim() && username.trim() && (password || initial?.has_password)

  const save = async () => {
    setBusy(true)
    setError(null)
    setResults(null)
    const form: RemoteForm = {
      name: name.trim(),
      host: host.trim(),
      fallback_host: fallback.trim() || null,
      username: username.trim(),
      ...(password ? { password } : {}),
    }
    try {
      const r = initial ? await api.editRemote(initial.id, form) : await api.addRemote(form)
      setResults(r.results)
      if (r.results.every((x) => x.ok)) onSaved()
      // A fallback that did not answer is saved anyway — a VPN address is often
      // unreachable at home — but the user is shown it before going back.
    } catch (e) {
      setError((e as Error).message)
      const detail = e instanceof ApiError ? (e.detail as { results?: HandshakeResult[] } | null) : null
      if (detail?.results) setResults(detail.results)
    } finally {
      setBusy(false)
    }
  }

  const saved = results !== null && error === null
  const field = (label: string, el: ReactNode, hint?: string) => (
    <label className="block space-y-1">
      <span className="text-xs font-medium text-muted">{label}</span>
      {el}
      {hint && <span className="block text-[11px] text-faint">{hint}</span>}
    </label>
  )

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-5">
        {field('Name', <input className={input} value={name} onChange={(e) => setName(e.target.value)} placeholder="NAS" autoFocus />)}
        {field(
          'Address',
          <input className={`${input} font-mono`} value={host} onChange={(e) => setHost(e.target.value)} placeholder="192.168.1.20 or nas.local:8765" />,
          'The server’s address on your network. Add :port if it is not 8765, or enter a full https://… address if it sits behind a reverse proxy.',
        )}
        {field(
          'Fallback address',
          <input className={`${input} font-mono`} value={fallback} onChange={(e) => setFallback(e.target.value)} placeholder="Optional — e.g. https://konduktor.example.net" />,
          'Tried when the first one does not answer — away from home, for instance. Same rules: host[:port] for the server itself (8765 if no port), or a full https://… address used exactly as typed.',
        )}
        {field('Username', <input className={input} value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" />)}
        {field(
          'Password',
          <input
            type="password"
            className={input}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={initial?.has_password ? 'Unchanged' : ''}
            autoComplete="current-password"
          />,
          'Kept in this computer’s keychain.',
        )}

        {results && (
          <div className="space-y-1 rounded-xl well px-3 py-2 text-xs">
            {results.map((r) => (
              <div key={r.address} className="flex items-start gap-2">
                <span className={r.ok ? 'text-mint' : r.kind === 'unreachable' ? 'text-gold' : 'text-pink'}>
                  <Icon name={r.ok ? 'check' : r.kind === 'unreachable' ? 'warning' : 'close'} size={13} />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="text-muted">{r.address === 'primary' ? 'Address' : 'Fallback'}</span>{' '}
                  <span className="font-mono text-faint">{r.url}</span>
                  {!r.ok && <span className="block text-faint">{r.error}</span>}
                </span>
              </div>
            ))}
          </div>
        )}
        {error && (
          <div className="rounded-xl bg-pink/10 px-3 py-2 text-xs text-pink shadow-[inset_0_0_0_1px_rgb(255_122_154/0.35)]">
            {error}
          </div>
        )}
      </div>
      <div className="flex items-center justify-end gap-2 border-t border-line px-5 py-3">
        {saved ? (
          <button onClick={onSaved} className="rounded-full btn-primary px-4 py-1.5 text-sm font-semibold">
            Done
          </button>
        ) : (
          <>
            <button onClick={onCancel} className="btn-glass rounded-full px-4 py-1.5 text-sm text-muted hover:text-text">
              Cancel
            </button>
            <button
              disabled={!ready || busy}
              onClick={() => void save()}
              className="rounded-full btn-primary px-4 py-1.5 text-sm font-semibold disabled:cursor-not-allowed disabled:opacity-40"
            >
              {busy ? 'Checking…' : initial ? 'Save' : 'Add'}
            </button>
          </>
        )}
      </div>
    </div>
  )
}

/** The edits another computer left unsaved in the library: save, discard, or
 *  decide later. Esc is "later" — never a discard. */
function PendingEditsDialog({
  edits,
  onChoose,
  onError,
}: {
  edits: PendingEdits
  onChoose: (choice: 'save' | 'discard' | 'later') => Promise<void>
  onError: (msg: string) => void
}) {
  const [busy, setBusy] = useState(false)
  const choose = async (choice: 'save' | 'discard' | 'later') => {
    setBusy(true)
    try {
      await onChoose(choice)
    } catch (e) {
      onError((e as Error).message)
      setBusy(false)
    }
  }
  return createPortal(
    <div
      aria-modal="true"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-6 backdrop-blur-[3px]"
      onKeyDown={(e) => e.key === 'Escape' && !busy && void choose('later')}
    >
      <div className="w-full max-w-sm overflow-hidden glass-overlay">
        <div className="border-b border-line px-5 py-4 text-[15px] font-semibold tracking-tight">
          Unsaved edits from {edits.machine || 'another computer'}
        </div>
        <div className="space-y-2 px-5 py-4 text-sm text-muted">
          <p>This library has edits that were never saved: {edits.summary}.</p>
          <p>Save them, or discard them to start from the library as it was last saved.</p>
        </div>
        <div className="flex items-center gap-2 border-t border-line px-5 py-3">
          <button
            disabled={busy}
            onClick={() => void choose('later')}
            className="rounded-full px-3 py-1.5 text-sm text-faint hover:text-text disabled:opacity-40"
          >
            Decide later
          </button>
          <span className="flex-1" />
          <button
            disabled={busy}
            onClick={() => void choose('discard')}
            className="rounded-full bg-pink/15 px-4 py-1.5 text-sm font-semibold text-[#ffd0da] shadow-[inset_0_0_0_1px_rgb(255_122_154/0.55)] hover:bg-pink/25 disabled:opacity-50"
          >
            Discard
          </button>
          <button
            autoFocus
            disabled={busy}
            onClick={() => void choose('save')}
            className="rounded-full btn-primary px-4 py-1.5 text-sm font-semibold disabled:opacity-50"
          >
            Save
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
