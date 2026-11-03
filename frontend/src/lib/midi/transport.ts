// Where MIDI comes from. The desktop app reads ports in the shell
// (src-tauri/src/midi.rs: WKWebView has no Web MIDI); a browser under
// `./dev.sh` uses Web MIDI (Chrome) — same interface, so nothing above here
// knows which. Neither (Safari, Firefox without the permission) = no MIDI.

export type OnBytes = (batch: ArrayLike<number>[]) => void

export interface MidiTransport {
  ports(): Promise<string[]>
  /** Start receiving; resolves to the function that stops it. */
  open(port: string, onBytes: OnBytes): Promise<() => void>
}

/** "X", "X #2" for two identical controllers — the shell names them the same way. */
export function uniqueNames(names: string[]): string[] {
  const seen = new Map<string, number>()
  return names.map((n) => {
    const k = (seen.get(n) ?? 0) + 1
    seen.set(n, k)
    return k === 1 ? n : `${n} #${k}`
  })
}

function tauriTransport(): MidiTransport {
  const core = () => import('@tauri-apps/api/core')
  return {
    ports: async () => (await core()).invoke<string[]>('midi_ports'),
    open: async (port, onBytes) => {
      const { invoke, Channel } = await core()
      // A fresh Channel per open: Tauri ends one whose Rust side was dropped.
      const ch = new Channel<{ wall_us: number; bytes: number[] }[]>()
      ch.onmessage = (batch) => onBytes(batch.map((p) => p.bytes))
      await invoke('midi_open', { port, onMessage: ch })
      return () => void invoke('midi_close', { port }).catch(() => {})
    },
  }
}

function webMidiTransport(): MidiTransport {
  // Chrome asks the user first. A refusal is not kept for good: after
  // REASK_MS the poll asks again, so allowing MIDI in the site settings takes
  // effect without a reload — but a dismissed prompt is not shown again at
  // once (every 1.5 s would nag, and Chrome blocks a site after repeats).
  const REASK_MS = 20_000
  let access: Promise<MIDIAccess> | null = null
  let refused: { at: number; error: unknown } | null = null
  const get = () => {
    if (refused && performance.now() - refused.at < REASK_MS) return Promise.reject(refused.error)
    return (access ??= navigator.requestMIDIAccess({ sysex: false }).then(
      (a) => ((refused = null), a),
      (e: unknown) => {
        access = null
        refused = { at: performance.now(), error: e }
        throw e
      },
    ))
  }
  const inputs = async () => {
    const list = [...(await get()).inputs.values()].filter((i) => i.state === 'connected')
    const names = uniqueNames(list.map((i) => i.name ?? 'MIDI input'))
    return list.map((input, k) => ({ input, name: names[k] }))
  }
  return {
    ports: async () => (await inputs()).map((i) => i.name),
    open: async (port, onBytes) => {
      const hit = (await inputs()).find((i) => i.name === port)
      if (!hit) throw new Error(`MIDI port not found: ${port}`)
      const handler = (e: MIDIMessageEvent) => {
        // System messages stay out, as the shell ignores them.
        if (e.data && e.data[0] < 0xf0) onBytes([e.data])
      }
      hit.input.addEventListener('midimessage', handler)
      // Only SETTING `onmidimessage` opens a port implicitly; a listener added
      // with addEventListener does not, so open it ourselves.
      await hit.input.open()
      return () => {
        hit.input.removeEventListener('midimessage', handler)
        void hit.input.close().catch(() => {})
      }
    },
  }
}

export function midiTransport(): MidiTransport | null {
  if (typeof window === 'undefined') return null
  if ('__TAURI_INTERNALS__' in window) return tauriTransport()
  if (typeof navigator.requestMIDIAccess === 'function') return webMidiTransport()
  return null
}
