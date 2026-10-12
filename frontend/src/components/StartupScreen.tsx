import { useEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { setApiBase } from '../api'
import { Icon } from '../lib/icons'

/**
 * What the desktop window shows while the backend starts, and the gate that
 * keeps the app from rendering before it can reach the backend.
 *
 * The shell (src-tauri/src/lib.rs) opens the window at once and starts the
 * sidecar beside it; this asks `backend_status` and listens for
 * `konduktor://backend`. A normal launch is about half a second. The first
 * launch after an install or update can take much longer — macOS checks every
 * native library in the sidecar once — so after `SLOW_MS` a note says why,
 * and after `STUCK_MS` the user may quit or keep waiting. If the sidecar
 * exits, its last output is shown with Try again (`backend_restart`).
 *
 * Until the backend is ready, nothing else is mounted: App, QuitGuard and
 * UpdateCheck would all query an API with no address yet. So this answers
 * the shell's quit request itself (there is nothing unsaved to ask about),
 * and stops answering once ready, when QuitGuard takes over.
 *
 * In a plain browser (dev) there is no shell and Vite proxies /api, so the
 * children render at once.
 */

type BackendStatus =
  | { state: 'starting' }
  | { state: 'ready'; url: string }
  | { state: 'stopped'; code: number | null; signal: number | null; log: string[] }

/** When the "takes longer" note appears. */
const SLOW_MS = 6_000
/** When the user is offered Quit / Keep waiting (and again after each wait). */
const STUCK_MS = 90_000
/** The cross-fade into the app. */
const FADE_MS = 450

const IN_SHELL = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window

export function StartupGate({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<BackendStatus | null>(null)
  const [ready, setReady] = useState(!IN_SHELL)
  const [leaving, setLeaving] = useState(false)
  const [gone, setGone] = useState(!IN_SHELL)

  // The backend's status: asked once, then followed. An event that arrives
  // before the answer is newer than it, so the answer is then dropped.
  useEffect(() => {
    if (!IN_SHELL) return
    let unlisten: (() => void) | undefined
    let cancelled = false
    void (async () => {
      const [{ listen }, { invoke }] = await Promise.all([
        import('@tauri-apps/api/event'),
        import('@tauri-apps/api/core'),
      ])
      let heard = false
      const off = await listen<BackendStatus>('konduktor://backend', (e) => {
        heard = true
        setStatus(e.payload)
      })
      if (cancelled) return off()
      unlisten = off
      const now = await invoke<BackendStatus>('backend_status')
      if (!heard && !cancelled) setStatus(now)
    })()
    return () => {
      cancelled = true
      unlisten?.()
    }
  }, [])

  // Ready: point the API at the sidecar, mount the app, fade this out over it.
  // Once ready it stays ready — a later crash is not a startup problem.
  useEffect(() => {
    if (status?.state !== 'ready' || ready) return
    setApiBase(status.url)
    setReady(true)
    setLeaving(true)
    const t = window.setTimeout(() => setGone(true), FADE_MS)
    return () => window.clearTimeout(t)
  }, [status, ready])

  // Before the app is mounted, a quit request is answered here: nothing can
  // be unsaved yet. Stops at ready, so it never races QuitGuard.
  useEffect(() => {
    if (!IN_SHELL || ready) return
    let unlisten: (() => void) | undefined
    let cancelled = false
    void (async () => {
      const [{ listen }, { invoke }] = await Promise.all([
        import('@tauri-apps/api/event'),
        import('@tauri-apps/api/core'),
      ])
      const off = await listen('konduktor://quit-requested', async () => {
        await invoke('quit_ack')
        await invoke('quit_now')
      })
      if (cancelled) off()
      else unlisten = off
    })()
    return () => {
      cancelled = true
      unlisten?.()
    }
  }, [ready])

  return (
    <>
      {ready && children}
      {!gone && <StartupScreen status={status} leaving={leaving} />}
    </>
  )
}

async function shell(command: 'backend_restart' | 'quit_now') {
  const { invoke } = await import('@tauri-apps/api/core')
  await invoke(command)
}

function StartupScreen({ status, leaving }: { status: BackendStatus | null; leaving: boolean }) {
  const stopped = status?.state === 'stopped' ? status : null
  return createPortal(
    <div
      className="fixed inset-0 z-[90] flex items-center justify-center p-6 transition-opacity ease-out"
      style={{ opacity: leaving ? 0 : 1, transitionDuration: `${FADE_MS}ms`, pointerEvents: leaving ? 'none' : undefined }}
    >
      {stopped ? <StoppedCard status={stopped} /> : <Starting />}
      <div className="absolute inset-x-0 bottom-[22px] text-center font-mono text-[11px] tracking-[0.02em] text-faint">
        v{__APP_VERSION__}
      </div>
    </div>,
    document.body,
  )
}

/** The OS doing the checking, for the slow-launch note. */
function osName(): string {
  const ua = navigator.userAgent
  if (ua.includes('Windows')) return 'Windows'
  if (ua.includes('Mac')) return 'macOS'
  return 'your system'
}

function Starting() {
  // Timed from mount: Try again replaces the failure card with a fresh one,
  // so each attempt gets its own clock.
  const [startedAt] = useState(() => Date.now())
  const [stuckAt, setStuckAt] = useState(STUCK_MS)
  const [elapsed, setElapsed] = useState(0)
  useEffect(() => {
    const t = window.setInterval(() => setElapsed(Date.now() - startedAt), 250)
    return () => window.clearInterval(t)
  }, [startedAt])

  const slow = elapsed >= SLOW_MS
  const stuck = elapsed >= stuckAt
  return (
    <div className="flex flex-col items-center text-center">
      <StartupMark />
      <div className="mt-[22px] text-[26px] font-semibold tracking-[-0.02em]">Konduktor</div>
      <div className="mt-1.5 min-h-[21px] text-sm text-muted" role="status" aria-live="polite">
        {stuck ? 'Still starting…' : 'Starting…'}
      </div>
      <p
        className="mt-[18px] max-w-[46ch] text-[13px] leading-[1.55] text-faint transition-[opacity,transform] duration-[600ms]"
        style={{ opacity: slow ? 1 : 0, transform: slow ? 'none' : 'translateY(4px)' }}
        aria-hidden={!slow}
      >
        The first launch after installing or updating takes longer while {osName()} checks the app. Later
        launches start in a second or two.
      </p>
      <div
        className="mt-4 flex justify-center gap-2 transition-opacity duration-[400ms]"
        style={{ opacity: stuck ? 1 : 0, pointerEvents: stuck ? undefined : 'none' }}
        aria-hidden={!stuck}
      >
        <button
          tabIndex={stuck ? 0 : -1}
          onClick={() => void shell('quit_now')}
          className="btn-glass rounded-full px-4 py-2 text-sm text-muted hover:text-text"
        >
          Quit
        </button>
        <button
          tabIndex={stuck ? 0 : -1}
          onClick={() => setStuckAt(elapsed + STUCK_MS)}
          className="btn-glass rounded-full px-4 py-2 text-sm text-muted hover:text-text"
        >
          Keep waiting
        </button>
      </div>
    </div>
  )
}

function StoppedCard({ status }: { status: Extract<BackendStatus, { state: 'stopped' }> }) {
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)
  const logRef = useRef<HTMLPreElement>(null)
  const summary =
    status.code !== null
      ? `The library service exited with code ${status.code}.`
      : status.signal !== null
        ? `The library service was stopped by signal ${status.signal}.`
        : null
  const lines = status.log.length ? status.log : ['It wrote nothing before stopping.']
  const details = [`Konduktor v${__APP_VERSION__}`, ...(summary ? [summary] : []), ...lines].join('\n')

  // Show the end of the output: that is where the error is.
  useEffect(() => {
    const el = logRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [status])

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(details)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      // No clipboard access: select the text so Cmd/Ctrl+C works.
      const el = logRef.current
      if (!el) return
      const range = document.createRange()
      range.selectNodeContents(el)
      window.getSelection()?.removeAllRanges()
      window.getSelection()?.addRange(range)
    }
  }

  const retry = async () => {
    setBusy(true)
    try {
      await shell('backend_restart')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="startup-failed-title"
      className="glass-overlay w-full max-w-[560px] overflow-hidden"
    >
      <div className="flex items-start gap-3.5 px-6 pb-4 pt-[22px]">
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-pink/10 text-pink shadow-[inset_0_0_0_1px_rgb(255_122_154/0.35)]">
          <Icon name="warning" size={18} />
        </span>
        <div className="min-w-0">
          <h2 id="startup-failed-title" className="text-base font-semibold tracking-[-0.01em]">
            Konduktor couldn't start
          </h2>
          <p className="mt-1 text-[13px] leading-[1.55] text-muted">
            Its library service stopped before it was ready. Nothing in your libraries was changed. Try again, and
            if it keeps happening, send the details below with your report.
          </p>
        </div>
      </div>
      <pre
        ref={logRef}
        className="well mx-6 max-h-[180px] select-text overflow-auto rounded-xl px-3.5 py-3 font-mono text-[11.5px] leading-[1.6] text-muted"
      >
        {summary && <div>{summary}</div>}
        {lines.map((line, i) => (
          <div key={i} className={i === lines.length - 1 ? 'text-pink' : undefined}>
            {line || ' '}
          </div>
        ))}
      </pre>
      <div className="flex items-center gap-2 px-6 pb-5 pt-4">
        <button onClick={() => void copy()} className="rounded-full px-2.5 py-2 text-sm text-faint hover:text-text">
          {copied ? 'Copied' : 'Copy details'}
        </button>
        <div className="flex-1" />
        <button
          onClick={() => void shell('quit_now')}
          className="btn-glass rounded-full px-4 py-2 text-sm text-muted hover:text-text"
        >
          Quit
        </button>
        <button
          autoFocus
          disabled={busy}
          onClick={() => void retry()}
          className="btn-primary rounded-full px-4 py-2 text-sm font-semibold disabled:opacity-50"
        >
          Try again
        </button>
      </div>
    </div>
  )
}

// ---- The mark: the app icon, with a track scrolling under a fixed playhead ----

const BEAT = 60 / 128
/** CSS px per second of audio, at the mark's 104 px. */
const SPEED = 46
/** 2 px bars with a 1 px gap: the main waveform's rhythm. */
const BAR = 2
const SLOT = 3
/** The icon's own four bands, back to front. */
const BANDS = ['#ff6a3d', '#8f6bff', '#3f9dff', '#7fe6ff']

const jitter = (i: number) => {
  const x = Math.sin(i * 12.9898) * 43758.5453
  return x - Math.floor(x)
}

/** Band levels (0..1) of a generated house track at time t: a kick on every
 *  beat, offbeat bass and hats, a clap on 2 and 4, a fill every 16 beats. */
function levels(t: number) {
  const n = Math.floor(t / BEAT)
  const pos = t - n * BEAT
  const in16 = n % 16
  const fill = in16 >= 14
  const sixteenth = BEAT / 4
  const p16 = t % sixteenth
  const kick = fill ? 0 : Math.exp(-pos / 0.11)
  const bass = pos > BEAT / 2 ? 0.5 * Math.exp(-(pos - BEAT / 2) / 0.16) : 0
  const roll = fill ? 0.35 + (0.5 * ((in16 - 14) * 4 + Math.floor(pos / sixteenth))) / 8 : 0
  const clap = (n % 2 === 1 ? 0.8 : 0) * Math.exp(-pos / 0.07) + roll * Math.exp(-p16 / 0.05)
  const pad = 0.22 + 0.08 * Math.sin(t * 0.9)
  const hat = 0.85 * Math.exp(-Math.abs(pos - BEAT / 2) / 0.025) + 0.3 * Math.exp(-p16 / 0.02)
  return { low: Math.max(kick, bass), mid: Math.max(clap, pad, kick * 0.5), high: Math.max(hat, clap * 0.6) }
}

function drawWave(ctx: CanvasRenderingContext2D, w: number, h: number, scroll: number) {
  ctx.clearRect(0, 0, w, h)
  const mid = h / 2
  const half = h * 0.34
  const centre = w / 2
  const first = Math.floor(scroll / SLOT) - 1
  const last = first + Math.ceil(w / SLOT) + 2
  for (let i = first; i <= last; i++) {
    const x = i * SLOT - scroll
    const t = (i * SLOT) / SPEED
    // The peak over the slot, so a kick never falls between two samples.
    const a = levels(t)
    const b = levels(t + SLOT / SPEED / 2)
    const low = Math.max(a.low, b.low)
    const md = Math.max(a.mid, b.mid)
    const hi = Math.max(a.high, b.high)
    const j = 0.9 + 0.2 * jitter(i)
    const h0 = half * Math.min(1, (0.16 + 0.84 * Math.max(low, md * 0.8, hi * 0.55)) * j)
    const h1 = Math.min(h0 * 0.86, half * (0.1 + 0.6 * md) * j)
    const h2 = Math.min(h1 * 0.84, half * (0.07 + 0.5 * hi) * j)
    const h3 = Math.min(h2 * 0.78, half * (0.04 + 0.32 * hi) * j)
    // Played audio, left of the playhead, sits dimmer, as on a deck.
    ctx.globalAlpha = x + BAR / 2 < centre ? 0.6 : 1
    ;[h0, h1, h2, h3].forEach((bh, k) => {
      ctx.fillStyle = BANDS[k]
      ctx.fillRect(x, mid - bh, BAR, bh * 2)
    })
  }
  ctx.globalAlpha = 1
  // Bars enter and leave softly at the icon's edges.
  ctx.globalCompositeOperation = 'destination-in'
  const g = ctx.createLinearGradient(0, 0, w, 0)
  g.addColorStop(0, 'rgba(0,0,0,0)')
  g.addColorStop(0.14, '#000')
  g.addColorStop(0.86, '#000')
  g.addColorStop(1, 'rgba(0,0,0,0)')
  ctx.fillStyle = g
  ctx.fillRect(0, 0, w, h)
  ctx.globalCompositeOperation = 'source-over'
}

function StartupMark() {
  const canvas = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const el = canvas.current
    const ctx = el?.getContext('2d')
    if (!el || !ctx) return
    const w = el.offsetWidth
    const h = el.offsetHeight
    const dpr = window.devicePixelRatio || 1
    el.width = Math.round(w * dpr)
    el.height = Math.round(h * dpr)
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    // Reduced motion: one still frame.
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      drawWave(ctx, w, h, 0)
      return
    }
    let raf = 0
    let start: number | null = null
    const frame = (now: number) => {
      start ??= now
      drawWave(ctx, w, h, ((now - start) / 1000) * SPEED)
      raf = requestAnimationFrame(frame)
    }
    raf = requestAnimationFrame(frame)
    return () => cancelAnimationFrame(raf)
  }, [])

  return (
    <div
      role="img"
      aria-label="Konduktor"
      className="relative h-[104px] w-[104px] [filter:drop-shadow(0_22px_40px_rgb(0_0_0/0.55))]"
    >
      <svg viewBox="0 0 100 100" className="absolute inset-0 h-full w-full" aria-hidden>
        <defs>
          <clipPath id="startup-clip">
            <rect x="2" y="2" width="96" height="96" rx="22" />
          </clipPath>
          <filter id="startup-blur" filterUnits="userSpaceOnUse" x="-60" y="-60" width="220" height="220">
            <feGaussianBlur stdDeviation="13" />
          </filter>
        </defs>
        <g clipPath="url(#startup-clip)">
          <rect width="100" height="100" fill="#07070d" />
          <g filter="url(#startup-blur)">
            <circle cx="22" cy="18" r="32" fill="#6d4aff" />
            <circle cx="90" cy="28" r="26" fill="#e0409a" />
            <circle cx="76" cy="96" r="32" fill="#12b5c9" />
            <circle cx="8" cy="82" r="26" fill="#2f3fe0" />
          </g>
          <rect width="100" height="100" fill="#05060a" opacity="0.5" />
        </g>
      </svg>
      {/* Clipped to the icon's rounded square: rx 22 inside a 2-unit inset. */}
      <canvas
        ref={canvas}
        aria-hidden
        className="absolute"
        style={{ inset: '1.92%', width: '96.15%', height: '96.15%', borderRadius: '22.9%' }}
      />
      <svg viewBox="0 0 100 100" className="absolute inset-0 h-full w-full" aria-hidden>
        <defs>
          <clipPath id="startup-clip-top">
            <rect x="2" y="2" width="96" height="96" rx="22" />
          </clipPath>
          <filter id="startup-shadow" filterUnits="userSpaceOnUse" x="-20" y="-20" width="140" height="140">
            <feGaussianBlur stdDeviation="2.2" />
          </filter>
          <linearGradient id="startup-sheen" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="#fff" stopOpacity=".16" />
            <stop offset=".42" stopColor="#fff" stopOpacity="0" />
          </linearGradient>
          <linearGradient id="startup-rim" x1="0" y1="0" x2="0.4" y2="1">
            <stop offset="0" stopColor="#fff" stopOpacity=".55" />
            <stop offset=".5" stopColor="#fff" stopOpacity=".08" />
            <stop offset="1" stopColor="#fff" stopOpacity=".22" />
          </linearGradient>
        </defs>
        <g clipPath="url(#startup-clip-top)">
          <g filter="url(#startup-shadow)" fill="#000" opacity=".95">
            <rect x="45.5" y="11" width="9" height="78" />
          </g>
          <rect x="47.8" y="12" width="4.4" height="76" fill="#fff" />
          <path d="M42 12h16L50 21z M42 88h16L50 79z" fill="#fff" />
          <rect width="100" height="100" fill="url(#startup-sheen)" />
        </g>
        <rect x="2.4" y="2.4" width="95.2" height="95.2" rx="21.6" fill="none" stroke="url(#startup-rim)" strokeWidth=".8" />
      </svg>
    </div>
  )
}
