// audren REV1/REV2 wire layouts, based on libnx services/audren.h (ISC).
// Copyright and permission notice: dm/third_party/libnx/LICENSE.md.
#pragma once
#include <stddef.h>
#include <stdint.h>
typedef struct { uint32_t revision, behavior, pools, voices, channels, effects, mixes, sinks, performance, reserved[6], total; } ar_header;
typedef struct { uint64_t address, size; uint32_t state, reserved[3]; } ar_pool;
typedef struct { uint32_t id; float volume[24]; uint8_t used, reserved[11]; } ar_channel;
typedef struct { uint8_t enabled, reserved; int16_t numerator[3], denominator[2]; } ar_biquad;
typedef struct { uint64_t address, size; int32_t start, end; uint8_t loop, eos, sent, reserved[5]; uint64_t context, context_size, reserved2; } ar_wave;
typedef struct {
    uint32_t id, node; uint8_t is_new, used, state, format;
    uint32_t rate, priority, order, channels; float pitch, volume;
    ar_biquad biquad[2]; uint32_t wave_count; uint16_t wave_head, reserved;
    uint32_t reserved2; uint64_t extra, extra_size; uint32_t mix, splitter;
    ar_wave wave[4]; uint32_t channel[6]; uint8_t reserved3[24];
} ar_voice;
typedef struct {
    float volume; uint32_t rate, buffers; uint8_t used, reserved[3];
    uint32_t id, effect_count, node, reserved2[2]; float matrix[24][24];
    uint32_t destination, splitter, reserved3;
} ar_mix;
typedef struct { uint8_t type, is_new, enabled, reserved; uint32_t mix; uint64_t work, work_size; uint32_t order, reserved2; uint8_t data[0xa0]; } ar_effect;
typedef struct { uint8_t type, used, reserved[2]; uint32_t node; uint64_t reserved2[3]; uint8_t data[0x120]; } ar_sink;
_Static_assert(sizeof(ar_header) == 0x40, "audren update header");
_Static_assert(sizeof(ar_pool) == 0x20 && sizeof(ar_channel) == 0x70, "audren pool/channel layout");
_Static_assert(sizeof(ar_wave) == 0x38 && sizeof(ar_voice) == 0x170, "audren voice layout");
_Static_assert(offsetof(ar_voice, wave) == 0x60 && offsetof(ar_voice, channel) == 0x140, "audren voice member offsets");
_Static_assert(sizeof(ar_mix) == 0x930 && sizeof(ar_effect) == 0xc0 && sizeof(ar_sink) == 0x140, "audren mix/effect/sink layout");

