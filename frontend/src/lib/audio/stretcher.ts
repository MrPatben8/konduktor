// Rubber Band (keylock's time-stretcher) from inside the deck's audio worklet.
//
// `rubberband.wasm` is built by `frontend/rubberband/build.sh` (upstream 3.3.0
// + Konduktor's own glue). It is a standalone WASI module; the few imports it
// declares are stubbed here, because the library, built with NO_TIMING and
// NO_THREADING, only touches them for diagnostics.
//
// Every buffer passed to the library lives in the module's own memory, and that
// memory can GROW (a new stretcher allocates). Growth detaches every view on the
// old buffer, so views are re-made whenever `memory.buffer` is a new object.

interface RbExports {
  memory: WebAssembly.Memory
  _initialize?: () => void
  kd_malloc(size: number): number
  kd_free(ptr: number): void
  kd_new(sampleRate: number, channels: number, options: number, timeRatio: number, pitchScale: number): number
  kd_delete(s: number): void
  kd_reset(s: number): void
  kd_set_time_ratio(s: number, r: number): void
  kd_set_pitch_scale(s: number, p: number): void
  kd_set_max_process_size(s: number, n: number): void
  kd_get_preferred_start_pad(s: number): number
  kd_get_start_delay(s: number): number
  kd_get_samples_required(s: number): number
  kd_process(s: number, input: number, samples: number, final: number): void
  kd_available(s: number): number
  kd_retrieve(s: number, output: number, samples: number): number
}

/** RubberBandOptionProcessRealTime | ThreadingNever | EngineFaster (R2:
 *  measured to fit 8 stretchers on one render thread; R3 does not). */
const OPTIONS = 0x00000001 | 0x00010000

/** The largest block ever passed to process() or retrieve(). */
export const MAX_BLOCK = 512

export class RubberBand {
  readonly x: RbExports
  private heap: ArrayBuffer | null = null
  private f32: Float32Array = new Float32Array(0)
  private u32: Uint32Array = new Uint32Array(0)

  constructor(module: WebAssembly.Module) {
    let memory: WebAssembly.Memory | null = null
    const u32 = () => new Uint32Array(memory!.buffer)
    const ok = () => 0
    const imports = {
      env: { emscripten_notify_memory_growth: ok },
      wasi_snapshot_preview1: {
        // fd_write must report every byte written, or libc retries forever.
        fd_write: (_fd: number, iovs: number, n: number, written: number) => {
          const v = u32()
          let total = 0
          for (let i = 0; i < n; i++) total += v[(iovs >> 2) + i * 2 + 1]
          v[written >> 2] = total
          return 0
        },
        fd_read: ok,
        fd_close: ok,
        fd_seek: ok,
        clock_time_get: ok,
        environ_sizes_get: (count: number, size: number) => {
          const v = u32()
          v[count >> 2] = 0
          v[size >> 2] = 0
          return 0
        },
        environ_get: ok,
        proc_exit: ok,
      },
    }
    const inst = new WebAssembly.Instance(module, imports)
    this.x = inst.exports as unknown as RbExports
    memory = this.x.memory
    this.x._initialize?.()
  }

  /** Float32 view of the module's memory (re-made after a growth). */
  floats(): Float32Array {
    if (this.heap !== this.x.memory.buffer) this.views()
    return this.f32
  }

  words(): Uint32Array {
    if (this.heap !== this.x.memory.buffer) this.views()
    return this.u32
  }

  private views(): void {
    this.heap = this.x.memory.buffer
    this.f32 = new Float32Array(this.heap)
    this.u32 = new Uint32Array(this.heap)
  }
}

/**
 * One real-time stretcher of `channels` channels. `timeRatio` is output frames
 * per input frame; `pitchScale` corrects for a source decoded at another rate
 * than the context plays at (1 when they agree).
 */
export class Stretcher {
  readonly channels: number
  private readonly rb: RubberBand
  private readonly s: number
  private readonly inPtrs: number
  private readonly outPtrs: number
  private readonly inBufs: number[] = []
  private readonly outBufs: number[] = []

  constructor(rb: RubberBand, sampleRate: number, channels: number) {
    this.rb = rb
    this.channels = channels
    const x = rb.x
    this.s = x.kd_new(sampleRate, channels, OPTIONS, 1, 1)
    x.kd_set_max_process_size(this.s, MAX_BLOCK)
    this.inPtrs = x.kd_malloc(channels * 4)
    this.outPtrs = x.kd_malloc(channels * 4)
    for (let c = 0; c < channels; c++) {
      this.inBufs.push(x.kd_malloc(MAX_BLOCK * 4))
      this.outBufs.push(x.kd_malloc(MAX_BLOCK * 4))
    }
    const w = rb.words()
    for (let c = 0; c < channels; c++) {
      w[(this.inPtrs >> 2) + c] = this.inBufs[c]
      w[(this.outPtrs >> 2) + c] = this.outBufs[c]
    }
  }

  reset(): void {
    this.rb.x.kd_reset(this.s)
  }

  setRatios(timeRatio: number, pitchScale: number): void {
    this.rb.x.kd_set_time_ratio(this.s, timeRatio)
    this.rb.x.kd_set_pitch_scale(this.s, pitchScale)
  }

  get startPad(): number {
    return this.rb.x.kd_get_preferred_start_pad(this.s)
  }

  get startDelay(): number {
    return this.rb.x.kd_get_start_delay(this.s)
  }

  get required(): number {
    return this.rb.x.kd_get_samples_required(this.s)
  }

  get available(): number {
    return Math.max(0, this.rb.x.kd_available(this.s))
  }

  /** Feed `n` (<= MAX_BLOCK) frames, channel c from `input[c]`. */
  process(input: Float32Array[], n: number): void {
    const f = this.rb.floats()
    for (let c = 0; c < this.channels; c++) f.set(input[c].subarray(0, n), this.inBufs[c] >> 2)
    this.rb.x.kd_process(this.s, this.inPtrs, n, 0)
  }

  /** Retrieve up to `n` (<= MAX_BLOCK) frames into `out[c]`; returns how many. */
  retrieve(out: Float32Array[], n: number): number {
    const got = this.rb.x.kd_retrieve(this.s, this.outPtrs, n)
    const f = this.rb.floats()
    for (let c = 0; c < this.channels; c++) {
      const at = this.outBufs[c] >> 2
      out[c].set(f.subarray(at, at + got))
    }
    return got
  }

  dispose(): void {
    const x = this.rb.x
    for (const p of [...this.inBufs, ...this.outBufs, this.inPtrs, this.outPtrs]) x.kd_free(p)
    x.kd_delete(this.s)
  }
}
