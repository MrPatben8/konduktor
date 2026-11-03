// MIDI messages as the mapping layer sees them: notes, control changes and
// pitch bend, each from a SOURCE — what one physical control sends on.
// Everything else a controller may send (aftertouch, program change) is
// ignored; system messages never leave the shell (src-tauri/src/midi.rs).

export type MidiKind = 'note' | 'cc' | 'pb'

export interface MidiMessage {
  kind: MidiKind
  /** 1-16, as controllers' manuals number them. */
  channel: number
  /** Note or controller number; 0 for pitch bend. */
  number: number
  /** 0-127; pitch bend 0-16383. A note-off (or note-on at velocity 0) is 0. */
  value: number
}

/** One physical control: `note:1:11`, `cc:2:33`, `pb:1`. */
export type SourceKey = string

export function parseMessage(bytes: ArrayLike<number>): MidiMessage | null {
  if (bytes.length < 2) return null
  const type = bytes[0] & 0xf0
  const channel = (bytes[0] & 0x0f) + 1
  const d1 = bytes[1] & 0x7f
  const d2 = bytes.length > 2 ? bytes[2] & 0x7f : 0
  switch (type) {
    case 0x80:
      return { kind: 'note', channel, number: d1, value: 0 }
    case 0x90:
      return { kind: 'note', channel, number: d1, value: d2 }
    case 0xb0:
      return { kind: 'cc', channel, number: d1, value: d2 }
    case 0xe0:
      return { kind: 'pb', channel, number: 0, value: d1 | (d2 << 7) }
    default:
      return null
  }
}

export function sourceKey(m: Pick<MidiMessage, 'kind' | 'channel' | 'number'>): SourceKey {
  return m.kind === 'pb' ? `pb:${m.channel}` : `${m.kind}:${m.channel}:${m.number}`
}

export function parseSource(key: SourceKey): Pick<MidiMessage, 'kind' | 'channel' | 'number'> | null {
  const [kind, ch, num] = key.split(':')
  const channel = Number(ch)
  if (!(channel >= 1 && channel <= 16)) return null
  if (kind === 'pb') return { kind, channel, number: 0 }
  const number = Number(num)
  if ((kind !== 'note' && kind !== 'cc') || !(number >= 0 && number <= 127)) return null
  return { kind, channel, number }
}

/** "Note 11 · Ch 1", "CC 33 · Ch 2", "Pitch bend · Ch 1". */
export function describeSource(key: SourceKey): string {
  const s = parseSource(key)
  if (!s) return key
  const what = s.kind === 'pb' ? 'Pitch bend' : s.kind === 'note' ? `Note ${s.number}` : `CC ${s.number}`
  return `${what} · Ch ${s.channel}`
}
