// Web Audio playback for the prep deck. Plays the already-decoded AudioBuffer
// through an AudioBufferSourceNode, which gives sample-accurate, gapless loops
// via native loopStart/loopEnd — something an <audio> element can't do.
//
// A source node is one-shot, so play/seek recreate it; position is derived from
// the AudioContext clock (high-resolution, so the playhead is smooth without any
// wall-clock interpolation). Loop bounds can be changed on the live node, so
// enabling a loop mid-playback is seamless.
//
// A STEM track loads four buffers instead of one. Each gets its own source and
// gain, and every source is started at the SAME scheduled context time, so the
// stems stay sample-locked through play, seek and loops — the deck plays their
// sum, as Traktor's stem deck does. Muting is a gain change (ramped over a few
// ms so it does not click), never a restart.

export class PlaybackEngine {
  private ctx: AudioContext | null = null
  private buffers: AudioBuffer[] = []
  private gain: GainNode | null = null
  /** One per buffer; a plain track has one of each. */
  private gains: GainNode[] = []
  private sources: AudioBufferSourceNode[] = []
  private startedAt = 0 // ctx time when the current source started
  private startOffset = 0 // buffer position (s) at that moment / paused position
  private _playing = false
  private loopOn = false
  private loopStart = 0
  private loopEnd = 0
  private onEnded: (() => void) | null = null

  /** One buffer (a plain track) or several played in lockstep (the stems). */
  load(ctx: AudioContext, buffer: AudioBuffer | AudioBuffer[]): void {
    this.ctx = ctx
    this.stopSource()
    this.buffers = Array.isArray(buffer) ? buffer : [buffer]
    if (!this.gain) {
      this.gain = ctx.createGain()
      this.gain.connect(ctx.destination)
    }
    for (const g of this.gains) g.disconnect()
    this.gains = this.buffers.map(() => {
      const g = ctx.createGain()
      g.connect(this.gain!)
      return g
    })
    this._playing = false
    this.startOffset = 0
    this.loopOn = false
  }

  setOnEnded(cb: () => void): void {
    this.onEnded = cb
  }

  private get buffer(): AudioBuffer | null {
    return this.buffers[0] ?? null
  }

  get ready(): boolean {
    return !!this.buffer
  }

  /** Per-buffer levels (0 = muted) — the stems' mute/solo. Ramped, not stepped. */
  setGains(levels: number[]): void {
    if (!this.ctx) return
    const now = this.ctx.currentTime
    this.gains.forEach((g, i) => {
      g.gain.cancelScheduledValues(now)
      g.gain.setTargetAtTime(levels[i] ?? 1, now, 0.004)
    })
  }
  get playing(): boolean {
    return this._playing
  }
  get duration(): number {
    return this.buffer?.duration ?? 0
  }
  get loopEnabled(): boolean {
    return this.loopOn
  }

  getPosition(): number {
    if (!this.buffer || !this.ctx || !this._playing) return this.startOffset
    // max(0, …): a scheduled start is a few ms in the future.
    let pos = this.startOffset + Math.max(0, this.ctx.currentTime - this.startedAt)
    if (this.loopOn && this.loopEnd > this.loopStart && pos >= this.loopEnd) {
      const len = this.loopEnd - this.loopStart
      pos = this.loopStart + ((pos - this.loopStart) % len)
    }
    return Math.min(pos, this.buffer.duration)
  }

  private startSource(offset: number): void {
    if (!this.ctx || !this.buffer || !this.gain) return
    const off = Math.max(0, Math.min(offset, this.buffer.duration - 0.001))
    // One shared start time, a hair ahead so every source can make it: that is
    // what keeps the stems sample-locked to each other.
    const when = this.ctx.currentTime + (this.buffers.length > 1 ? 0.01 : 0)
    const started = this.buffers.map((buf, i) => {
      const src = this.ctx!.createBufferSource()
      src.buffer = buf
      if (this.loopOn && this.loopEnd > this.loopStart) {
        src.loop = true
        src.loopStart = this.loopStart
        src.loopEnd = this.loopEnd
      }
      src.connect(this.gains[i] ?? this.gain!)
      src.start(when, off)
      return src
    })
    const first = started[0]
    first.onended = () => {
      if (this.sources[0] === first) {
        // Natural end (our own stops null out onended first).
        this._playing = false
        this.stopSource()
        this.onEnded?.()
      }
    }
    this.sources = started
    this.startedAt = when
    this.startOffset = offset
    this._playing = true
  }

  private stopSource(): void {
    for (const src of this.sources) {
      src.onended = null
      try {
        src.stop()
      } catch {
        /* already stopped */
      }
      src.disconnect()
    }
    this.sources = []
  }

  play(): void {
    if (!this.ctx || !this.buffer || this._playing) return
    void this.ctx.resume()
    let off = this.startOffset
    if (off >= this.buffer.duration - 0.01) off = 0 // restart from top if at the end
    this.startSource(off)
  }

  pause(): void {
    if (!this._playing) return
    this.startOffset = this.getPosition()
    this.stopSource()
    this._playing = false
  }

  seek(t: number): void {
    const clamped = Math.max(0, Math.min(t, this.duration))
    if (this._playing) {
      this.stopSource()
      this.startOffset = clamped
      this.startSource(clamped)
    } else {
      this.startOffset = clamped
    }
  }

  // Snapshot the true buffer position into (startOffset, startedAt) so the
  // position model stays correct after the loop params change. Must be called
  // BEFORE mutating loopOn/loopStart/loopEnd (it reads the current/old params).
  private reanchor(): void {
    if (this._playing && this.ctx) {
      this.startOffset = this.getPosition()
      this.startedAt = this.ctx.currentTime
    }
  }

  /** Set the loop region and whether it's active. Bounds apply live if playing. */
  setLoop(start: number, end: number, enabled: boolean): void {
    this.reanchor() // capture the real position under the OLD loop state first
    this.loopStart = start
    this.loopEnd = end
    this.loopOn = enabled
    for (const src of this.sources) {
      if (enabled && end > start) {
        src.loop = true
        src.loopStart = start
        src.loopEnd = end
      } else {
        src.loop = false
      }
    }
    // If the playhead is already at/past the loop end, jump into it (the live
    // node won't loop back on its own from beyond loopEnd).
    if (enabled && end > start && this.startOffset >= end) {
      this.seek(start)
    }
  }

  setLoopEnabled(enabled: boolean): void {
    this.setLoop(this.loopStart, this.loopEnd, enabled)
  }
}
