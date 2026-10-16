"""Lay out one release's assets under user-facing names, plus the updater's
latest.json and the release notes' Download section.

Run by build.yml's release job:

    python3 .github/scripts/release_assets.py artifacts release \\
        --version 0.6.0 --tag v0.6.0-b42 --repo owner/konduktor

Finds, anywhere under `artifacts`, what `tauri build` produced: the macOS
`*.dmg` and `*.app.tar.gz` (+ `.sig`) and the Windows `*-setup.exe` (+ `.sig`).
Copies them into `release` as

    Konduktor-<version>-macOS.dmg              (download)
    Konduktor-<version>-Windows-Setup.exe      (download; the Windows updater runs it too)
    Konduktor-<version>-app-updater.tar.gz     (the macOS updater's package)
    latest.json                                (the updater's manifest)

and writes `download.md`. Renaming is safe: the signatures cover contents,
not names, and latest.json is written from the final names. `latest.json`
itself keeps its name: the app asks for releases/download/<tag>/latest.json.
Fails if anything is missing: a manifest lacking a platform is refused by the
updater as a whole.
"""

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


def find_one(root: Path, suffix: str) -> Path:
    found = [p for p in root.rglob(f"*{suffix}") if p.is_file()]
    if len(found) != 1:
        sys.exit(f"expected exactly one *{suffix} under {root}, found {len(found)}")
    return found[0]


def signature(asset: Path) -> str:
    sig = asset.with_name(asset.name + ".sig")
    if not sig.is_file():
        sys.exit(f"missing signature {sig}")
    return sig.read_text(encoding="utf-8").strip()


def lay_out(artifacts: Path, out: Path, version: str, tag: str, repo: str) -> None:
    def url(name: str) -> str:
        return f"https://github.com/{repo}/releases/download/{quote(tag)}/{quote(name)}"

    dmg = find_one(artifacts, ".dmg")
    app = find_one(artifacts, ".app.tar.gz")
    setup = find_one(artifacts, "-setup.exe")

    names = {
        dmg: f"Konduktor-{version}-macOS.dmg",
        setup: f"Konduktor-{version}-Windows-Setup.exe",
        app: f"Konduktor-{version}-app-updater.tar.gz",
    }
    # Tauri's platform keys -> the file that platform's updater installs.
    manifest = {
        "version": version,
        "pub_date": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "platforms": {
            "darwin-aarch64": {"signature": signature(app), "url": url(names[app])},
            "windows-x86_64": {"signature": signature(setup), "url": url(names[setup])},
        },
    }

    out.mkdir(parents=True, exist_ok=True)
    for src, name in names.items():
        shutil.copy2(src, out / name)
    (out / "latest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (out / "download.md").write_text(
        "## Download\n"
        f"- **macOS** (Apple Silicon): [{names[dmg]}]({url(names[dmg])})\n"
        f"- **Windows**: [{names[setup]}]({url(names[setup])})\n",
        encoding="utf-8",
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("artifacts", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--version", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--repo", required=True)
    args = ap.parse_args()
    lay_out(args.artifacts, args.out, args.version, args.tag, args.repo)
    for p in sorted(args.out.iterdir()):
        print(p.name)


if __name__ == "__main__":
    main()
