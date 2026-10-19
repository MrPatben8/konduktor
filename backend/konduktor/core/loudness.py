"""Loudness measurement: integrated LUFS, RMS, true peak, LRA and PLR.

The Mastering columns. Each value is a property of the AUDIO, measured once per
file and kept in Konduktor's own store (`konduktor/loudness_store.py`) — never
in the library and never in file tags (decided 2026-10-19, see
`.claude/discussions/discuss-loudness-analysis-2026-10-19.md`).

- **Integrated loudness** (LUFS): ITU-R BS.1770-4 / EBU R128 — K-weighting,
  400 ms blocks every 100 ms, the absolute gate at -70 LUFS and the relative
  one 10 LU below the absolute-gated mean.
- **LRA** (LU): EBU Tech 3342 — 3 s short-term loudness every 100 ms, gated at
  -70 LUFS and 20 LU below their mean, then P95 - P10.
- **True peak** (dBTP): BS.1770 Annex 2 — the signal oversampled (4x below
  96 kHz, 2x below 192 kHz), the largest magnitude over every channel.
- **PLR** (dB): true peak minus integrated loudness. Low = a crushed master.
- **RMS** (dB): the DR meter's convention (foobar2000 / TT DR / FFmpeg
  `drmeter`) — power in non-overlapping 3 s blocks, channels averaged, the mean
  of the LOUDEST 20% of blocks. Unweighted, so beside LUFS it shows bass
  weight; over the loud blocks only, so a long intro does not pull it down
  ("how hot does the drop hit"). AES17 sine-referenced: a full-scale sine
  reads 0 dB, a full-scale square +3 dB (square-referenced meters read 3 dB
  lower — the usual source of disagreeing RMS figures).

Everything is computed from per-100 ms sums of the K-weighted and of the raw
signal's power, so a track streams through in chunks: memory stays at a few
seconds of audio however long the file. The decode is the file's OWN rate and
channels — not the analysis decode (`waveform.load`, mono 22.05 kHz), which
BS.1770 cannot use (it sums channel POWERS) and which would hide true peaks.

`test_loudness.py` checks it against FFmpeg's `ebur128` filter (bundled in
PyAV's wheel) on generated signals.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

#: Bump when a measurement changes: stored values from an older version count
#: as stale and are offered for re-analysis.
VERSION = 2  # 2: a stem file is its four stems summed, not its mix stream

_ABS_GATE = -70.0          # LUFS
_REL_GATE = -10.0          # LU, integrated
_LRA_REL_GATE = -20.0      # LU, loudness range
_RMS_TOP = 0.2             # the loudest fifth of the 3 s blocks
_AES17 = 10.0 * math.log10(2.0)   # +3.01 dB: a sine's mean square is half its peak squared
_TP_PAD = 32               # samples of context either side of an oversampled chunk


@dataclass(frozen=True)
class Loudness:
    lufs: float
    rms: float
    true_peak: float
    lra: float
    plr: float

    def as_dict(self) -> dict[str, float]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Loudness":
        return cls(**{k: float(d[k]) for k in ("lufs", "rms", "true_peak", "lra", "plr")})


def _k_weighting(sr: int) -> np.ndarray:
    """BS.1770's two K-weighting biquads at any rate, as second-order sections.
    The analogue prototype's constants are libebur128's (and FFmpeg's), which
    reproduce the standard's 48 kHz coefficients exactly."""
    # Stage 1: the high shelf (+4 dB above ~1.7 kHz) modelling the head.
    f0, gain, q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    k = math.tan(math.pi * f0 / sr)
    vh = 10.0 ** (gain / 20.0)
    vb = vh ** 0.4996667741545416
    a0 = 1.0 + k / q + k * k
    shelf = [(vh + vb * k / q + k * k) / a0, 2.0 * (k * k - vh) / a0, (vh - vb * k / q + k * k) / a0,
             1.0, 2.0 * (k * k - 1.0) / a0, (1.0 - k / q + k * k) / a0]
    # Stage 2: the RLB high-pass (~38 Hz).
    f0, q = 38.13547087602444, 0.5003270373238773
    k = math.tan(math.pi * f0 / sr)
    a0 = 1.0 + k / q + k * k
    highpass = [1.0, -2.0, 1.0, 1.0, 2.0 * (k * k - 1.0) / a0, (1.0 - k / q + k * k) / a0]
    return np.array([shelf, highpass])


class _Meter:
    """Consumes `(channels, n)` float blocks in order; `result()` measures."""

    def __init__(self, sr: int, channels: int):
        from scipy import signal

        self._signal = signal
        self.sr = sr
        self.seg = max(1, round(sr / 10))                    # 100 ms
        self._sos = _k_weighting(sr)
        self._zi = np.zeros((self._sos.shape[0], channels, 2))
        self._pending = np.zeros((channels, 0), dtype=np.float32)
        self._k_sums: list[np.ndarray] = []                  # (channels, segs) K-weighted energy
        self._raw_sums: list[np.ndarray] = []                # (channels, segs) raw energy
        self._up = 4 if sr < 96000 else 2 if sr < 192000 else 1
        self._tp_tail = np.zeros((channels, _TP_PAD), dtype=np.float32)
        self._peak = 0.0

    def feed(self, block: np.ndarray) -> None:
        self._true_peak(block)
        self._pending = np.concatenate([self._pending, block.astype(np.float32, copy=False)], axis=1)
        whole = (self._pending.shape[1] // self.seg) * self.seg
        if whole:
            self._segments(self._pending[:, :whole])
            self._pending = self._pending[:, whole:]

    def _segments(self, x: np.ndarray) -> None:
        ch = x.shape[0]
        y, self._zi = self._signal.sosfilt(self._sos, x, axis=1, zi=self._zi)
        n = x.shape[1] // self.seg
        self._k_sums.append(np.square(y, dtype=np.float64).reshape(ch, n, self.seg).sum(axis=2))
        self._raw_sums.append(np.square(x, dtype=np.float64).reshape(ch, n, self.seg).sum(axis=2))

    def _true_peak(self, block: np.ndarray, final: bool = False) -> None:
        """The oversampled peak of every sample whose context has arrived: the
        last `_TP_PAD` samples wait for the next block (or the end)."""
        buf = np.concatenate([self._tp_tail, block.astype(np.float32, copy=False)], axis=1)
        if final:
            buf = np.concatenate([buf, np.zeros((buf.shape[0], _TP_PAD), dtype=np.float32)], axis=1)
        valid = buf.shape[1] - 2 * _TP_PAD
        if valid <= 0:
            self._tp_tail = buf if not final else self._tp_tail
            return
        if self._up > 1:
            over = self._signal.resample_poly(buf, self._up, 1, axis=1)
            region = over[:, _TP_PAD * self._up:(_TP_PAD + valid) * self._up]
        else:
            region = buf[:, _TP_PAD:_TP_PAD + valid]
        sample = buf[:, _TP_PAD:_TP_PAD + valid]
        self._peak = max(self._peak, float(np.max(np.abs(region))), float(np.max(np.abs(sample))))
        self._tp_tail = buf[:, -2 * _TP_PAD:]

    def result(self) -> Loudness | None:
        self._true_peak(np.zeros((self._tp_tail.shape[0], 0), dtype=np.float32), final=True)
        if not self._k_sums:
            return None
        k = np.concatenate(self._k_sums, axis=1).sum(axis=0)     # channel powers summed (G = 1)
        raw = np.concatenate(self._raw_sums, axis=1).mean(axis=0)
        segs = k.shape[0]
        if segs < 30:                                            # under 3 s: no short-term block
            return None
        lufs = _integrated(k, self.seg)
        if lufs is None:
            return None
        lra = _loudness_range(k, self.seg)
        rms = _rms(raw, self.seg)
        if self._peak <= 0.0 or rms is None:
            return None
        true_peak = 20.0 * math.log10(self._peak)
        return Loudness(lufs=lufs, rms=rms, true_peak=true_peak, lra=lra, plr=true_peak - lufs)


def _windows(segment_sums: np.ndarray, width: int, seg: int) -> np.ndarray:
    """Mean square of every `width`-segment window, one window per segment."""
    c = np.concatenate([[0.0], np.cumsum(segment_sums)])
    return (c[width:] - c[:-width]) / (width * seg)


def _lufs(z: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore"):
        return -0.691 + 10.0 * np.log10(z)


def _integrated(k: np.ndarray, seg: int) -> float | None:
    z = _windows(k, 4, seg)                                      # 400 ms blocks, 100 ms apart
    z = z[_lufs(z) > _ABS_GATE]
    if not z.size:
        return None
    threshold = _lufs(np.array([z.mean()]))[0] + _REL_GATE
    z = z[_lufs(z) > threshold]
    return float(_lufs(np.array([z.mean()]))[0]) if z.size else None


def _loudness_range(k: np.ndarray, seg: int) -> float:
    z = _windows(k, 30, seg)                                     # 3 s short-term, 100 ms apart
    z = z[_lufs(z) > _ABS_GATE]
    if not z.size:
        return 0.0
    threshold = _lufs(np.array([z.mean()]))[0] + _LRA_REL_GATE
    levels = _lufs(z)
    levels = levels[levels > threshold]
    if not levels.size:
        return 0.0
    lo, hi = np.percentile(levels, [10, 95])
    return float(hi - lo)


def _rms(raw: np.ndarray, seg: int) -> float | None:
    blocks = raw.shape[0] // 30                                  # non-overlapping 3 s blocks
    if not blocks:
        return None
    power = raw[:blocks * 30].reshape(blocks, 30).sum(axis=1) / (30 * seg)
    top = np.sort(power)[::-1][:max(1, int(round(blocks * _RMS_TOP)))]
    mean = float(top.mean())
    return 10.0 * math.log10(mean) + _AES17 if mean > 0 else None


def measure(pcm: np.ndarray, sr: int) -> Loudness | None:
    """Measure `(channels, n)` (or mono `(n,)`) float audio at `sr`. None for
    silence or anything under 3 s."""
    pcm = np.atleast_2d(np.asarray(pcm, dtype=np.float32))
    meter = _Meter(sr, pcm.shape[0])
    step = meter.seg * 100
    for start in range(0, pcm.shape[1], step):
        meter.feed(pcm[:, start:start + step])
    return meter.result()


def measure_file(path: Path | str) -> Loudness | None:
    """Decode `path` at its own rate and channel count (more than two are
    downmixed to stereo; mono stays mono, as BS.1770 and FFmpeg treat it) and
    measure it as it streams. Raises if the file cannot be decoded.

    A stem FILE is measured as its four stems SUMMED — what a stem deck plays
    (Traktor's and Konduktor's), and what Traktor's own gain analysis measures
    (decided 2026-10-19 after `bench_traktor_gain.py`: commercial stem files'
    mix streams are mastered ~2.8 dB, up to 5.3, hotter than their stems sum
    to; see the gain-normalisation discussion)."""
    from . import stem_file

    if stem_file.is_stem_file(path):
        return _measure_stem_sum(path)
    return _measure_stream(path)


def _measure_stream(path: Path | str) -> Loudness | None:
    import av

    from . import pyav

    with pyav.open_input(path) as container:
        stream = container.streams.audio[0]
        sr = int(stream.codec_context.sample_rate)
        channels = 1 if stream.codec_context.channels == 1 else 2
        resampler = av.AudioResampler(format="fltp", layout="mono" if channels == 1 else "stereo", rate=sr)
        meter = _Meter(sr, channels)
        pending: list[np.ndarray] = []
        size = 0

        def flush() -> None:
            nonlocal pending, size
            if pending:
                meter.feed(np.concatenate(pending, axis=1))
                pending, size = [], 0

        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                pending.append(out.to_ndarray())
                size += out.samples
            if size >= sr * 5:                                   # ~5 s per feed
                flush()
        for out in resampler.resample(None):
            pending.append(out.to_ndarray())
        flush()
    return meter.result()


def _measure_stem_sum(path: Path | str) -> Loudness | None:
    """Streams 1-4 of a stem file, decoded in ONE demux pass and summed as they
    arrive, so memory stays at seconds of audio. The stems share their encoder
    priming (they were muxed in lockstep), so sample n of each is the same
    instant; the sum ends where the shortest stem does."""
    import av

    from . import pyav

    with pyav.open_input(path) as container:
        stems = list(container.streams.audio[1:5])
        if len(stems) < 4:
            raise ValueError(f"{path}: a stem file with {len(stems)} stems")
        sr = int(stems[0].codec_context.sample_rate)
        resamplers = {s.index: av.AudioResampler(format="fltp", layout="stereo", rate=sr) for s in stems}
        queues: dict[int, list[np.ndarray]] = {s.index: [] for s in stems}
        meter = _Meter(sr, 2)

        def drain(final: bool = False) -> None:
            ready = min(sum(a.shape[1] for a in q) for q in queues.values())
            if ready == 0 or (not final and ready < sr * 5):          # ~5 s per feed
                return
            total = np.zeros((2, ready), dtype=np.float32)
            for idx, q in queues.items():
                buf = np.concatenate(q, axis=1)
                total += buf[:, :ready]
                queues[idx] = [buf[:, ready:]] if buf.shape[1] > ready else []
            meter.feed(total)

        for packet in container.demux(stems):
            idx = packet.stream.index
            for frame in packet.decode():
                for out in resamplers[idx].resample(frame):
                    queues[idx].append(out.to_ndarray())
            drain()
        for idx, rs in resamplers.items():
            for out in rs.resample(None):
                queues[idx].append(out.to_ndarray())
        drain(final=True)
    return meter.result()
