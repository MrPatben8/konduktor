"""What an MP3's first frame says about where its audio starts.

A gapless MP3 opens with a HEADER FRAME — a valid MPEG frame whose payload is a
`Xing` (VBR) or `Info` (CBR) tag instead of audio — often followed by a LAME
extension recording the encoder delay. A gapless decoder (FFmpeg, libsndfile's
mpg123, CoreAudio) skips that frame and trims `encoder delay + 529` samples of
start padding; a naive one decodes the frame as silence and trims nothing. Where
a platform's clock sits relative to Konduktor's decoded time base therefore
depends on these facts, per file — which is why they are read here, once, rather
than assumed per format.

Format knowledge, not platform knowledge: this reads bytes and says what is
there. What a platform DOES with them lives in that platform's `timebase`.

Two things are easy to get wrong, and both were got wrong once:
  * the first frame comes AFTER the ID3v2 tag, which can be megabytes of cover
    art — a check that only looks at the start of the file sees no header;
  * the tag sits at a fixed offset after the side information, whose size
    depends on the MPEG version and channel mode.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

#: The mp3 decoder delay a gapless decoder trims on top of the encoder's.
DECODER_DELAY = 529
#: Encoder strings whose LAME-style delay field FFmpeg (and mpg123) honour.
_TRUSTED_ENCODERS = (b"LAME", b"Lavf", b"Lavc")
#: How far past the ID3 tag to look for the first frame (junk, padding).
_SCAN = 64 * 1024

_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


@dataclass(frozen=True)
class Mp3Start:
    sample_rate: int
    #: Samples in one frame: 1152 for MPEG-1 Layer III, 576 for MPEG-2/2.5.
    samples_per_frame: int
    #: The first frame is a Xing/Info/VBRI tag, not audio.
    header_frame: bool
    #: Encoder delay from a LAME-style tag a gapless decoder trusts, else None.
    encoder_delay: int | None

    @property
    def trimmed(self) -> int:
        """Samples of start padding a gapless decoder removes (0 without a
        trusted delay field: it has nothing to trim by)."""
        return 0 if self.encoder_delay is None else self.encoder_delay + DECODER_DELAY


def _id3_end(head: bytes) -> int:
    if head[:3] != b"ID3" or len(head) < 10:
        return 0
    size = ((head[6] & 127) << 21) | ((head[7] & 127) << 14) | ((head[8] & 127) << 7) | (head[9] & 127)
    footer = 10 if head[5] & 0x10 else 0
    return 10 + size + footer


def _frame_header(b: bytes, i: int):
    """(version bits, sample rate, mono) if a Layer III frame header starts at i."""
    if i + 4 > len(b) or b[i] != 0xFF or (b[i + 1] & 0xE0) != 0xE0:
        return None
    version = (b[i + 1] >> 3) & 3
    layer = (b[i + 1] >> 1) & 3
    bitrate = b[i + 2] >> 4
    rate = (b[i + 2] >> 2) & 3
    if version == 1 or layer != 1 or bitrate in (0, 15) or rate == 3:
        return None
    return version, _RATES[version][rate], (b[i + 3] >> 6) == 3


def parse(data: bytes, audio_start: int = 0) -> Mp3Start | None:
    """Read the first frame of `data`, whose audio starts at `audio_start`
    (after any ID3v2 tag). None if no Layer III frame is found."""
    for i in range(audio_start, min(len(data) - 4, audio_start + _SCAN)):
        hdr = _frame_header(data, i)
        if hdr is None:
            continue
        version, rate, mono = hdr
        mpeg1 = version == 3
        spf = 1152 if mpeg1 else 576
        side = (17 if mono else 32) if mpeg1 else (9 if mono else 17)
        x = i + 4 + side
        tag = data[x:x + 4]
        if tag in (b"Xing", b"Info"):
            flags = int.from_bytes(data[x + 4:x + 8], "big")
            lame = x + 8 + (4 if flags & 1 else 0) + (4 if flags & 2 else 0) \
                + (100 if flags & 4 else 0) + (4 if flags & 8 else 0)
            delay = None
            if data[lame:lame + 4] in _TRUSTED_ENCODERS and len(data) >= lame + 24:
                d = data[lame + 21:lame + 24]
                delay = (d[0] << 4) | (d[1] >> 4)
            return Mp3Start(rate, spf, True, delay)
        if data[i + 36:i + 40] == b"VBRI":  # Fraunhofer's tag, always at 32 + 4
            return Mp3Start(rate, spf, True, None)
        return Mp3Start(rate, spf, False, None)
    return None


@lru_cache(maxsize=16384)
def _read_cached(path: str, size: int, mtime_ns: int) -> Mp3Start | None:
    with open(path, "rb") as f:
        head = f.read(10)
        start = _id3_end(head)
        f.seek(start)
        # Enough for the first frame and a scan past junk before it.
        return parse(f.read(_SCAN + 2048), 0)


def read(path: Path | str | None) -> Mp3Start | None:
    """`parse` a file on disk; None when it is missing, unreadable or no MP3.

    Cached per (path, size, mtime), so a file that appears later — a drive
    plugged in mid-session — or is replaced is read afresh, and a missing one is
    never remembered as headerless.
    """
    if not path:
        return None
    try:
        st = os.stat(path)
        return _read_cached(str(path), st.st_size, st.st_mtime_ns)
    except OSError:
        return None
