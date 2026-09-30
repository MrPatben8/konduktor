"""Write a native-instruments STEM file (`.stem.m4a`) from a source track.

The file layout is measured, not guessed, and matches the user's commercial
stem files (see the stem-conversion discussion log): an `ipod` MP4 with FIVE
AAC-LC stereo 44.1 kHz streams — stream 0 the full mix and 1-4 drums / bass /
synths / vox — streams 1-4 not enabled (`disposition` 0, so a player that knows
nothing of stems plays the mix), and a `moov/udta/stem` box holding JSON that
names and colours the stems. Traktor 4.5 loads such a file as stems and honours
the MP4 edit list, so a stem file's time base is the decoded audio's own.

Platform-independent on purpose: the separation itself is a callback
(`separate`) so this module never imports torch — the stem ENGINE supplies it
in production, and the tests use a fake one. Konduktor's backend owns decode,
encode, mux, tags and verification, where they are testable on any machine.

Things that were measured and are easy to get wrong:
  * **Mono sources are duplicated by hand.** FFmpeg's resampler upmixes mono to
    stereo at -3 dB (0.5 -> 0.354), which would make a converted mono track
    quieter than its source.
  * **Resampling does not move time**: a 48 kHz impulse at 1.000 s lands on
    sample 44100 exactly. Tested, because a shift here would move every cue.
  * **Both encoders are sample-exact on timing** (`aac` primes 1024 samples,
    Apple's `aac_at` 2112, both recorded in the edit list), but each decodes a
    little LONGER than the input — the last frame's padding (up to one frame for
    `aac`, ~2 for `aac_at`). So the length check allows 0..2048 extra samples,
    and the duration reported to the collection is the SOURCE's.
  * **Two JSON serialisations, one object.** The file's `stem` box is written
    the way the commercial files write it (Python's default `json.dumps`: key
    order kept, `4.0`, 17-digit floats) and Traktor's `<STEMS>` element the way
    Traktor writes it (sorted keys, `4`, 16 digits — the one value on all 3,678
    stem entries of the real collection). Neither can be derived from the other
    byte for byte — Traktor's 16 digits do not even round-trip the same double —
    so both are literals, and a test proves they hold the same float32 values
    (the precision NI stores them at: 0.02f, 0.14f, 0.05f).
"""
from __future__ import annotations

import fractions
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from . import mp4_edit, tag_copy

SR = 44100
FRAME = 1024  # AAC-LC frame size, for both encoders
#: The stem slots, in file order after the mix. Demucs names its outputs
#: drums / bass / other / vocals; "other" is what NI calls Synths.
STEM_NAMES = ("Drums", "Bass", "Synths", "Vox")
DEMUCS_ORDER = ("drums", "bass", "other", "vocals")

#: The `moov/udta/stem` box payload, byte for byte as the commercial files have it.
STEM_BOX_JSON = (
    b'{"mastering_dsp": {"compressor": {"ratio": 4.0, "output_gain": 0.0, "enabled": false, '
    b'"threshold": 0.0, "attack": 0.019999999552965164, "input_gain": 0.0, "release": '
    b'0.14000000059604645, "hp_cutoff": 50, "dry_wet": 50}, "limiter": {"release": '
    b'0.05000000074505806, "threshold": 0.0, "ceiling": 0.0, "enabled": false}}, "version": 1, '
    b'"stems": [{"color": "#FD4A4A", "name": "Drums"}, {"color": "#FFFF00", "name": "Bass"}, '
    b'{"color": "#00E8E8", "name": "Synths"}, {"color": "#AD65FF", "name": "Vox"}]}'
)
#: Traktor's `<ENTRY><STEMS STEMS="…">` value for the same object, as Traktor writes it.
NML_STEMS_JSON = (
    '{"mastering_dsp":{"compressor":{"attack":0.01999999955296516,"dry_wet":50,"enabled":false,'
    '"hp_cutoff":50,"input_gain":0,"output_gain":0,"ratio":4,"release":0.1400000005960464,'
    '"threshold":0},"limiter":{"ceiling":0,"enabled":false,"release":0.05000000074505806,'
    '"threshold":0}},"stems":[{"color":"#FD4A4A","name":"Drums"},{"color":"#FFFF00","name":"Bass"},'
    '{"color":"#00E8E8","name":"Synths"},{"color":"#AD65FF","name":"Vox"}],"version":1}'
)

#: Codecs whose source counts as lossless for the bitrate rule.
_LOSSLESS = ("flac", "alac", "wavpack", "tta", "ape", "truehd", "mlp")
#: Output bitrate: sources below 256 kbps get 256 (AAC below that is audible on a
#: soloed stem), everything else — and every lossless source — 320.
_FLOOR, _TOP = 256_000, 320_000
#: Decoded length may exceed the source by the encoders' final-frame padding.
_LENGTH_SLACK = 2 * FRAME


class StemFileError(Exception):
    """The file could not be written, or failed verification. User-facing."""


@dataclass(frozen=True)
class Source:
    """A decoded source: stereo float32 at 44.1 kHz, plus what the bitrate rule
    and the collection need to know about the original."""

    pcm: np.ndarray  # shape (2, n), float32 — NOT clamped (masters exceed 0 dBFS)
    codec: str
    bit_rate: int | None  # bps, as the container/stream reports it

    @property
    def samples(self) -> int:
        return int(self.pcm.shape[1])

    @property
    def lossless(self) -> bool:
        return self.codec.startswith("pcm_") or self.codec in _LOSSLESS


@dataclass(frozen=True)
class Written:
    """What a finished stem file is, for the collection entry that points at it."""

    path: Path
    bit_rate: int  # bps per stream, as encoded
    duration: float  # seconds, the SOURCE's (see the module notes)
    size: int  # bytes
    tags: dict


def default_encoder() -> str:
    """Apple's AudioToolbox encoder on macOS (the better AAC encoder there),
    FFmpeg's own elsewhere. Both are timing-exact in Traktor (kit 1)."""
    import av

    return "aac_at" if sys.platform == "darwin" and "aac_at" in av.codecs_available else "aac"


def choose_bitrate(source: Source) -> int:
    if source.lossless or not source.bit_rate:
        return _TOP
    return _FLOOR if source.bit_rate < _FLOOR else _TOP


def decode(path: Path | str) -> Source:
    """Decode the first audio stream to stereo float32 at 44.1 kHz."""
    import av

    with av.open(str(path), metadata_errors="ignore") as container:
        stream = container.streams.audio[0]
        codec = stream.codec_context.name
        bit_rate = stream.bit_rate or container.bit_rate or None
        channels = stream.codec_context.channels
        # Mono stays mono through the resampler and is duplicated below (see the
        # module notes); anything wider is downmixed by it.
        layout = "mono" if channels == 1 else "stereo"
        resampler = av.AudioResampler(format="fltp", layout=layout, rate=SR)
        chunks = [f.to_ndarray() for frame in container.decode(stream) for f in resampler.resample(frame)]
        chunks += [f.to_ndarray() for f in resampler.resample(None)]
    if not chunks:
        raise StemFileError("The file has no audio to convert")
    pcm = np.concatenate(chunks, axis=1).astype(np.float32, copy=False)
    if pcm.shape[0] == 1:
        pcm = np.repeat(pcm, 2, axis=0)
    return Source(pcm=np.ascontiguousarray(pcm), codec=codec, bit_rate=bit_rate)


def _encode(fp, tracks: list[np.ndarray], *, bit_rate: int, encoder: str,
            checkpoint: Callable[[], None] | None) -> None:
    """Mux five AAC streams IN LOCKSTEP — one frame of each in turn — so the
    muxer never buffers a whole stream (which it would if they came one after
    another)."""
    import av

    n = tracks[0].shape[1]
    tb = fractions.Fraction(1, SR)
    container = av.open(fp, "w", format="ipod")
    try:
        streams = []
        for k in range(len(tracks)):
            s = container.add_stream(encoder, rate=SR, layout="stereo")
            s.bit_rate = bit_rate
            if k:
                s.disposition = 0  # present, not enabled: a plain player plays the mix
            streams.append(s)
        for i, offset in enumerate(range(0, n, FRAME)):
            if checkpoint is not None and i % 512 == 0:
                checkpoint()
            for s, x in zip(streams, tracks):
                frame = av.AudioFrame.from_ndarray(
                    np.ascontiguousarray(x[:, offset:offset + FRAME], dtype=np.float32),
                    format="fltp", layout="stereo",
                )
                frame.sample_rate, frame.pts, frame.time_base = SR, offset, tb
                for packet in s.encode(frame):
                    container.mux(packet)
        for s in streams:
            for packet in s.encode(None):
                container.mux(packet)
    finally:
        container.close()


def _find(f, start: int, end: int, kind: bytes):
    for k, body, box_end in mp4_edit._boxes(f, start, end):
        if k == kind:
            return body - 8, body, box_end  # header offset, payload, end
    return None


def add_stem_box(path: Path | str) -> None:
    """Append the `stem` box to `moov/udta` in place.

    Only valid when `moov` is the file's LAST box, which it is for what `_encode`
    writes (FFmpeg's default): then growing it moves no media data, so no chunk
    offset changes and only the sizes of `moov` and `udta` need patching. The
    box header is always 32-bit here (`moov` is far below 4 GiB).
    """
    path = Path(path)
    size = path.stat().st_size
    box = struct.pack(">I4s", 8 + len(STEM_BOX_JSON), b"stem") + STEM_BOX_JSON
    with open(path, "r+b") as f:
        moov = _find(f, 0, size, b"moov")
        if moov is None or moov[2] != size:
            raise StemFileError("Unexpected MP4 layout: the index is not at the end of the file")
        moov_at, moov_body, _ = moov
        f.seek(moov_at)
        data = bytearray(f.read(size - moov_at))
        udta = _find(f, moov_body, size, b"udta")
        if udta is not None:
            u_at, _u_body, u_end = (x - moov_at for x in udta)
            data[u_end:u_end] = box
            struct.pack_into(">I", data, u_at, u_end - u_at + len(box))
            grow = len(box)
        else:
            wrapped = struct.pack(">I4s", 8 + len(box), b"udta") + box
            data += wrapped
            grow = len(wrapped)
        struct.pack_into(">I", data, 0, struct.unpack_from(">I", data, 0)[0] + grow)
        f.seek(moov_at)
        f.write(data)


def read_stem_box(path: Path | str) -> bytes | None:
    """The `moov/udta/stem` payload, or None."""
    path = Path(path)
    size = path.stat().st_size
    with open(path, "rb") as f:
        moov = _find(f, 0, size, b"moov")
        if moov is None:
            return None
        udta = _find(f, moov[1], moov[2], b"udta")
        if udta is None:
            return None
        stem = _find(f, udta[1], udta[2], b"stem")
        if stem is None:
            return None
        f.seek(stem[1])
        return f.read(stem[2] - stem[1])


def _aligned(decoded: np.ndarray, source: np.ndarray, window: int = 10 * SR, search: int = 64) -> int:
    """Lag (samples) of `decoded` against `source` in the loudest `window`,
    searched within +-`search`. 0 means the file starts where the source does."""
    mono_s = source.mean(axis=0)
    mono_d = decoded.mean(axis=0)
    n = min(len(mono_s), len(mono_d))
    if n <= 2 * search:
        return 0
    block = SR
    energy = [float(np.square(mono_s[i:i + block]).sum()) for i in range(0, max(1, n - window), block)]
    start = int(np.argmax(energy)) * block if energy else 0
    stop = min(n - search, start + window)
    start = max(search, start)
    ref = mono_s[start:stop]
    best, best_lag = -np.inf, 0
    for lag in range(-search, search + 1):
        seg = mono_d[start + lag:stop + lag]
        if len(seg) != len(ref):
            continue
        score = float(np.dot(ref, seg))
        if score > best:
            best, best_lag = score, lag
    return best_lag


def verify(path: Path | str, source: Source) -> None:
    """Refuse a file that is not the stem file it should be: five stereo
    44.1 kHz streams, only the first enabled, the stem box intact (mutagen
    rewrote the tags after it), the mix the right length and — by
    cross-correlation over its loudest ten seconds — not shifted by a sample."""
    import av

    with av.open(str(path)) as container:
        streams = container.streams.audio
        if len(streams) != 5:
            raise StemFileError(f"Expected 5 audio streams, found {len(streams)}")
        for s in streams:
            if s.codec_context.channels != 2 or s.codec_context.sample_rate != SR:
                raise StemFileError("A stream is not stereo 44.1 kHz")
        if int(streams[0].disposition) == 0 or any(int(s.disposition) for s in streams[1:]):
            raise StemFileError("Only the mix stream may be enabled")
        chunks = [f.to_ndarray() for f in container.decode(streams[0])]
    mix = np.concatenate(chunks, axis=1) if chunks else np.zeros((2, 0), np.float32)
    extra = mix.shape[1] - source.samples
    if not 0 <= extra <= _LENGTH_SLACK:
        raise StemFileError(f"The mix is {extra:+d} samples off the source's length")
    if read_stem_box(path) != STEM_BOX_JSON:
        raise StemFileError("The stem description box is missing or damaged")
    lag = _aligned(mix, source.pcm)
    if abs(lag) > 1:
        raise StemFileError(f"The mix is shifted {lag:+d} samples against the source")


Separate = Callable[[np.ndarray], list[np.ndarray]]


def build(source_path: Path | str, target: Path | str, separate: Separate, *,
          encoder: str | None = None, checkpoint: Callable[[], None] | None = None) -> Written:
    """Convert `source_path` into a stem file at `target`.

    `separate(pcm)` gets the stereo 44.1 kHz mix and must return the four stems
    in `DEMUCS_ORDER`, each the mix's shape. `target` may be any name — the batch
    writes a `*.konduktor-partial` and renames it once the batch commits — since
    nothing here infers a format from the suffix. `checkpoint()` is called between
    phases and during the encode, and may raise to cancel.
    """
    target = Path(target)
    tick = checkpoint or (lambda: None)
    source = decode(source_path)
    tick()
    stems = separate(source.pcm)
    tick()
    if len(stems) != 4 or any(s.shape != source.pcm.shape for s in stems):
        raise StemFileError("The separation did not return four stems of the mix's length")
    bit_rate = choose_bitrate(source)
    with open(target, "wb") as fp:
        _encode(fp, [source.pcm, *stems], bit_rate=bit_rate,
                encoder=encoder or default_encoder(), checkpoint=checkpoint)
    add_stem_box(target)
    tags = tag_copy.copy_tags(source_path, target)
    verify(target, source)
    return Written(path=target, bit_rate=bit_rate, duration=source.samples / SR,
                   size=target.stat().st_size, tags=tags)


def stem_target_name(source_name: str) -> str:
    """`Track.mp3` -> `Track.stem.m4a` (Traktor needs the `.stem.m4a` suffix)."""
    stem = source_name.rsplit(".", 1)[0] if "." in source_name else source_name
    return f"{stem}.stem.m4a"


def is_stem_file(path: Path | str) -> bool:
    """Whether a FILE is already a stem container — decided by its contents, not
    by `<STEMS>` in the collection (105 plain MP3s there carry one)."""
    path = Path(path)
    if path.suffix.lower() not in (".m4a", ".mp4"):
        return False
    try:
        return read_stem_box(path) is not None
    except OSError:
        return False


def stem_layout(path: Path | str) -> list[dict] | None:
    """The stems a file carries, for PLAYBACK: `[{"name", "color"}]` in stream
    order (stream k+1 is stem k), or None when the file is not a playable stem
    file — no `stem` box, a box that does not parse, or fewer audio streams than
    it names. Read from the FILE, never from the library's `<STEMS>`: 105 plain
    MP3 entries in the real collection carry that element."""
    import json

    import av

    path = Path(path)
    try:
        raw = read_stem_box(path)
        if raw is None:
            return None
        stems = json.loads(raw).get("stems") or []
        with av.open(str(path), metadata_errors="ignore") as c:
            streams = len(c.streams.audio)
    except (OSError, ValueError, av.FFmpegError):
        return None
    if not stems or streams < 1 + len(stems):
        return None
    return [{"name": str(s.get("name") or f"Stem {i + 1}"), "color": str(s.get("color") or "#FFFFFF")}
            for i, s in enumerate(stems)]


def extract_stream(path: Path | str, stream: int, fp) -> None:
    """Copy audio stream `stream` (0 = the mix, 1-4 the stems) into its own MP4,
    written to the file object `fp` — packets COPIED, never re-encoded.

    A browser decodes only an MP4's first audio stream, so this is how the deck
    gets at the stems. Measured bit-identical to decoding the stream in place,
    with the edit list (and so the priming: 1024 for FFmpeg's encoder, 2112 for
    Apple's) carried across — on Konduktor's files and commercial ones alike, so
    every stem stays sample-locked to the mix and to the cue timeline.
    """
    import av

    with av.open(str(path), metadata_errors="ignore") as inp:
        if not 0 <= stream < len(inp.streams.audio):
            raise StemFileError(f"The file has no audio stream {stream}")
        ist = inp.streams.audio[stream]
        out = av.open(fp, "w", format="ipod")
        try:
            ost = out.add_stream_from_template(ist)
            for pkt in inp.demux(ist):
                if pkt.dts is None:
                    continue  # the demuxer's end-of-stream flush packet
                pkt.stream = ost
                out.mux(pkt)
        finally:
            out.close()


__all__ = [
    "DEMUCS_ORDER", "NML_STEMS_JSON", "SR", "STEM_BOX_JSON", "STEM_NAMES", "Source",
    "StemFileError", "Written", "add_stem_box", "build", "choose_bitrate", "decode",
    "default_encoder", "extract_stream", "is_stem_file", "read_stem_box", "stem_layout", "stem_target_name", "verify",
]
