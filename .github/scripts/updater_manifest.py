"""Write the Tauri updater's latest.json for one release.

The app's update dialog finds the release itself (newest app tag on GitHub)
and points the updater at THAT release's latest.json, so each release carries
its own manifest describing its own assets. Run by build.yml's release job:

    python3 .github/scripts/updater_manifest.py artifacts \\
        --version 0.6.0 --tag v0.6.0-b42 --repo owner/konduktor

Expects, anywhere under the folder, the macOS `*.app.tar.gz` and the Windows
`*-setup.exe`, each with its `.sig` (written by `tauri build` when
`bundle.createUpdaterArtifacts` is on). Fails if any is missing: a release
whose manifest lacks a platform would be refused by the updater as a whole.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

# Tauri's platform keys -> the asset suffix that platform installs from.
PLATFORMS = {
    "darwin-aarch64": ".app.tar.gz",
    "windows-x86_64": "-setup.exe",
}


def find_one(root: Path, suffix: str) -> Path:
    found = [p for p in root.rglob(f"*{suffix}") if p.is_file()]
    if len(found) != 1:
        sys.exit(f"expected exactly one *{suffix} under {root}, found {len(found)}")
    return found[0]


def build(root: Path, version: str, tag: str, repo: str) -> dict:
    platforms = {}
    for key, suffix in PLATFORMS.items():
        asset = find_one(root, suffix)
        sig = asset.with_name(asset.name + ".sig")
        if not sig.is_file():
            sys.exit(f"missing signature {sig}")
        platforms[key] = {
            "signature": sig.read_text(encoding="utf-8").strip(),
            "url": f"https://github.com/{repo}/releases/download/{quote(tag)}/{quote(asset.name)}",
        }
    return {
        "version": version,
        "pub_date": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "platforms": platforms,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("artifacts", type=Path)
    ap.add_argument("--version", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--repo", required=True)
    args = ap.parse_args()
    manifest = build(args.artifacts, args.version, args.tag, args.repo)
    out = args.artifacts / "latest.json"
    out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
