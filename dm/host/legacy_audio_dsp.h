// Legacy audren CPU processing. The caller serializes updates and rendering.
#pragma once
#include <stddef.h>
#include <stdint.h>
typedef struct ar_dsp ar_dsp;
ar_dsp *ar_dsp_create(const uint8_t *parameter, size_t size);
void ar_dsp_destroy(ar_dsp *dsp);
uint32_t ar_dsp_update(ar_dsp *dsp, const void *input, size_t input_size, void *output, size_t output_size);
int ar_dsp_render(ar_dsp *dsp, int16_t *stereo, size_t capacity_frames);
