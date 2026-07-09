#!/usr/bin/env bash
# Build the frozen backend sidecar (single-file binary) with PyInstaller and
# stage it for Tauri as binaries/konduktor-sidecar-<target-triple>.
# Output: backend/dist/konduktor-sidecar  +  frontend/src-tauri/binaries/…
#
# One-time: pip install -r requirements-build.txt (into the backend venv).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
PY="${PY:-.venv/bin/python}"
"$PY" -m PyInstaller --noconfirm --clean konduktor-sidecar.spec
echo "Built: $ROOT/dist/konduktor-sidecar"

# macOS: ad-hoc sign the sidecar so the app can spawn it. On Apple Silicon the
# OS kills unsigned executables on exec, and a signed .app (see tauri.conf.json
# bundle.macOS.signingIdentity "-") isn't enough on its own — the nested sidecar
# must be signed too. Ad-hoc (`-s -`) needs no cert/keychain; --force re-signs
# cleanly even if Tauri signs it again during bundling.
if [ "$(uname)" = "Darwin" ] && command -v codesign >/dev/null 2>&1; then
  codesign --force -s - "$ROOT/dist/konduktor-sidecar"
  echo "Ad-hoc signed: $ROOT/dist/konduktor-sidecar"
fi

# Stage for Tauri: the sidecar must be named with the host target triple so the
# shell plugin can resolve it in both dev and bundle. (rustc must be on PATH.)
if command -v rustc >/dev/null 2>&1; then
  TRIPLE="$(rustc -vV | sed -n 's/host: //p')"
  DEST="$ROOT/../frontend/src-tauri/binaries"
  mkdir -p "$DEST"
  cp "$ROOT/dist/konduktor-sidecar" "$DEST/konduktor-sidecar-$TRIPLE"
  echo "Staged: $DEST/konduktor-sidecar-$TRIPLE"
else
  echo "warning: rustc not found — skipped staging the Tauri sidecar binary" >&2
fi
