// Fixed-function DrawTexture mapping, checked against synthetic calls into the
// SDK 1.6.0 NVN driver (build 1686ae4b62a9c6a3b6ee7a9187ee4932, +0x9c1fc).
#pragma once
#include <stdint.h>
#include <limits.h>
#include <math.h>

typedef struct {
    int32_t dst[4];       // origin and signed extent, in 20.12 fixed point
    int32_t src[2];       // source origin in texels, in 20.12 fixed point
    int64_t step[2];      // source texels per destination pixel, in 32.32 fixed point
} nvn_draw_texture_map;

static inline int32_t nvn_dt_position(float value) {
    double fixed = floor((double)value * 256.0 + 0.5) * 16.0;
    if (fixed >= (double)INT32_MAX) return INT32_MAX;
    if (fixed <= (double)INT32_MIN) return INT32_MIN;
    return (int32_t)fixed;
}

static inline int64_t nvn_dt_step(float delta, int32_t extent) {
    double fixed = floor((double)delta / ((double)extent / 4096.0) * 4294967296.0 + 0.5);
    if (fixed >= (double)INT64_MAX) return INT64_MAX;
    if (fixed <= (double)INT64_MIN) return INT64_MIN;
    return (int64_t)fixed;
}

// 1 = drawable, 0 = an empty quantized destination, -1 = invalid coordinates.
static inline int nvn_dt_prepare(nvn_draw_texture_map *out, const float dst[4], const float src[4]) {
    for (int i = 0; i < 4; i++) if (!isfinite(dst[i]) || !isfinite(src[i])) return -1;
    int32_t x0 = nvn_dt_position(dst[0]), y0 = nvn_dt_position(dst[1]);
    int32_t x1 = nvn_dt_position(dst[2]), y1 = nvn_dt_position(dst[3]);
    int32_t w = (int32_t)((uint32_t)x1 - (uint32_t)x0), h = (int32_t)((uint32_t)y1 - (uint32_t)y0);
    if (!w || !h) return 0;
    out->dst[0] = x0; out->dst[1] = y0; out->dst[2] = w; out->dst[3] = h;
    out->src[0] = nvn_dt_position(src[0]); out->src[1] = nvn_dt_position(src[1]);
    // NVN subtracts the source corners as float before computing the double slope.
    float dx = src[2] - src[0], dy = src[3] - src[1];
    out->step[0] = nvn_dt_step(dx, w); out->step[1] = nvn_dt_step(dy, h);
    return 1;
}
