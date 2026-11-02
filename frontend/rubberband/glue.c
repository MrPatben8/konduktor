/*
 * Konduktor's WebAssembly interface to the Rubber Band Library, used by the
 * deck's keylock (time-stretch without a pitch change). Part of Konduktor,
 * licensed under the GNU AGPL-3.0 (see LICENSE at the repository root).
 *
 * Plain exports of the C API the deck worklet calls, plus malloc/free so it
 * can place its sample buffers in the module's memory. Rubber Band itself is
 * GPL-2.0-or-later (see README.md here).
 */
#include <stdlib.h>
#include <emscripten.h>
#include <rubberband-c.h>

#define EXPORT EMSCRIPTEN_KEEPALIVE

EXPORT void *kd_malloc(size_t size) { return malloc(size); }
EXPORT void kd_free(void *ptr) { free(ptr); }

EXPORT RubberBandState kd_new(unsigned int sample_rate, unsigned int channels,
                              RubberBandOptions options, double time_ratio,
                              double pitch_scale) {
  return rubberband_new(sample_rate, channels, options, time_ratio, pitch_scale);
}
EXPORT void kd_delete(RubberBandState s) { rubberband_delete(s); }
EXPORT void kd_reset(RubberBandState s) { rubberband_reset(s); }

EXPORT void kd_set_time_ratio(RubberBandState s, double r) { rubberband_set_time_ratio(s, r); }
EXPORT void kd_set_pitch_scale(RubberBandState s, double p) { rubberband_set_pitch_scale(s, p); }
EXPORT void kd_set_max_process_size(RubberBandState s, unsigned int n) {
  rubberband_set_max_process_size(s, n);
}

EXPORT unsigned int kd_get_preferred_start_pad(RubberBandState s) {
  return rubberband_get_preferred_start_pad(s);
}
EXPORT unsigned int kd_get_start_delay(RubberBandState s) { return rubberband_get_start_delay(s); }
EXPORT unsigned int kd_get_samples_required(RubberBandState s) {
  return rubberband_get_samples_required(s);
}

EXPORT void kd_process(RubberBandState s, const float *const *input, unsigned int samples,
                       int final) {
  rubberband_process(s, input, samples, final);
}
EXPORT int kd_available(RubberBandState s) { return rubberband_available(s); }
EXPORT unsigned int kd_retrieve(RubberBandState s, float *const *output, unsigned int samples) {
  return rubberband_retrieve(s, output, samples);
}
