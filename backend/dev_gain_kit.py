"""Kit G1 — what Traktor 4.5 does with gain (dev only, like dev_stems.py).

Answers, by measuring in Traktor itself (see
.claude/discussions/discuss-gain-normalization-2026-10-19.md, "Kit G1"):

  1. Does a LEVEL change to a file make Traktor re-analyse it (LOUDNESS,
     BPM/grid, AUDIO_ID)? MP3 by lossless frame gain (same size; + an APE undo
     tag; + the mtime put back), FLAC re-written exactly, and a control whose
     mtime alone changes.
  2. Does Traktor KEEP a LOUDNESS Konduktor writes — on an entry it analysed
     (AUDIO_ID kept), and on one Konduktor added (AUDIO_ID empty)?
  3. Which LOUDNESS attribute drives the deck's auto-gain (the three are set
     to values that cannot be confused)?

    python dev_gain_kit.py prepare   # build the kit folder
    #  -> in Traktor: import the folder, analyse, quit
    python dev_gain_kit.py apply     # change the files + write LOUDNESS
    #  -> in Traktor: load each G1 track, note the gain knob, quit
    python dev_gain_kit.py report    # what Traktor changed

Traktor must be closed for `apply`. The collection is saved through
TraktorStore (backup-first, byte-exact render).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

NML = Path.home() / "Documents/Native Instruments/Traktor 4.5.0/collection.nml"
KITS = Path.home() / "Music/Konduktor Stem Test"
KIT = KITS / "Kit G1"
STEPS = -2  # global_gain steps of 2^(1/4): -2 = -3.01 dB

# name -> (source, what happens at `apply`)
FILES = {
    "G1-A0 control touch.mp3": (KITS / "Kit 2/K2 01 All I Need.mp3", "mtime only"),
    "G1-A1 mp3 framegain.mp3": (KITS / "Kit 2/K1 01 Abyss of Love - Franky Rizardo Remix.mp3", "frame gain"),
    "G1-A2 mp3 framegain ape.mp3": (KITS / "Kit 2/K4 Ragga Bomb with Ragga Twins.mp3", "frame gain + APE undo tag"),
    "G1-A3 flac exact.flac": (KITS / "Kit 4/K4-3 flac.flac", "exact gain, re-written"),
    "G1-A4 mp3 framegain same mtime.mp3": (KITS / "Kit 4/K4-5 mp3 cbr noxing.mp3", "frame gain, mtime restored"),
    "G1-B1 loudness written.mp3": (KITS / "Kit 2/K3 01 Motorola.mp3", "LOUDNESS written, AUDIO_ID kept"),
}
# Added by Konduktor at `apply` (so Traktor never analysed it): AUDIO_ID empty.
B2 = ("G1-B2 konduktor added.mp3", KITS / "Kit 4/K4-4 mp3 cbr xing.mp3")

# Values no real analysis would give, so the deck's knob says which one it read.
B1_LOUDNESS = (2.5, -6.0, 6.0)  # PEAK_DB, PERCEIVED_DB, ANALYZED_DB
B2_LOUDNESS = (2.5, -4.0, 4.0)


# ── MP3 frame gain (mp3gain's edit) ──────────────────────────────────────────

_BITRATE = {
    True: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    False: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_RATE = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def _get(buf: bytearray, bit: int, n: int) -> int:
    v = 0
    for i in range(n):
        v = (v << 1) | ((buf[(bit + i) >> 3] >> (7 - ((bit + i) & 7))) & 1)
    return v


def _put(buf: bytearray, bit: int, n: int, v: int) -> None:
    for i in range(n):
        b = (v >> (n - 1 - i)) & 1
        byte, shift = (bit + i) >> 3, 7 - ((bit + i) & 7)
        buf[byte] = (buf[byte] & ~(1 << shift) & 0xFF) | (b << shift)


def _header(buf: bytearray, pos: int):
    if pos + 4 > len(buf) or buf[pos] != 0xFF or (buf[pos + 1] & 0xE0) != 0xE0:
        return None
    ver, layer = (buf[pos + 1] >> 3) & 3, (buf[pos + 1] >> 1) & 3
    br_i, sr_i = buf[pos + 2] >> 4, (buf[pos + 2] >> 2) & 3
    if ver == 1 or layer != 1 or br_i in (0, 15) or sr_i == 3:
        return None
    mpeg1 = ver == 3
    br, sr = _BITRATE[mpeg1][br_i], _RATE[ver][sr_i]
    pad = (buf[pos + 2] >> 1) & 1
    length = (144000 if mpeg1 else 72000) * br // sr + pad
    return {
        "mpeg1": mpeg1,
        "crc": (buf[pos + 1] & 1) == 0,
        "nch": 1 if (buf[pos + 3] >> 6) == 3 else 2,
        "len": length,
    }


def _gain_bits(h: dict) -> list[int]:
    """Bit offsets (from the side info's start) of every granule's global_gain."""
    nch = h["nch"]
    if h["mpeg1"]:
        start, block, granules = 9 + (5 if nch == 1 else 3) + 4 * nch, 59, 2
    else:
        start, block, granules = 8 + (1 if nch == 1 else 2), 63, 1
    return [start + (g * nch + c) * block + 21 for g in range(granules) for c in range(nch)]


def mp3_frame_gain(path: Path, steps: int) -> int:
    """Add `steps` to every audio granule's global_gain in place; returns frames edited."""
    buf = bytearray(path.read_bytes())
    pos = 0
    if buf[:3] == b"ID3":
        size = (buf[6] << 21) | (buf[7] << 14) | (buf[8] << 7) | buf[9]
        pos = 10 + size + (10 if buf[5] & 0x10 else 0)
    frames = 0
    first = True
    while (h := _header(buf, pos)) is not None:
        side = pos + 4 + (2 if h["crc"] else 0)
        if h["crc"]:
            raise SystemExit(f"{path.name}: CRC-protected frames are not handled by the kit")
        side_len = (17 if h["nch"] == 1 else 32) if h["mpeg1"] else (9 if h["nch"] == 1 else 17)
        tag = bytes(buf[side + side_len : side + side_len + 4])
        if first and (tag in (b"Xing", b"Info") or bytes(buf[pos + 36 : pos + 40]) == b"VBRI"):
            pass  # the header frame is not audio (Traktor decodes it anyway — leave it as it is)
        else:
            for off in _gain_bits(h):
                g = _get(buf, side * 8 + off, 8) + steps
                if not 0 <= g <= 255:
                    raise SystemExit(f"{path.name}: global_gain would leave 0..255")
                _put(buf, side * 8 + off, 8, g)
            frames += 1
        first = False
        pos += h["len"]
    rest = bytes(buf[pos : pos + 8])
    if pos < len(buf) - 128 and not rest.startswith((b"TAG", b"APETAGEX", b"LYRICS")):
        raise SystemExit(f"{path.name}: lost frame sync at {pos} of {len(buf)}")
    path.write_bytes(buf)
    return frames


def _decode(path: Path) -> np.ndarray:
    from konduktor.core import pyav

    out = []
    with pyav.open_input(path) as c:
        for frame in c.decode(audio=0):
            a = frame.to_ndarray()
            out.append(a if a.ndim == 2 and a.shape[0] <= 2 else a.reshape(1, -1))
    return np.concatenate(out, axis=1).astype(np.float64)


def _check_gain(before: np.ndarray, after: Path, db: float, what: str) -> None:
    b = before
    a = _decode(after)
    n = min(b.shape[1], a.shape[1])
    want = 10 ** (db / 20)
    err = np.max(np.abs(a[:, :n] - b[:, :n] * want)) / max(np.max(np.abs(b)), 1e-9)
    print(f"    {what}: decoded = original x {want:.4f} ({db:+.2f} dB), max error {err:.1e} of peak")
    if err > 1e-3:
        raise SystemExit("    gain check FAILED")


def flac_exact_gain(path: Path, db: float) -> None:
    import soundfile as sf
    from mutagen.flac import FLAC

    info = sf.info(str(path))
    data, rate = sf.read(str(path), dtype="float64", always_2d=True)
    old = FLAC(str(path))
    tags, pictures = list(old.tags.items()) if old.tags else [], list(old.pictures)
    tmp = path.with_suffix(".tmp.flac")
    sf.write(str(tmp), data * 10 ** (db / 20), rate, subtype=info.subtype, format="FLAC")
    new = FLAC(str(tmp))
    for k, v in tags:
        new[k] = v
    for p in pictures:
        new.add_picture(p)
    new.save()
    tmp.replace(path)


# ── steps ────────────────────────────────────────────────────────────────────


def _traktor_running() -> bool:
    # The executable is "Traktor Pro 4" (an exact "Traktor" never matches).
    return subprocess.run(["pgrep", "-f", r"Traktor Pro 4\.app/Contents/MacOS/"], capture_output=True).returncode == 0


def prepare() -> None:
    KIT.mkdir(parents=True, exist_ok=True)
    for name, (src, what) in FILES.items():
        shutil.copy2(src, KIT / name)
        print(f"  {name:38s} <- {src.name}   [{what} at apply]")
    print(f"\nKit at {KIT}\n"
          "Next, in Traktor 4.5:\n"
          "  1. Preferences > Mixer: note the Autogain setting, and turn ON\n"
          "     'Set Autogain when Loading Track' (or the nearest wording).\n"
          "  2. Import the Kit G1 folder; select its 6 tracks; Analyze (Async) > All.\n"
          "  3. Quit Traktor (it writes the collection on exit).\n"
          "Then: python dev_gain_kit.py apply")


def _entries(nml_text: str) -> dict[str, str]:
    out = {}
    for e in re.findall(r"<ENTRY .*?</ENTRY>", nml_text, re.S):
        m = re.search(r'<LOCATION [^>]*FILE="(G1-[^"]+)"', e)
        if m:
            out[m.group(1)] = e
    return out


def apply() -> None:
    if _traktor_running():
        raise SystemExit("Quit Traktor first.")
    text = NML.read_text(encoding="utf-8")
    have = _entries(text)
    missing = [n for n in FILES if n not in have]
    if missing:
        raise SystemExit(f"Not in the collection yet (import + analyse in Traktor): {missing}")
    unanalysed = [n for n in FILES if "<LOUDNESS" not in have[n] or 'AUDIO_ID=""' in have[n] or "AUDIO_ID" not in have[n]]
    if unanalysed:
        raise SystemExit(f"Not analysed by Traktor yet: {unanalysed}")
    shutil.copy2(NML, KIT / "1-after-traktor-analysed.nml")

    print("Changing files:")
    for name, (_, what) in FILES.items():
        p = KIT / name
        st = p.stat()
        if what == "mtime only":
            os.utime(p)
            print(f"  {name}: mtime touched")
        elif what.startswith("frame gain"):
            before = _decode(p)
            n = mp3_frame_gain(p, STEPS)
            print(f"  {name}: {n} frames, {STEPS:+d} steps")
            _check_gain(before, p, STEPS * 20 * np.log10(2 ** 0.25), "check")
            if "APE" in what:
                from mutagen.apev2 import APEv2

                tag = APEv2()
                tag["MP3GAIN_UNDO"] = f"{-STEPS:+04d},{-STEPS:+04d},N"
                tag.save(str(p))
                print("    + APEv2 MP3GAIN_UNDO tag")
            if "restored" in what:
                os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
                print(f"    mtime restored, size {'same' if p.stat().st_size == st.st_size else 'CHANGED'}")
        elif what.startswith("exact"):
            before = _decode(p)
            db = STEPS * 20 * np.log10(2 ** 0.25)
            flac_exact_gain(p, db)
            print(f"  {name}: {db:+.2f} dB, size {st.st_size} -> {p.stat().st_size}")
            _check_gain(before, p, db, "check")

    from konduktor.adapters.traktor.store import TraktorStore
    from konduktor.core import folder
    from traktor_nml_utils.models.collection import Loudnesstype

    shutil.copy2(B2[1], KIT / B2[0])
    store = TraktorStore(NML)
    store.add_entry(folder.read_track(KIT / B2[0]), KIT / B2[0])

    def set_loudness(file: str, vals) -> None:
        for e in store._nml.collection.entry:
            if e.location is not None and e.location.file == file:
                e.loudness = Loudnesstype(peak_db=vals[0], perceived_db=vals[1], analyzed_db=vals[2])
                return
        raise SystemExit(f"entry {file} not found")

    set_loudness("G1-B1 loudness written.mp3", B1_LOUDNESS)
    set_loudness(B2[0], B2_LOUDNESS)
    store.save()
    shutil.copy2(NML, KIT / "2-after-konduktor-apply.nml")
    print(f"\nLOUDNESS written: B1 {B1_LOUDNESS}, B2 (added, AUDIO_ID empty) {B2_LOUDNESS}\n"
          "Next, in Traktor 4.5 (do NOT run Analyze):\n"
          "  1. Load EACH G1 track into deck A in turn (B1 and B2 first).\n"
          "     For each, note the GAIN knob's value (hover / the readout) and\n"
          "     whether Traktor shows it analysing.\n"
          "  2. Quit Traktor.\n"
          "Then: python dev_gain_kit.py report")


_FIELDS = {
    "AUDIO_ID": r'AUDIO_ID="([^"]*)"',
    "LOUDNESS": r"<LOUDNESS ([^>]*)>",
    "TEMPO": r'<TEMPO BPM="([^"]*)"',
    "GRID": r'<CUE_V2 [^>]*TYPE="4"[^>]*START="([^"]*)"',
    "MODIFIED": r'MODIFIED_DATE="([^"]*)" MODIFIED_TIME="([^"]*)"',
    "FILESIZE": r'FILESIZE="([^"]*)"',
    "LAST_PLAYED": r'LAST_PLAYED="([^"]*)"',
}


def report() -> None:
    if _traktor_running():
        raise SystemExit("Quit Traktor first (it writes the collection on exit).")
    shutil.copy2(NML, KIT / "3-after-traktor-load.nml")
    stages = [
        ("analysed", _entries((KIT / "1-after-traktor-analysed.nml").read_text(encoding="utf-8"))),
        ("applied", _entries((KIT / "2-after-konduktor-apply.nml").read_text(encoding="utf-8"))),
        ("loaded", _entries(NML.read_text(encoding="utf-8"))),
    ]
    for name in [*FILES, B2[0]]:
        print(f"\n== {name}  [{FILES.get(name, (None, 'added by Konduktor'))[1]}]")
        for field, rx in _FIELDS.items():
            vals = []
            for _, ents in stages:
                e = ents.get(name)
                m = re.search(rx, e) if e else None
                vals.append(" ".join(m.groups()) if m else ("-" if e else "(no entry)"))
            flag = "" if vals[1] == vals[2] else "   <-- CHANGED by Traktor"
            print(f"  {field:11s} analysed: {vals[0][:60]}\n"
                  f"  {'':11s} applied:  {vals[1][:60]}\n"
                  f"  {'':11s} loaded:   {vals[2][:60]}{flag}")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    {"prepare": prepare, "apply": apply, "report": report}[sys.argv[1] if len(sys.argv) > 1 else "prepare"]()
