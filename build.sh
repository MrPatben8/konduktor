#!/usr/bin/env bash
# Build the Konduktor desktop app on macOS: freeze + stage the backend sidecar,
# then build the Tauri app (.app + .dmg). The macOS twin of build.ps1.
# One-time setup:
#   python3 -m venv backend/.venv
#   backend/.venv/bin/pip install -r backend/requirements.txt -r backend/requirements-build.txt
#   (cd frontend && npm install)
#   a Rust toolchain from https://rustup.rs
#
#   ./build.sh                   # sidecar + app (release bundles)
#   ./build.sh --skip-sidecar    # frontend-only change: reuse the staged sidecar
#   ./build.sh --debug           # anything else is passed on to `tauri build`
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SKIP_SIDECAR=0
TAURI_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --skip-sidecar) SKIP_SIDECAR=1 ;;
    *) TAURI_ARGS+=("$arg") ;;
  esac
done

die() { echo "error: $*" >&2; exit 1; }

[ -x "$ROOT/backend/.venv/bin/python" ] || die "No backend venv at backend/.venv - see the one-time setup at the top of build.sh."
[ -d "$ROOT/frontend/node_modules" ] || die "No frontend/node_modules - run \`npm install\` in frontend first."
STAGED="$ROOT/frontend/src-tauri/binaries/konduktor-sidecar/konduktor-sidecar"

# rustup adds ~/.cargo/bin to PATH only in shells started AFTER the install -
# look there before giving up, rather than failing after the sidecar build.
if ! command -v cargo >/dev/null 2>&1; then
  [ -x "$HOME/.cargo/bin/cargo" ] || die "No Rust toolchain (cargo) - install it from https://rustup.rs."
  export PATH="$HOME/.cargo/bin:$PATH"
fi

# Bundles newer than this marker are this run's (release or --debug profile).
MARKER="$(mktemp)"
trap 'rm -f "$MARKER"' EXIT

if [ "$SKIP_SIDECAR" = 1 ]; then
  [ -x "$STAGED" ] || die "No staged sidecar to reuse - run without --skip-sidecar first."
  echo "==> Reusing the staged sidecar (--skip-sidecar)"
else
  echo "==> 1/2 Freezing the backend sidecar"
  PY="$ROOT/backend/.venv/bin/python" "$ROOT/backend/build_sidecar.sh"
fi

# Updater packages are signed with the release key (CI's secret). Without it,
# build the app and .dmg alone rather than fail at the signing step.
if [ -z "${TAURI_SIGNING_PRIVATE_KEY:-}" ]; then
  TAURI_ARGS=(--config src-tauri/tauri.unsigned.conf.json ${TAURI_ARGS[@]+"${TAURI_ARGS[@]}"})
fi

# A failed .dmg step leaves its read-write image behind (`rw.<pid>.*.dmg`,
# hundreds of MB each); clear old ones.
find "$ROOT/frontend/src-tauri/target" -path '*/bundle/macos/rw.*.dmg' -delete 2>/dev/null || true

echo ""
echo "==> 2/2 Building the app (tauri build ${TAURI_ARGS[*]+${TAURI_ARGS[*]}})"
LOG="$(mktemp)"
trap 'rm -f "$MARKER" "$LOG"' EXIT
if ! (cd "$ROOT/frontend" && npx tauri build ${TAURI_ARGS[@]+"${TAURI_ARGS[@]}"}) 2>&1 | tee "$LOG"; then
  # The .dmg step mounts a fresh image and has Finder lay it out, which fails
  # now and then (measured: the same build passed on a rerun). The .app is
  # already built by then, so retry just the .dmg once before giving up.
  grep -q 'bundle_dmg.sh' "$LOG" || exit 1
  echo ""
  echo "==> The .dmg step failed; retrying it once"
  (cd "$ROOT/frontend" && npx tauri build ${TAURI_ARGS[@]+"${TAURI_ARGS[@]}"} --bundles dmg)
fi

echo ""
BUILT="$(find "$ROOT/frontend/src-tauri/target" -path '*/bundle/*' \( -name '*.dmg' -o -name '*.app' \) \
  -newer "$MARKER" -prune 2>/dev/null || true)"
if [ -n "$BUILT" ]; then
  echo "Built:"
  echo "$BUILT" | sed 's/^/  /'
else
  echo "Done (nothing bundled - see frontend/src-tauri/target/)."
fi
