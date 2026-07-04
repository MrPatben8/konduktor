// Web Audio playback for the prep deck. Plays the already-decoded AudioBuffer
// through an AudioBufferSourceNode, which gives sample-accurate, gapless loops
// via native loopStart/loopEnd — something an <audio> element can't do.
//
// A source node is one-shot, so play/seek recreate it; position is derived from
// the AudioContext clock (high-resolution, so the playhead is smooth without any
// wall-clock interpolation). Loop bounds can be changed on the live node, so
// enabling a loop mid-playback is seamless.

export class PlaybackEngine {
  private ctx: AudioContext | null = null
  private buffer: AudioBuffer | null = null
  private gain: GainNode | null = null
  private source: AudioBufferSourceNode | null = null
  private startedAt = 0 // ctx time when the current source started
  private startOffset = 0 // buffer position (s) at that moment / paused position
  private _playing = false
  private loopOn = false
  private loopStart = 0
  private loopEnd = 0
  private onEnded: (() => void) | null = null

  load(ctx: AudioContext, buffer: AudioBuffer): void {
    this.ctx = ctx
    this.buffer = buffer
    if (!this.gain) {
      this.gain = ctx.createGain()
      this.gain.connect(ctx.destination)
    }
    this.stopSource()
    this._playing = false
    this.startOffset = 0
    this.loopOn = false
  }

  setOnEnded(cb: () => void): void {
    this.onEnded = cb
  }

  get ready(): boolean {
    return !!this.buffer
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
    let pos = this.startOffset + (this.ctx.currentTime - this.startedAt)
    if (this.loopOn && this.loopEnd > this.loopStart && pos >= this.loopEnd) {
      const len = this.loopEnd - this.loopStart
      pos = this.loopStart + ((pos - this.loopStart) % len)
    }
    return Math.min(pos, this.buffer.duration)
  }

  private startSource(offset: number): void {
    if (!this.ctx || !this.buffer || !this.gain) return
    const src = this.ctx.createBufferSource()
    src.buffer = this.buffer
    if (this.loopOn && this.loopEnd > this.loopStart) {
      src.loop = true
      src.loopStart = this.loopStart
      src.loopEnd = this.loopEnd
    }
    src.connect(this.gain)
    src.onended = () => {
      if (this.source === src) {
        // Natural end (our own stops null out onended first).
        this._playing = false
        this.source = null
        this.onEnded?.()
      }
    }
    const off = Math.max(0, Math.min(offset, this.buffer.duration - 0.001))
    src.start(0, off)
    this.source = src
    this.startedAt = this.ctx.currentTime
    this.startOffset = offset
    this._playing = true
  }

  private stopSource(): void {
    if (this.source) {
      this.source.onended = null
      try {
        this.source.stop()
      } catch {
        /* already stopped */
      }
      this.source.disconnect()
      this.source = null
    }
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
    if (this.source) {
      if (enabled && end > start) {
        this.source.loop = true
        this.source.loopStart = start
        this.source.loopEnd = end
      } else {
        this.source.loop = false
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
