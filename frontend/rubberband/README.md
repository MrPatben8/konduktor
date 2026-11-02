# Rubber Band (keylock)

`src/lib/audio/rubberband.wasm` is the [Rubber Band Library](https://breakfastquay.com/rubberband/)
3.3.0 compiled to WebAssembly with Konduktor's own glue (`glue.c`). The deck's
audio worklet uses it for keylock: tempo changes without a pitch change. It
runs the R2 ("faster") engine in real-time mode.

**Licence.** Rubber Band is GPL-2.0-or-later. Every upstream source file says
"either version 2 of the License, or (at your option) any later version", and
its bundled `src/ext` code (KissFFT, Speex resampler, getopt, pommier) is
permissive. That is what allows it to combine with Konduktor's AGPL-3.0, via
GPLv3 §13. Do NOT replace it with a prebuilt npm package:
`rubberband-wasm`'s glue is labelled "GPLv2" without "or later", and
`@echogarden/rubberband-wasm` is GPL-2.0-only. Neither can be combined with
AGPL-3.0.

**Corresponding source** for the `.wasm`: `build.sh` plus `glue.c` plus the
upstream release it downloads, which is pinned by version and SHA-256.

**Rebuilding** is needed only when bumping Rubber Band or changing the glue:
run `./build.sh`, which needs `emcc` on PATH (`brew install emscripten`). The
`.wasm` is checked in, so neither `npm run dev` nor a release build needs
emscripten.

**Measured** (2026-11-02, Apple M3 Pro, stereo 44.1 kHz, 128-frame quanta):
about 40-55x realtime in both JavaScriptCore (WKWebView) and V8 (WebView2).
p99 is about 0.17 ms per quantum against a 2.9 ms budget, so 8 stretchers
(2 stem decks) fit on the one audio render thread. The R3 ("finer") engine is
about 11x realtime and does not fit at 8. `-msimd128` measured no faster than
the `-Oz` npm build.
