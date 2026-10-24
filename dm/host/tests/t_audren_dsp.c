// End-to-end REV2 packet tests with synthetic audio, no game data or platform APIs.
#include "../legacy_audio_dsp.h"
#include "../legacy_audio_protocol.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum { VOICES = 2, EFFECTS = 3, POOLS = VOICES * 4 + EFFECTS };
typedef struct {
    ar_header header; uint8_t behavior[16]; ar_pool pool[POOLS];
    ar_channel channel[VOICES]; ar_voice voice[VOICES]; ar_effect effect[EFFECTS];
    ar_mix mix[2]; ar_sink sink; uint8_t performance[16];
} packet;
typedef struct { ar_dsp *dsp; packet p; uint8_t arena[65536], output[1024]; int16_t pcm[3072]; unsigned samples; } fixture;
static int checks, failures;
#define CHECK(c, msg) do { checks++; if (!(c)) { failures++; fprintf(stderr, "FAIL: %s (line %d)\n", msg, __LINE__); } } while (0)
static void put32(void *p, uint32_t v) { memcpy(p, &v, 4); }
static void put64(void *p, uint64_t v) { memcpy(p, &v, 8); }
static void putf(void *p, float v) { memcpy(p, &v, 4); }
static uint32_t get32(const void *p) { uint32_t v; memcpy(&v, p, 4); return v; }
static uint64_t get64(const void *p) { uint64_t v; memcpy(&v, p, 8); return v; }
static fixture *create(unsigned samples) {
    fixture *f = calloc(1, sizeof *f); if (!f) exit(2);
    uint32_t parameter[13] = {48000, samples, 26, 1, VOICES, 1, EFFECTS, 0, 0, 0, 0, 0, 0x32564552};
    f->samples = samples; f->dsp = ar_dsp_create((const uint8_t *)parameter, sizeof parameter);
    if (!f->dsp) exit(2);
    packet *p = &f->p;
    p->header = (ar_header){.revision=parameter[12], .behavior=16, .pools=sizeof p->pool, .voices=sizeof p->voice,
        .channels=sizeof p->channel, .effects=sizeof p->effect, .mixes=sizeof p->mix, .sinks=sizeof p->sink, .performance=16, .total=sizeof *p};
    put32(p->behavior, parameter[12]);
    p->pool[0] = (ar_pool){.address=(uintptr_t)f->arena, .size=sizeof f->arena, .state=4};
    for (unsigned i = 0; i < VOICES; i++) {
        p->channel[i].id = i; p->channel[i].used = 1;
        p->voice[i].id = i; p->voice[i].pitch = 1; p->voice[i].volume = 1;
        for (unsigned w = 0; w < 4; w++) p->voice[i].wave[w].sent = 1;
    }
    p->channel[0].volume[0] = p->channel[0].volume[1] = 1;
    p->channel[1].volume[1] = 1;
    for (unsigned i = 0; i < 2; i++) p->mix[i] = (ar_mix){.volume=1, .rate=48000, .buffers=6, .used=i == 0, .id=i, .destination=0x7fffffff, .splitter=0xffffffff};
    p->sink.type = p->sink.used = 1; put32(p->sink.data + 0x100, 2); p->sink.data[0x105] = 1;
    return f;
}
static void destroy(fixture *f) { ar_dsp_destroy(f->dsp); free(f); }
static void wave(fixture *f, unsigned format, unsigned channels, unsigned bytes, unsigned frames) {
    ar_voice *v = f->p.voice; v->used = v->is_new = 1; v->state = 0; v->format = format; v->channels = channels;
    v->rate = 48000; v->wave_count = 1; v->channel[1] = 1;
    v->wave[0] = (ar_wave){.address=(uintptr_t)f->arena, .size=bytes, .end=frames};
}
static void submit(fixture *f) {
    CHECK(!ar_dsp_update(f->dsp, &f->p, sizeof f->p, f->output, sizeof f->output), "valid update accepted");
    for (unsigned i = 0; i < VOICES; i++) { f->p.voice[i].is_new = 0; for (int k = 0; k < 4; k++) f->p.voice[i].wave[k].sent = 1; }
    for (unsigned i = 0; i < EFFECTS; i++) f->p.effect[i].is_new = 0;
}
static void render(fixture *f) { CHECK(ar_dsp_render(f->dsp, f->pcm, 1536) == (int)f->samples, "PCM frame size follows renderer parameter"); }
static void counters(fixture *f, uint64_t played, uint32_t consumed) {
    submit(f); const uint8_t *out = f->output + 0x40 + POOLS * 0x10;
    CHECK(get64(out) == played && get32(out + 8) == consumed, "played samples and consumed wave buffers reported");
}
static void test_pcm(void) {
    fixture *f = create(8); int16_t input[] = {-1000, 2000, 32000, -32000}; memcpy(f->arena, input, sizeof input);
    wave(f, 2, 1, sizeof input, 4); f->p.voice[0].volume = 0.5f; f->p.channel[0].volume[0] = 2;
    submit(f); CHECK(get32(f->output + 0x40) == 5, "pool attachment acknowledged"); render(f);
    for (int i = 0; i < 4; i++) CHECK(f->pcm[2*i] == input[i] && f->pcm[2*i+1] == input[i]/2, "voice and channel gain applied independently");
    CHECK(!f->pcm[8] && !f->pcm[9], "completed wave pads remaining frame with silence");
    counters(f, 4, 1); render(f); CHECK(!f->pcm[0], "sent buffers are never replayed by later updates");
    f->p.pool[0].state = 2; submit(f); CHECK(get32(f->output + 0x40) == 3, "pool detach acknowledged"); destroy(f);

    f = create(8); int16_t stereo[] = {100, -300, 30000, -30000}; memcpy(f->arena, stereo, sizeof stereo);
    wave(f, 2, 2, sizeof stereo, 2); f->p.channel[0].volume[1] = 0; f->p.voice[0].volume = 2;
    submit(f); render(f); CHECK(f->pcm[0] == 200 && f->pcm[1] == -600 && f->pcm[2] == 32767 && f->pcm[3] == -32768, "interleaved stereo decoded with saturating output"); destroy(f);
    for (unsigned format = 1; format <= 5; format++) {
        if (format == 2) continue;
        f = create(8); unsigned bytes = format == 1 ? 1 : format == 3 ? 3 : 4;
        if (format == 1) f->arena[0] = 0x80;
        else if (format == 3) f->arena[2] = 0x80;
        else if (format == 4) put32(f->arena, 0x80000000);
        else putf(f->arena, -1);
        wave(f, format, 1, bytes, 1); submit(f); render(f);
        CHECK(f->pcm[0] == -32768 && f->pcm[1] == -32768, "PCM8/24/32/float full negative scale"); destroy(f);
    }
}
static void test_queue(void) {
    fixture *f = create(8); int16_t input[] = {100, 200}; memcpy(f->arena, input, sizeof input);
    wave(f, 2, 1, sizeof input, 2); f->p.voice[0].wave[0].loop = 1;
    submit(f); render(f); for (int i = 0; i < 8; i++) CHECK(f->pcm[2*i] == input[i&1], "loop repeats within frame");
    counters(f, 8, 0); f->p.voice[0].state = 2; submit(f); render(f); CHECK(!f->pcm[0], "paused voice outputs silence"); counters(f, 8, 0);
    f->p.voice[0].state = 0; submit(f); render(f); CHECK(f->pcm[0] == 100, "resume keeps loop position"); destroy(f);
    f = create(8); int16_t ramp[] = {0, 1000, 2000, 3000}; memcpy(f->arena, ramp, sizeof ramp);
    wave(f, 2, 1, sizeof ramp, 4); f->p.voice[0].rate = 24000;
    submit(f); render(f); CHECK(f->pcm[0] == 0 && f->pcm[2] == 500 && f->pcm[4] == 1000 && f->pcm[10] == 2500, "source sample rate resampled by interpolation"); counters(f, 4, 1); destroy(f);
    f = create(8); memcpy(f->arena, ramp, sizeof ramp); wave(f, 2, 1, 4, 2);
    f->p.voice[0].wave[1] = (ar_wave){.address=(uintptr_t)(f->arena+4), .size=4, .end=2}; f->p.voice[0].wave_count = 2;
    submit(f); render(f); CHECK(f->pcm[0] == 0 && f->pcm[2] == 1000 && f->pcm[4] == 2000 && f->pcm[6] == 3000, "successive wave buffers join without a silent gap"); counters(f, 4, 2); destroy(f);
}
static void test_adpcm(void) {
    fixture *f = create(28); f->arena[0] = f->arena[8] = 0;
    memset(f->arena+1, 0x11, 7); memset(f->arena+9, 0x22, 7);
    int16_t coefficients[16] = {2048}; memcpy(f->arena+32768, coefficients, sizeof coefficients);
    uint16_t context[3] = {0, 100, 0}; memcpy(f->arena+32800, context, sizeof context);
    wave(f, 6, 1, 16, 28); f->p.voice[0].extra = (uintptr_t)(f->arena+32768); f->p.voice[0].extra_size = 32;
    f->p.voice[0].wave[0].context = (uintptr_t)(f->arena+32800); f->p.voice[0].wave[0].context_size = 6;
    submit(f); render(f);
    CHECK(f->pcm[0] == 101 && f->pcm[26] == 114 && f->pcm[28] == 116 && f->pcm[54] == 142, "DSP ADPCM predictor history crosses 14-sample blocks"); counters(f, 28, 1); destroy(f);
    f = create(8); memset(f->arena+1, 0x11, 7); memcpy(f->arena+32768, coefficients, sizeof coefficients); memcpy(f->arena+32800, context, sizeof context);
    wave(f, 6, 1, 8, 14); f->p.voice[0].wave[0].start = 7; f->p.voice[0].wave[0].context = (uintptr_t)(f->arena+32800); f->p.voice[0].wave[0].context_size = 6;
    f->p.voice[0].extra = (uintptr_t)(f->arena+32768); f->p.voice[0].extra_size = 32;
    submit(f); render(f); CHECK(f->pcm[0] == 101 && f->pcm[12] == 107 && !f->pcm[14], "ADPCM context begins at explicit nonzero sample offset"); destroy(f);
}
static void test_mix(void) {
    fixture *f = create(8); int16_t input = 1000; memcpy(f->arena, &input, 2); wave(f, 2, 1, 2, 1);
    f->p.voice[0].mix = 1; f->p.mix[1].used = 1; f->p.mix[1].destination = 0; f->p.mix[1].volume = 0.5f;
    f->p.mix[1].matrix[0][0] = 0.5f; f->p.mix[1].matrix[1][1] = 2;
    submit(f); render(f); CHECK(f->pcm[0] == 250 && f->pcm[1] == 1000, "submixes processed before destination with matrix and volume"); destroy(f);
    f = create(8); memcpy(f->arena, &input, 2); wave(f, 2, 1, 2, 1);
    ar_effect *e = f->p.effect; e->type = e->enabled = e->is_new = 1; e->data[0] = 0; e->data[1] = 1; e->data[0x18] = e->data[0x19] = 0;
    putf(e->data+0x30, 0.5f); putf(e->data+0x34, 0.25f); put32(e->data+0x90, 2);
    submit(f); render(f); CHECK(f->pcm[0] == 750 && f->pcm[1] == 1000, "buffer mixer snapshots source inputs and sums duplicate destinations"); destroy(f);
    f = create(8); memcpy(f->arena, &input, 2); wave(f, 2, 1, 2, 1); e = f->p.effect; e->type = e->enabled = 1;
    put32(e->data+0x90, 1); submit(f); render(f); CHECK(!f->pcm[0], "zero effect gain clears destination"); destroy(f);
    f = create(8); memcpy(f->arena, &input, 2); wave(f, 2, 1, 2, 1); f->p.voice[0].biquad[0].enabled = 1; f->p.voice[0].biquad[0].numerator[0] = 8192;
    submit(f); render(f); CHECK(f->pcm[0] == 500, "Q14 biquad numerator gain"); destroy(f);
}
static void test_aux(void) {
    fixture *f = create(8); int16_t input[] = {10,20,30,40,50,60,70,80}; memcpy(f->arena, input, sizeof input); wave(f, 2, 1, sizeof input, 8);
    uint32_t *send = (uint32_t *)(f->arena+40000), *ret = (uint32_t *)(f->arena+41024);
    int32_t *sd = (int32_t *)(f->arena+42048), *rd = (int32_t *)(f->arena+44096);
    send[0] = 80; send[17] = 124; ret[0] = 77; ret[1] = 4; ret[16] = 124; ret[17] = 4;
    for (int i = 0; i < 8; i++) rd[(124+i)%128] = 100*(i+1);
    ar_effect *e = f->p.effect; e->type = 2; e->enabled = 1; put32(e->data+0x30, 1); put32(e->data+0x34, 48000); put32(e->data+0x38, 128);
    put64(e->data+0x40, (uintptr_t)send); put64(e->data+0x48, (uintptr_t)sd); put64(e->data+0x50, (uintptr_t)ret); put64(e->data+0x58, (uintptr_t)rd);
    submit(f); render(f);
    CHECK(send[17] == 4 && send[0] == 80 && sd[124] == 10 && sd[3] == 80, "aux send ring wraps and publishes server cursor");
    CHECK(ret[16] == 4 && ret[0] == 77 && ret[1] == 4 && ret[17] == 4, "aux return consumes only server read cursor");
    CHECK(f->pcm[0] == 100 && f->pcm[14] == 800, "aux return samples reach the final mix"); destroy(f);
}
static void test_reverb(void) {
    fixture *f = create(240); int16_t impulse = 10000; memcpy(f->arena, &impulse, 2); wave(f, 2, 1, 480, 240);
    ar_effect *e = f->p.effect; e->type = 5; e->enabled = e->is_new = 1;
    e->data[0xe] = 1; put32(e->data+0x14, 48000); putf(e->data+0x1c, 5000); putf(e->data+0x20, 0.5f); putf(e->data+0x24, 0.5f);
    putf(e->data+0x34, 80); putf(e->data+0x38, 0.005f); putf(e->data+0x3c, 0.01f); putf(e->data+0x40, 60); putf(e->data+0x44, 0.5f);
    submit(f); render(f); CHECK(f->pcm[0] == 5000, "reverb dry path retains impulse");
    long long tail = 0; for (int i = 0; i < 100; i++) { render(f); for (int t = 0; t < 240; t++) tail += abs(f->pcm[2*t]); }
    CHECK(tail > 100 && tail < 10000000, "reverb has a bounded nonzero delayed tail"); destroy(f);
}
static void test_invalid(void) {
    fixture *f = create(8); int16_t input = 1234; memcpy(f->arena, &input, 2); wave(f, 2, 1, 2, 1); submit(f);
    f->p.voice[0].is_new = 1; f->p.voice[0].wave[0].address = 1; f->p.voice[0].wave[0].sent = 0;
    CHECK(ar_dsp_update(f->dsp, &f->p, sizeof f->p, f->output, sizeof f->output), "wave outside attached pools rejected");
    render(f); CHECK(f->pcm[0] == 1234, "rejected update preserves pending audio state");
    f->p.voice[0].used = 0; f->p.mix[1].used = 1; f->p.mix[1].destination = 0; f->p.mix[0].destination = 1;
    CHECK(ar_dsp_update(f->dsp, &f->p, sizeof f->p, f->output, sizeof f->output), "cyclic submix graph rejected");
    f->p.mix[0].destination = 0x7fffffff; f->p.effect[0].type = 99;
    CHECK(ar_dsp_update(f->dsp, &f->p, sizeof f->p, f->output, sizeof f->output), "unsupported effect rejected explicitly");
    f->p.effect[0].type = 0;
    CHECK(ar_dsp_update(f->dsp, &f->p, sizeof f->p-1, f->output, sizeof f->output), "truncated packet rejected");
    CHECK(ar_dsp_update(f->dsp, &f->p, sizeof f->p, f->output, 64), "undersized output rejected"); destroy(f);
}
int main(void) {
    test_pcm(); test_queue(); test_adpcm(); test_mix(); test_aux(); test_reverb(); test_invalid();
    printf("legacy audio DSP: %d checks, %d failures\n", checks, failures); return failures ? 1 : 0;
}
