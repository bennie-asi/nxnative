// Software processing for the original audren protocol, used when the SDK has no
// in-process CPU renderer. REV2's 0x170-byte voices and 0xc0-byte effects were
// checked against a native SDK 1.6 request: 182 voices, 26 effects, two mixes.
#include "legacy_audio_dsp.h"
#include "legacy_audio_protocol.h"
#include <math.h>
#include <stdlib.h>
#include <string.h>

enum { AR_BAD = 0x499, AR_MAX_CHANNELS = 6, AR_MIX_CHANNELS = 24 };
typedef struct { int initialized, next_sample, block[2]; int16_t samples[2][14], h1, h2; uint16_t index; } ar_adpcm;
typedef struct {
    ar_wave wave[4]; uint8_t pending[4]; int current, positioned;
    double position; uint64_t played; uint32_t consumed;
    ar_adpcm adpcm[6]; float filter[6][2][4];
} ar_voice_state;
typedef struct { float *data, low; int length, at; } ar_delay;
typedef struct { ar_delay pre[6], early[6], comb[6][4], allpass[6][2]; float low[6]; uint32_t rate; } ar_reverb;
struct ar_dsp {
    uint32_t revision, rate, samples, voices, effects, mixes, sinks, pool_count;
    ar_pool *pool; ar_channel *channel; ar_voice *voice; ar_voice_state *state;
    ar_mix *mix; ar_effect *effect; ar_sink *sink; ar_reverb *reverb;
    float *buffers, *scratch; uint32_t *sink_offset; uint64_t rendered;
};
static uint32_t u32_at(const void *p) { uint32_t v; memcpy(&v, p, 4); return v; }
static uint64_t u64_at(const void *p) { uint64_t v; memcpy(&v, p, 8); return v; }
static float f32_at(const void *p) { float v; memcpy(&v, p, 4); return isfinite(v) ? v : 0; }
static float finite_gain(float v) { return isfinite(v) ? v : 0; }
static int16_t clip16(float v) { if (!isfinite(v)) return 0; return v > 32767 ? 32767 : v < -32768 ? -32768 : (int16_t)lrintf(v); }
static void decoder_reset(ar_voice_state *s) {
    for (int c = 0; c < 6; c++) { memset(&s->adpcm[c], 0, sizeof s->adpcm[c]); s->adpcm[c].block[0] = s->adpcm[c].block[1] = -1; }
}
static void voice_reset(ar_voice_state *s, unsigned head) { memset(s, 0, sizeof *s); s->current = head & 3; decoder_reset(s); }
static void delay_free(ar_delay *d) { free(d->data); memset(d, 0, sizeof *d); }
static void reverb_free(ar_reverb *r) {
    for (int c = 0; c < 6; c++) {
        delay_free(&r->pre[c]); delay_free(&r->early[c]);
        for (int k = 0; k < 4; k++) delay_free(&r->comb[c][k]);
        for (int k = 0; k < 2; k++) delay_free(&r->allpass[c][k]);
    }
    memset(r, 0, sizeof *r);
}
void ar_dsp_destroy(ar_dsp *s) {
    if (!s) return;
    if (s->reverb) for (unsigned i = 0; i < s->effects; i++) reverb_free(s->reverb + i);
    free(s->pool); free(s->channel); free(s->voice); free(s->state); free(s->mix); free(s->effect);
    free(s->sink); free(s->reverb); free(s->buffers); free(s->scratch); free(s->sink_offset); free(s);
}
ar_dsp *ar_dsp_create(const uint8_t *p, size_t n) {
    if (!p || n < 0x34) return NULL;
    uint32_t a[13]; memcpy(a, p, sizeof a);
    if ((a[12] != 0x31564552 && a[12] != 0x32564552) || (a[0] != 32000 && a[0] != 48000) ||
        !a[1] || a[1] > 1024 || !a[4] || a[4] > 1024 || a[3] > 255 || a[6] > 256 || a[5] > 256 || a[9]) return NULL;
    ar_dsp *s = calloc(1, sizeof *s); if (!s) return NULL;
    s->revision = a[12]; s->rate = a[0]; s->samples = a[1]; s->voices = a[4]; s->mixes = a[3] + 1; s->effects = a[6]; s->sinks = a[5];
    s->pool_count = 4 * s->voices + s->effects;
    s->pool = calloc(s->pool_count, sizeof *s->pool); s->channel = calloc(s->voices, sizeof *s->channel);
    s->voice = calloc(s->voices, sizeof *s->voice); s->state = calloc(s->voices, sizeof *s->state);
    s->mix = calloc(s->mixes, sizeof *s->mix); s->effect = calloc(s->effects ? s->effects : 1, sizeof *s->effect);
    s->sink = calloc(s->sinks ? s->sinks : 1, sizeof *s->sink); s->reverb = calloc(s->effects ? s->effects : 1, sizeof *s->reverb);
    s->buffers = calloc((size_t)s->mixes * AR_MIX_CHANNELS * s->samples, sizeof(float)); s->scratch = calloc(AR_MIX_CHANNELS * s->samples, sizeof(float));
    s->sink_offset = calloc(s->sinks ? s->sinks : 1, sizeof *s->sink_offset);
    if (!s->pool || !s->channel || !s->voice || !s->state || !s->mix || !s->effect || !s->sink || !s->reverb || !s->buffers || !s->scratch || !s->sink_offset) { ar_dsp_destroy(s); return NULL; }
    for (unsigned i = 0; i < s->voices; i++) voice_reset(s->state + i, 0);
    return s;
}
static int pool_contains(const ar_dsp *s, uint64_t addr, uint64_t n) {
    if (!n) return 1;
    if (!addr || addr + n < addr) return 0;
    for (unsigned i = 0; i < s->pool_count; i++) {
        const ar_pool *p = s->pool + i;
        if ((p->state == 4 || p->state == 5) && addr >= p->address && n <= p->size && addr - p->address <= p->size - n) return 1;
    }
    return 0;
}

static int wave_frames(const ar_voice *v, const ar_wave *w) {
    static const unsigned bytes[] = {0, 1, 2, 3, 4, 4};
    uint64_t n = v->format == 6 ? w->size / (8 * v->channels) * 14 : w->size / (bytes[v->format] * v->channels);
    return n > 0x7fffffff ? 0x7fffffff : (int)n;
}
static int wave_end(const ar_voice *v, const ar_wave *w) { int n = wave_frames(v, w); return w->end > 0 && w->end < n ? w->end : n; }
static float pcm_value(const ar_voice *v, const ar_wave *w, int frame, int channel) {
    const uint8_t *p = (const uint8_t *)(uintptr_t)w->address; size_t at = (size_t)frame * v->channels + channel;
    if (v->format == 1) return (float)((const int8_t *)p)[at] * 256;
    if (v->format == 2) { int16_t value; memcpy(&value, p + 2 * at, 2); return value; }
    if (v->format == 3) { p += 3 * at; int32_t value = (int32_t)((uint32_t)p[0] << 8 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 24); return value / 65536.0f; }
    if (v->format == 4) { int32_t value; memcpy(&value, p + 4 * at, 4); return value / 65536.0f; }
    return f32_at(p + 4 * at) * 32768;
}
static float adpcm_value(ar_voice_state *s, const ar_voice *v, const ar_wave *w, int frame, int channel) {
    ar_adpcm *d = s->adpcm + channel; int block = frame / 14;
    if (d->block[block & 1] == block && frame < d->next_sample) return d->samples[block & 1][frame % 14];
    if (frame < d->next_sample) { memset(d, 0, sizeof *d); d->block[0] = d->block[1] = -1; }
    if (!d->initialized) {
        d->initialized = 1;
        if (w->context && w->context_size >= (uint64_t)(channel + 1) * 6) {
            const uint8_t *context = (const uint8_t *)(uintptr_t)w->context + channel * 6;
            // An explicit context is the history immediately before start, not
            // a history at sample zero. Its predictor applies to the first partial block.
            d->next_sample = w->start; memcpy(&d->index, context, 2);
            memcpy(&d->h1, context + 2, 2); memcpy(&d->h2, context + 4, 2);
        }
    }
    const int16_t *coefficients = (const int16_t *)(uintptr_t)v->extra + channel * 16;
    while (d->next_sample <= frame) {
        int current = d->next_sample / 14, i = d->next_sample % 14, slot = current & 1;
        const uint8_t *p = (const uint8_t *)(uintptr_t)w->address + ((size_t)current * v->channels + channel) * 8;
        if (!i || !w->context) d->index = p[0];
        unsigned predictor = d->index >> 4, scale = 1u << (d->index & 15);
        int c0 = predictor < 8 ? coefficients[2 * predictor] : 0, c1 = predictor < 8 ? coefficients[2 * predictor + 1] : 0;
        int nibble = p[1 + i / 2] >> (i & 1 ? 0 : 4) & 15; if (nibble & 8) nibble -= 16;
        int64_t raw = (int64_t)nibble * scale * 2048 + (int64_t)c0 * d->h1 + (int64_t)c1 * d->h2 + 1024;
        raw >>= 11; int16_t value = raw < -32768 ? -32768 : raw > 32767 ? 32767 : (int16_t)raw;
        d->h2 = d->h1; d->h1 = value; d->samples[slot][i] = value; d->next_sample++;
        d->block[slot] = current;
    }
    return d->samples[block & 1][frame % 14];
}
static int voice_position(ar_voice_state *s, const ar_voice *v) {
    for (int attempt = 0; attempt < 4; attempt++) {
        int slot = s->current;
        if (!s->pending[slot]) return 0;
        ar_wave *w = s->wave + slot; int end = wave_end(v, w), start = w->start > 0 ? w->start : 0;
        if (!s->positioned) { s->position = start; s->positioned = 1; decoder_reset(s); }
        if (s->position < end && w->address) return 1;
        if (w->loop && end > start && w->address) { s->position = start + fmod(s->position - start, end - start); decoder_reset(s); return 1; }
        double remainder = end > start ? s->position - end : 0;
        s->pending[slot] = 0; s->consumed++; s->current = (slot + 1) & 3; s->positioned = 0;
        if (s->pending[s->current]) {
            int next_start = s->wave[s->current].start > 0 ? s->wave[s->current].start : 0;
            s->position = next_start + fmax(0, remainder); s->positioned = 1; decoder_reset(s);
        }
    }
    return 0;
}
static float voice_filter(ar_voice_state *s, const ar_voice *v, unsigned channel, float value) {
    for (int i = 0; i < 2; i++) if (v->biquad[i].enabled) {
        const ar_biquad *b = v->biquad + i; float *h = s->filter[channel][i];
        float output = (b->numerator[0] * value + b->numerator[1] * h[0] + b->numerator[2] * h[1] - b->denominator[0] * h[2] - b->denominator[1] * h[3]) / 16384;
        h[1] = h[0]; h[0] = value; h[3] = h[2]; h[2] = isfinite(output) ? output : 0; value = h[2];
    }
    return value;
}
static float *mix_buffer(ar_dsp *s, unsigned mix, unsigned channel) { return s->buffers + ((size_t)mix * AR_MIX_CHANNELS + channel) * s->samples; }
static void render_voices(ar_dsp *s) {
    for (unsigned i = 0; i < s->voices; i++) {
        const ar_voice *v = s->voice + i; ar_voice_state *state = s->state + i;
        if (!v->used || v->state != 0) continue;
        double step = (double)v->rate / s->rate * v->pitch;
        for (unsigned t = 0; t < s->samples && voice_position(state, v); t++) {
            ar_wave *w = state->wave + state->current; int at = (int)state->position, end = wave_end(v, w); float fraction = (float)(state->position - at);
            for (unsigned c = 0; c < v->channels; c++) {
                float a = v->format == 6 ? adpcm_value(state, v, w, at, c) : pcm_value(v, w, at, c);
                float b = a;
                if (at + 1 < end) b = v->format == 6 ? adpcm_value(state, v, w, at + 1, c) : pcm_value(v, w, at + 1, c);
                float sample = voice_filter(state, v, c, a + fraction * (b - a)) * finite_gain(v->volume);
                if (v->mix >= s->mixes || !s->mix[v->mix].used) continue;
                const ar_channel *channel = s->channel + v->channel[c];
                for (unsigned out = 0; out < s->mix[v->mix].buffers; out++) if (channel->volume[out]) mix_buffer(s, v->mix, out)[t] += sample * finite_gain(channel->volume[out]);
            }
            double next = state->position + step;
            state->played += (uint64_t)fmax(0, floor(fmin(next, end)) - floor(state->position));
            state->position = next;
        }
        voice_position(state, v);   // a buffer ending exactly at the frame boundary is released now
    }
}
static void effect_mix(ar_dsp *s, const ar_effect *e) {
    unsigned count = u32_at(e->data + 0x90); if (count > 24) return;
    uint8_t touched[24] = {0};
    for (unsigned i = 0; i < count; i++) {
        unsigned input = e->data[i], output = e->data[0x18 + i];
        if (input >= s->mix[e->mix].buffers || output >= s->mix[e->mix].buffers) return;
        float gain = f32_at(e->data + 0x30 + 4 * i);
        for (unsigned t = 0; t < s->samples; t++) s->scratch[(size_t)i * s->samples + t] = mix_buffer(s, e->mix, input)[t] * gain;
        touched[output] = 1;
    }
    for (unsigned c = 0; c < 24; c++) if (touched[c]) memset(mix_buffer(s, e->mix, c), 0, s->samples * sizeof(float));
    for (unsigned i = 0; i < count; i++) for (unsigned t = 0; t < s->samples; t++) mix_buffer(s, e->mix, e->data[0x18 + i])[t] += s->scratch[(size_t)i * s->samples + t];
}
static void effect_aux(ar_dsp *s, const ar_effect *e) {
    unsigned channels = u32_at(e->data + 0x30), count = u32_at(e->data + 0x38);
    if (!channels || channels > 24 || !count) return;
    uint64_t si = u64_at(e->data + 0x40), sd = u64_at(e->data + 0x48), ri = u64_at(e->data + 0x50), rd = u64_at(e->data + 0x58);
    if (!pool_contains(s, si, 0x80) || !pool_contains(s, sd, (uint64_t)count * 4) || !pool_contains(s, ri, 0x80) || !pool_contains(s, rd, (uint64_t)count * 4)) return;
    // AuxBufferInfo has separate 64-byte client/server cache lines. The SDK's
    // ReadAuxBuffer consumes +0/+4; WriteAuxBuffer publishes +0x44. A render
    // frame writes/reads channel-major int32 samples, then publishes its cursor.
    uint32_t *send = (uint32_t *)(uintptr_t)si, *ret = (uint32_t *)(uintptr_t)ri;
    int32_t *send_data = (int32_t *)(uintptr_t)sd, *return_data = (int32_t *)(uintptr_t)rd;
    unsigned write = __atomic_load_n(send + 0x44 / 4, __ATOMIC_ACQUIRE) % count;
    unsigned read = __atomic_load_n(send, __ATOMIC_ACQUIRE) % count;
    unsigned room = (read + count - write - 1) % count, requested = channels * s->samples, sent = room < requested ? 0 : requested;
    for (unsigned k = 0; k < sent; k++) {
        unsigned c = k / s->samples, t = k % s->samples, input = e->data[c];
        float value = input < s->mix[e->mix].buffers ? mix_buffer(s, e->mix, input)[t] : 0;
        send_data[(write + k) % count] = !isfinite(value) ? 0 : value > 2147483647.0 ? 2147483647 : value < -2147483648.0 ? (-2147483647 - 1) : (int32_t)lrintf(value);
    }
    __atomic_store_n(send + 0x40 / 4, read, __ATOMIC_RELEASE);
    __atomic_store_n(send + 0x44 / 4, (write + sent) % count, __ATOMIC_RELEASE);
    read = __atomic_load_n(ret + 0x40 / 4, __ATOMIC_ACQUIRE) % count;
    write = __atomic_load_n(ret + 0x44 / 4, __ATOMIC_ACQUIRE) % count;
    unsigned available = (write + count - read) % count, got = available < requested ? 0 : requested;
    for (unsigned k = 0; k < requested; k++) {
        unsigned c = k / s->samples, t = k % s->samples, output = e->data[0x18 + c];
        if (output < s->mix[e->mix].buffers) mix_buffer(s, e->mix, output)[t] = k < got ? (float)return_data[(read + k) % count] : 0;
    }
    __atomic_store_n(ret + 0x40 / 4, (read + got) % count, __ATOMIC_RELEASE);
}
static float bounded(float value, float lo, float hi) { return value < lo ? lo : value > hi ? hi : value; }
static int delay_size(ar_delay *d, int length) {
    if (length < 1) length = 1;
    if (d->length == length) return 1;
    float *data = calloc((size_t)length, sizeof(float)); if (!data) return 0;
    free(d->data); d->data = data; d->length = length; d->at = 0; d->low = 0; return 1;
}
static float delay_sample(ar_delay *d, float input) {
    float output = d->data[d->at]; d->data[d->at] = input;
    if (++d->at == d->length) d->at = 0;
    return output;
}
static void effect_reverb(ar_dsp *s, unsigned index) {
    const ar_effect *e = s->effect + index; const uint8_t *p = e->data;
    uint16_t channels; memcpy(&channels, p + 0xe, 2);
    if (!channels || channels > 6) return;
    ar_reverb *r = s->reverb + index;
    // I3DL2-compatible delay network. Public gains, delays, T60, damping,
    // diffusion and density are applied explicitly. Nintendo's exact delay-line
    // topology is not assumed; impulse matching remains a separate validation.
    float room = powf(10, bounded(f32_at(p + 0x18), -10000, 0) / 2000);
    float hf_ref = bounded(f32_at(p + 0x1c), 20, s->rate * 0.49f);
    float decay = bounded(f32_at(p + 0x20), 0.1f, 20), ratio = bounded(f32_at(p + 0x24), 0.1f, 2);
    float room_hf = powf(10, bounded(f32_at(p + 0x28), -10000, 0) / 2000);
    float early_gain = powf(10, bounded(f32_at(p + 0x2c), -10000, 2000) / 2000);
    float late_gain = powf(10, bounded(f32_at(p + 0x30), -10000, 2000) / 2000);
    float diffusion = bounded(f32_at(p + 0x34), 0, 100) / 100;
    float early_seconds = bounded(f32_at(p + 0x38), 0, 0.3f), late_seconds = bounded(f32_at(p + 0x3c), 0, 0.1f);
    float density = bounded(f32_at(p + 0x40), 0, 100) / 100, dry = f32_at(p + 0x44);
    float input_alpha = expf(-6.28318530718f * hf_ref / s->rate), damp = expf(-6.28318530718f * hf_ref * ratio / s->rate);
    static const int comb_frames[4] = {1493, 1601, 1747, 1867}, pass_frames[2] = {211, 73};
    for (unsigned c = 0; c < channels; c++) {
        unsigned input = p[c], output = p[6 + c];
        if (input >= s->mix[e->mix].buffers || output >= s->mix[e->mix].buffers) return;
        if (!delay_size(&r->pre[c], (int)(late_seconds * s->rate)) || !delay_size(&r->early[c], (int)(early_seconds * s->rate))) return;
        for (int k = 0; k < 4; k++) if (!delay_size(&r->comb[c][k], (int)((comb_frames[k] + 23 * c) * (s->rate / 48000.0f) * (0.5f + 0.5f * density)))) return;
        for (int k = 0; k < 2; k++) if (!delay_size(&r->allpass[c][k], (int)((pass_frames[k] + 7 * c) * (s->rate / 48000.0f)))) return;
        float *src = mix_buffer(s, e->mix, input); memcpy(s->scratch + (size_t)c * s->samples, src, s->samples * sizeof(float));
    }
    for (unsigned c = 0; c < channels; c++) {
        float *dst = mix_buffer(s, e->mix, p[6 + c]); const float *src = s->scratch + (size_t)c * s->samples;
        float feedback[4]; for (int k = 0; k < 4; k++) feedback[k] = powf(10, -3.0f * r->comb[c][k].length / (s->rate * decay));
        for (unsigned t = 0; t < s->samples; t++) {
            float sample = src[t]; r->low[c] = input_alpha * r->low[c] + (1 - input_alpha) * sample;
            float shaped = (r->low[c] + room_hf * (sample - r->low[c])) * room;
            float early = early_seconds > 0 ? delay_sample(&r->early[c], shaped) : shaped;
            float feed = late_seconds > 0 ? delay_sample(&r->pre[c], shaped) : shaped, late = 0;
            for (int k = 0; k < 4; k++) {
                ar_delay *line = &r->comb[c][k]; float value = line->data[line->at];
                line->low = damp * line->low + (1 - damp) * value;
                delay_sample(line, feed + line->low * feedback[k]); late += value * 0.25f;
            }
            for (int k = 0; k < 2; k++) {
                ar_delay *line = &r->allpass[c][k]; float value = line->data[line->at], gain = 0.5f * diffusion;
                delay_sample(line, late + value * gain); late = value - late * gain;
            }
            dst[t] = finite_gain(dry * sample + early_gain * early + late_gain * late);
        }
    }
}
static void render_effects(ar_dsp *s, unsigned mix) {
    for (unsigned order = 0; order < s->effects; order++) for (unsigned i = 0; i < s->effects; i++) {
        const ar_effect *e = s->effect + i;
        if (!e->type || !e->enabled || e->mix != mix || e->order != order) continue;
        if (e->type == 1) effect_mix(s, e);
        else if (e->type == 2) effect_aux(s, e);
        else if (e->type == 5) effect_reverb(s, i);
    }
}
static int graph_order(const ar_mix *mix, unsigned count, unsigned *order) {
    unsigned degree[256] = {0}, written = 0; uint8_t done[256] = {0};
    for (unsigned i = 0; i < count; i++) if (mix[i].used && mix[i].destination < count && mix[i].destination != i) degree[mix[i].destination]++;
    while (written < count) {
        unsigned i = 0; while (i < count && (done[i] || degree[i])) i++;
        if (i == count) return 0;
        done[i] = 1; order[written++] = i;
        if (mix[i].used && mix[i].destination < count && mix[i].destination != i) degree[mix[i].destination]--;
    }
    return 1;
}
static void render_mixes(ar_dsp *s) {
    unsigned order[256]; if (!graph_order(s->mix, s->mixes, order)) return;
    for (unsigned k = 0; k < s->mixes; k++) {
        unsigned id = order[k]; const ar_mix *mix = s->mix + id;
        if (!mix->used) continue;
        render_effects(s, id);
        for (unsigned c = 0; c < mix->buffers; c++) {
            float *data = mix_buffer(s, id, c); for (unsigned t = 0; t < s->samples; t++) data[t] *= finite_gain(mix->volume);
        }
        unsigned to = mix->destination;
        if (to >= s->mixes || to == id || !s->mix[to].used) continue;
        for (unsigned in = 0; in < mix->buffers; in++) for (unsigned out = 0; out < s->mix[to].buffers; out++) if (mix->matrix[in][out]) {
            float gain = finite_gain(mix->matrix[in][out]), *src = mix_buffer(s, id, in), *dst = mix_buffer(s, to, out);
            for (unsigned t = 0; t < s->samples; t++) dst[t] += src[t] * gain;
        }
    }
}
int ar_dsp_render(ar_dsp *s, int16_t *stereo, size_t capacity_frames) {
    if (!s || !stereo) return 0;
    unsigned frames = (unsigned)((uint64_t)s->samples * 48000 / s->rate);
    if (capacity_frames < frames) return 0;
    memset(s->buffers, 0, (size_t)s->mixes * AR_MIX_CHANNELS * s->samples * sizeof(float));
    render_voices(s); render_mixes(s);
    memset(stereo, 0, frames * 2 * sizeof(int16_t));
    for (unsigned i = 0; i < s->sinks; i++) {
        const ar_sink *sink = s->sink + i;
        if (!sink->used || sink->type != 1) continue;
        unsigned count = u32_at(sink->data + 0x100); if (!count || count > 6) continue;
        float front = 1, center = 0.70710678f, rear = 0.70710678f, lfe = 0;
        if (sink->data[0x10b]) { front = f32_at(sink->data + 0x10c); center = f32_at(sink->data + 0x110); rear = f32_at(sink->data + 0x114); lfe = f32_at(sink->data + 0x118); }
        for (unsigned t = 0; t < frames; t++) {
            double at = (double)t * s->rate / 48000; unsigned first = (unsigned)at, next = first + 1 < s->samples ? first + 1 : first; float fraction = (float)(at - first), channel[6] = {0};
            for (unsigned c = 0; c < count; c++) {
                unsigned index = sink->data[0x104 + c]; if (index >= s->mix[0].buffers) continue;
                float *data = mix_buffer(s, 0, index); channel[c] = data[first] + fraction * (data[next] - data[first]);
            }
            float left = channel[0], right = count == 1 ? left : channel[1];
            if (count > 2) { left = front * left + center * channel[2] + lfe * channel[3] + rear * channel[4]; right = front * right + center * channel[2] + lfe * channel[3] + rear * channel[5]; }
            stereo[2 * t] = clip16(left); stereo[2 * t + 1] = clip16(right);
        }
        break;
    }
    s->rendered++;
    return (int)frames;
}
static size_t output_bytes(const ar_dsp *s) { return 0x40 + (size_t)s->pool_count * 0x10 + (size_t)s->voices * 0x10 + (size_t)s->effects * 0x10 + (size_t)s->sinks * 0x20 + 0x10 + 0xb0; }
uint32_t ar_dsp_update(ar_dsp *s, const void *input, size_t input_size, void *output, size_t output_size) {
    if (!s || !input || !output || input_size < sizeof(ar_header) || output_size < output_bytes(s)) return AR_BAD;
    ar_header h; memcpy(&h, input, sizeof h);
    if (h.revision != s->revision || h.total > input_size || h.total < sizeof h || h.behavior != 0x10 ||
        h.pools != s->pool_count * sizeof(ar_pool) || h.channels != s->voices * sizeof(ar_channel) || h.voices != s->voices * sizeof(ar_voice) ||
        h.effects != s->effects * sizeof(ar_effect) || h.mixes != s->mixes * sizeof(ar_mix) || h.sinks != s->sinks * sizeof(ar_sink) || h.performance != 0x10) return AR_BAD;
    for (unsigned i = 0; i < 6; i++) if (h.reserved[i]) return AR_BAD;   // splitter packets need their own format
    size_t sum = sizeof h + (size_t)h.behavior + h.pools + h.channels + h.voices + h.effects + h.mixes + h.sinks + h.performance;
    if (sum != h.total) return AR_BAD;
    const uint8_t *p = (const uint8_t *)input + sizeof h + h.behavior;
    const ar_pool *pools = (const ar_pool *)p; p += h.pools;
    const ar_channel *channels = (const ar_channel *)p; p += h.channels;
    const ar_voice *voices = (const ar_voice *)p; p += h.voices;
    const ar_effect *effects = (const ar_effect *)p; p += h.effects;
    const ar_mix *mixes = (const ar_mix *)p; p += h.mixes;
    const ar_sink *sinks = (const ar_sink *)p;
    ar_dsp view = *s; view.pool = (ar_pool *)pools;
    for (unsigned i = 0; i < s->voices; i++) {
        const ar_voice *v = voices + i;
        if (v->used && (v->channels > 6 || !v->channels || v->format < 1 || v->format > 6 || !v->rate || !isfinite(v->pitch) || v->pitch <= 0)) return AR_BAD;
        if (v->used) for (unsigned c = 0; c < v->channels; c++) if (v->channel[c] >= s->voices) return AR_BAD;
        if (v->used) for (unsigned k = 0; k < 4; k++) {
            const ar_wave *w = !v->wave[k].sent ? v->wave + k : (!v->is_new && s->state[i].pending[k] ? s->state[i].wave + k : NULL);
            if (!w || (!w->address && !w->eos)) continue;
            if (!pool_contains(&view, w->address, w->size) || w->start < 0 || w->end < 0 || w->start > wave_frames(v, w) || (w->end && w->end < w->start)) return AR_BAD;
            if (w->context && !pool_contains(&view, w->context, w->context_size)) return AR_BAD;
            if (v->format == 6 && (!pool_contains(&view, v->extra, 32 * v->channels) || v->extra_size < 32 * v->channels)) return AR_BAD;
        }
    }
    for (unsigned i = 0; i < s->mixes; i++) if (mixes[i].used && (mixes[i].buffers > AR_MIX_CHANNELS || mixes[i].rate != s->rate || (i && mixes[i].destination == i))) return AR_BAD;
    unsigned order[256]; if (!graph_order(mixes, s->mixes, order)) return AR_BAD;
    for (unsigned i = 0; i < s->effects; i++) {
        if (effects[i].type && effects[i].type != 1 && effects[i].type != 2 && effects[i].type != 5) return AR_BAD;
        if (effects[i].type && effects[i].mix >= s->mixes) return AR_BAD;
        if (!effects[i].type || !effects[i].enabled) continue;
        const uint8_t *params = effects[i].data; unsigned count, input_offset = 0, output_offset = 0x18;
        if (effects[i].type == 1) count = u32_at(params + 0x90);
        else if (effects[i].type == 2) {
            count = u32_at(params + 0x30); uint64_t samples = u32_at(params + 0x38);
            if (!samples || !pool_contains(&view, u64_at(params + 0x40), 0x80) || !pool_contains(&view, u64_at(params + 0x48), samples * 4) ||
                !pool_contains(&view, u64_at(params + 0x50), 0x80) || !pool_contains(&view, u64_at(params + 0x58), samples * 4)) return AR_BAD;
        } else { uint16_t n; memcpy(&n, params + 0xe, 2); count = n; output_offset = 6; if (count > 6) return AR_BAD; }
        if (count > 24 || effects[i].order >= s->effects) return AR_BAD;
        for (unsigned c = 0; c < count; c++) if (params[input_offset + c] >= mixes[effects[i].mix].buffers || params[output_offset + c] >= mixes[effects[i].mix].buffers) return AR_BAD;
    }
    for (unsigned i = 0; i < s->sinks; i++) if (sinks[i].used) {
        unsigned count = u32_at(sinks[i].data + 0x100); if (sinks[i].type != 1 || !count || count > 6) return AR_BAD;
        for (unsigned c = 0; c < count; c++) if (sinks[i].data[0x104 + c] >= mixes[0].buffers) return AR_BAD;
    }
    memcpy(s->pool, pools, h.pools); memcpy(s->channel, channels, h.channels);
    for (unsigned i = 0; i < s->voices; i++) {
        const ar_voice *v = voices + i; ar_voice_state *state = s->state + i;
        if (!v->used || v->is_new || !s->voice[i].used) voice_reset(state, v->wave_head);
        if (v->used) for (unsigned k = 0; k < 4; k++) if (!v->wave[k].sent && (v->wave[k].address || v->wave[k].eos)) { state->wave[k] = v->wave[k]; state->pending[k] = 1; }
    }
    for (unsigned i = 0; i < s->effects; i++) if (effects[i].type == 5 && (effects[i].is_new || s->effect[i].type != 5)) reverb_free(s->reverb + i);
    memcpy(s->voice, voices, h.voices); memcpy(s->effect, effects, h.effects); memcpy(s->mix, mixes, h.mixes); memcpy(s->sink, sinks, h.sinks);
    ar_header result = {0}; result.revision = s->revision; result.behavior = 0xb0; result.pools = s->pool_count * 0x10;
    result.voices = s->voices * 0x10; result.effects = s->effects * 0x10; result.sinks = s->sinks * 0x20; result.performance = 0x10; result.total = (uint32_t)output_bytes(s);
    memset(output, 0, result.total); memcpy(output, &result, sizeof result); uint8_t *o = (uint8_t *)output + sizeof result;
    for (unsigned i = 0; i < s->pool_count; i++, o += 0x10) {
        uint32_t state = s->pool[i].state; if (state == 4) state = 5; else if (state == 2) state = 3; memcpy(o, &state, 4);
    }
    for (unsigned i = 0; i < s->voices; i++, o += 0x10) { memcpy(o, &s->state[i].played, 8); memcpy(o + 8, &s->state[i].consumed, 4); }
    // SDK EffectInfo::OutStatus at +0xc0: 1/4 are removable when disabled.
    for (unsigned i = 0; i < s->effects; i++, o += 0x10) o[0] = s->effect[i].type ? (s->effect[i].enabled ? 3 : 4) : 0;
    for (unsigned i = 0; i < s->sinks; i++, o += 0x20) memcpy(o, &s->sink_offset[i], 4);
    return 0;
}
