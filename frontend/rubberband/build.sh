#!/usr/bin/env bash
# Builds src/lib/audio/rubberband.wasm — the Rubber Band Library compiled to
# WebAssembly with Konduktor's own glue (glue.c) — for the deck's keylock.
#
# The .wasm is checked in, so dev and release builds need no emscripten; rerun
# this only when bumping Rubber Band or changing the glue. Needs emcc on PATH
# (macOS: `brew install emscripten`).
#
# Upstream is pinned by version AND SHA-256. Rubber Band is GPL-2.0-or-later
# ("either version 2 of the License, or (at your option) any later version"),
# which is what lets it combine with Konduktor's AGPL-3.0 — do NOT swap in a
# prebuilt npm package: their glue is labelled GPL-2.0(-only).
set -euo pipefail
cd "$(dirname "$0")"

VERSION=3.3.0
SHA256=d9ef89e2b8ef9f85b13ac3c2faec30e20acf2c9f3a9c8c45ce637f2bc95e576c
TARBALL="upstream/rubberband-${VERSION}.tar.bz2"
SRC="upstream/rubberband-${VERSION}"
OUT=../src/lib/audio/rubberband.wasm

mkdir -p upstream
if [ ! -f "$TARBALL" ]; then
  curl -fsSL -o "$TARBALL" "https://breakfastquay.com/files/releases/rubberband-${VERSION}.tar.bz2"
fi
echo "${SHA256}  ${TARBALL}" | shasum -a 256 -c -
rm -rf "$SRC"
tar xjf "$TARBALL" -C upstream

# -O3 + SIMD for speed on the audio thread (every deck's stretchers share one
# render thread). Single-threaded: RubberBandSingle.cpp sets NO_THREADING.
FLAGS=(-O3 -flto -msimd128 -fno-rtti -I"$SRC/rubberband")

# Rubber Band is C++, the glue C: compile each with its own driver, link as C++.
OBJ=upstream/obj
mkdir -p "$OBJ"
em++ "${FLAGS[@]}" -c "$SRC/single/RubberBandSingle.cpp" -o "$OBJ/rubberband.o"
emcc "${FLAGS[@]}" -c glue.c -o "$OBJ/glue.o"
em++ "${FLAGS[@]}" "$OBJ/rubberband.o" "$OBJ/glue.o" \
  -sSTANDALONE_WASM=1 \
  -sALLOW_MEMORY_GROWTH=1 \
  -sFILESYSTEM=0 \
  -sASSERTIONS=0 \
  -sERROR_ON_UNDEFINED_SYMBOLS=1 \
  --no-entry \
  -o "$OUT"

echo "wrote $OUT ($(wc -c < "$OUT" | tr -d ' ') bytes)"
