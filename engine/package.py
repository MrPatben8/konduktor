"""Archive a frozen engine for release: tar.gz, split under GitHub's asset cap.

    python package.py TARGET DIST_DIR OUT_DIR

Writes `konduktor-engine-<version>-<target>.tar.gz[.partNN]` and
`<target>.json` (the parts with sizes and SHA-256, plus the whole archive's
hash) for the release job to fold into `engine-manifest.json`.

tar.gz, not zip: `zipfile` drops the executable bit on macOS. Split because a
release asset is capped at 2 GiB and the CUDA engine is bigger than that.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from konduktor_engine import VERSION  # noqa: E402

PART = 1_900 * 1024 * 1024  # below the 2 GiB cap with room to spare


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(target: str, dist: str, out: str) -> int:
    src = Path(dist) / "konduktor-engine"
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Symlinks are archived as links (the macOS build has several), so they
    # must not be counted — following them counted torch's libraries twice.
    files = [p for p in src.rglob("*") if p.is_file() and not p.is_symlink()]
    unpacked = sum(p.lstat().st_size for p in files)
    longest = max(len(p.relative_to(src.parent).as_posix()) for p in files)
    base = f"konduktor-engine-{VERSION}-{target}.tar.gz"
    archive = out_dir / base
    with tarfile.open(archive, "w:gz", compresslevel=6) as tar:
        tar.add(src, arcname="konduktor-engine")
    whole = sha256_file(archive)
    size = archive.stat().st_size
    parts = []
    if size <= PART:
        parts.append({"name": base, "size": size, "sha256": whole})
    else:
        with open(archive, "rb") as f:
            n = 0
            while chunk := f.read(PART):
                name = f"{base}.part{n:02d}"
                (out_dir / name).write_bytes(chunk)
                parts.append({"name": name, "size": len(chunk), "sha256": hashlib.sha256(chunk).hexdigest()})
                n += 1
        archive.unlink()
    info = {"target": target, "version": VERSION, "archive": base, "size": size, "sha256": whole,
            "unpacked_size": unpacked, "files": len(files), "longest_path": longest, "parts": parts}
    (out_dir / f"{target}.json").write_text(json.dumps(info, indent=1))
    print(json.dumps({k: v for k, v in info.items() if k != "parts"}), f"{len(parts)} part(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))
