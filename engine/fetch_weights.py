"""Fetch and verify the pinned htdemucs_ft weights into a folder (CI, dev).

    python fetch_weights.py DEST

Standard library only. Files already present with the right hash are kept.
Konduktor itself downloads through its engine manager, from the same manifest.
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

MANIFEST = json.loads((Path(__file__).resolve().parent / "weights.json").read_text())


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(dest: str) -> int:
    folder = Path(dest)
    folder.mkdir(parents=True, exist_ok=True)
    for f in MANIFEST["files"]:
        target = folder / f["name"]
        if target.exists() and sha256(target) == f["sha256"]:
            print(f"ok      {f['name']}")
            continue
        url = MANIFEST["url"].format(repo=MANIFEST["repo"], revision=MANIFEST["revision"], name=f["name"])
        partial = target.with_name(target.name + ".part")
        with urllib.request.urlopen(url, timeout=60) as r, open(partial, "wb") as out:
            while chunk := r.read(1 << 20):
                out.write(chunk)
        got = sha256(partial)
        if got != f["sha256"]:
            partial.unlink()
            print(f"HASH MISMATCH {f['name']}: {got}")
            return 1
        partial.replace(target)
        print(f"fetched {f['name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
