#!/usr/bin/env bash
# Build the frozen backend sidecar (a onedir folder) with PyInstaller and stage
# it for Tauri as frontend/src-tauri/binaries/konduktor-sidecar/, which
# tauri.conf.json bundles as a resource folder (`sidecar/` in the app).
# Output: backend/dist/konduktor-sidecar/  +  frontend/src-tauri/binaries/konduktor-sidecar/
#
# One-time: pip install -r requirements-build.txt (into the backend venv).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
PY="${PY:-.venv/bin/python}"
"$PY" -m PyInstaller --noconfirm --clean konduktor-sidecar.spec
echo "Built: $ROOT/dist/konduktor-sidecar/"

# NOTE: do NOT re-codesign anything in this folder. PyInstaller already ad-hoc
# signs the executable AND its collected libraries (Python, the .so/.dylib
# files) with a matching identity. Re-signing only some of them desyncs the
# Team IDs, so dlopen fails with "different Team IDs" and the sidecar dies on
# launch (the app then shows "Konduktor couldn't start"). The app bundle is
# ad-hoc signed separately via tauri.conf.json `bundle.macOS.signingIdentity`,
# which seals this folder as a resource without re-signing it.

# Stage for Tauri. Replace the whole folder so no file from an older build
# survives; `cp -R` keeps PyInstaller's symlinks as symlinks.
DEST="$ROOT/../frontend/src-tauri/binaries"
mkdir -p "$DEST"
rm -rf "$DEST/konduktor-sidecar"
cp -R "$ROOT/dist/konduktor-sidecar" "$DEST/konduktor-sidecar"
echo "Staged: $DEST/konduktor-sidecar/"
