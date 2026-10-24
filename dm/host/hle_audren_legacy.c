// SDK 1.6 has no ExecuteAudioRendererRendering or DFC server. Its audio renderer
// talks to audren:u. The host processes REV1/REV2 packets with a software DSP,
// paced by the same PCM output used by the SDK's newer in-process renderer.
// IPC protocol reference: switchbrew/libnx nx/source/services/audren.c.
#include "hle.h"
#include "audren_legacy.h"
#include "legacy_audio_dsp.h"
#include <stdatomic.h>
#include <time.h>
#include <unistd.h>

void *dm_pcmout_open(int ch);
void dm_pcmout_close(void *k);
void dm_pcmout_start(void *k);
int dm_pcmout_append(void *k, void *b);
void *dm_pcmout_released(void *k);
void dm_pcmout_wait(void *k, int ms);
static struct {
    uint8_t param[0x34]; uint32_t event_read, event_write; int open, started, has_thread;
    _Atomic int run; pthread_t thread; ar_dsp *dsp; uint64_t frames, nonsilent, updates;
} g_ar1;
// Requests serialize the lifecycle; the render thread takes only the DSP lock,
// so Stop/Close can join it without holding a mutex it needs to finish.
static pthread_mutex_t g_ar1_mu = PTHREAD_MUTEX_INITIALIZER, g_ar1_dsp_mu = PTHREAD_MUTEX_INITIALIZER;
int svc_thread(int num, uint64_t *regs);

static void ar1_event(int num, uint32_t h) { uint64_t r[31] = {h}; svc_thread(num, r); }
typedef struct { void *ptr; uint64_t cap, size; } ar1_pcm_buffer;
static void *ar1_render(void *unused) {
    (void)unused;
    enum { NBUF = 4, MAX_FRAME = 1536 };
    int16_t data[NBUF][MAX_FRAME * 2]; ar1_pcm_buffer buffer[NBUF], *free_list[NBUF]; int nfree = NBUF;
    for (int i = 0; i < NBUF; i++) { buffer[i] = (ar1_pcm_buffer){data[i], sizeof data[i], 0}; free_list[i] = buffer + i; }
    void *output = dm_pcmout_open(2); dm_pcmout_start(output);
    while (atomic_load(&g_ar1.run)) {
        if (!nfree) {
            ar1_pcm_buffer *released = dm_pcmout_released(output);
            if (released) free_list[nfree++] = released;
            else { dm_pcmout_wait(output, 10); continue; }
        }
        ar1_pcm_buffer *b = free_list[--nfree];
        pthread_mutex_lock(&g_ar1_dsp_mu);
        int frames = ar_dsp_render(g_ar1.dsp, b->ptr, MAX_FRAME);
        g_ar1.frames++; int nonzero = 0;
        for (int i = 0; i < frames * 2; i++) if (((int16_t *)b->ptr)[i]) { nonzero = 1; break; }
        g_ar1.nonsilent += nonzero;
        if ((g_ar1.frames == 1 || g_ar1.frames == 256 || g_ar1.frames == 2048))
            LOG("audren legacy: rendered %llu frames (%llu nonsilent), %llu updates", (unsigned long long)g_ar1.frames, (unsigned long long)g_ar1.nonsilent, (unsigned long long)g_ar1.updates);
        pthread_mutex_unlock(&g_ar1_dsp_mu);
        if (!frames) { LOG("[RED] audren legacy: renderer produced no PCM frame"); break; }
        b->size = (uint64_t)frames * 4;
        if (!dm_pcmout_append(output, b)) {
            free_list[nfree++] = b;
            struct timespec pause = {0, 2000000}; nanosleep(&pause, NULL);
        }
        ar1_event(0x11, g_ar1.event_write);
    }
    // Stop the consumer before the thread's stack buffers go out of scope.
    dm_pcmout_close(output); return NULL;
}
static void ar1_stop(void) {
    atomic_store(&g_ar1.run, 0);
    if (g_ar1.has_thread) { pthread_join(g_ar1.thread, NULL); g_ar1.has_thread = 0; }
    g_ar1.started = 0;
}
void ar1_close(void) {
    pthread_mutex_lock(&g_ar1_mu);
    if (g_ar1.open) {
        ar1_stop(); ar_dsp_destroy(g_ar1.dsp);
        ar1_event(0x16, g_ar1.event_read); ar1_event(0x16, g_ar1.event_write);
        memset(&g_ar1, 0, sizeof g_ar1);
    }
    pthread_mutex_unlock(&g_ar1_mu);
}
static uint32_t ar1_word(const uint8_t *p) { uint32_t v; memcpy(&v, p, 4); return v; }
static int ar1_param(const uint8_t *p, size_t n) {
    return p && n >= 0x34 && (ar1_word(p + 0x30) == 0x31564552 || ar1_word(p + 0x30) == 0x32564552) &&
        (ar1_word(p) == 48000 || ar1_word(p) == 32000) && ar1_word(p + 4) > 0 && ar1_word(p + 4) <= 1024;
}
static void ar1_hex(FILE *f, const void *data, size_t size) {
    const uint8_t *p = data; for (size_t i = 0; i < size; i++) fprintf(f, "%02x", p[i]);
}
static void ar1_capture(const ar1_buffer *in, int nin, const ar1_buffer *out, int nout) {
    const char *path = getenv("DM_AUDREN_LEGACY_CAPTURE");
    if (!path || !*path || nin != 1 || in[0].size > 16 * 1024 * 1024) return;
    FILE *f = fopen(path, "w");
    if (!f) die("audren legacy: cannot write the requested capture");
    fputs("{\"parameter_hex\":\"", f); ar1_hex(f, g_ar1.param, sizeof g_ar1.param);
    fputs("\",\"input_hex\":\"", f); ar1_hex(f, in[0].data, in[0].size);
    fputs("\",\"output_sizes\":[", f);
    for (int i = 0; i < nout; i++) fprintf(f, "%s%zu", i ? "," : "", out[i].size);
    fputs("]}\n", f); fclose(f);
    LOG("audren legacy: captured %zu input bytes and %d output buffers; stopping this diagnostic before playback", in[0].size, nout);
    _exit(43);
}
static uint32_t ar1_request_locked(int manager, unsigned command, const uint8_t *args, size_t args_size,
                     const ar1_buffer *in, int nin, const ar1_buffer *out, int nout, ar1_reply *reply) {
    memset(reply, 0, sizeof *reply);
    if (manager) {
        if (command == 0 || command == 1) {
            if (!ar1_param(args, args_size)) { LOG("[RED] audren legacy: unsupported renderer parameter (size %zu)", args_size); return 0x499; }
            if (command == 1) { reply->value = 0x10000; reply->value_size = 8; return 0; }
            if (g_ar1.open) return RES_FAIL;
            ar_dsp *dsp = ar_dsp_create(args, args_size);
            if (!dsp) { LOG("[RED] audren legacy: unsupported DSP configuration"); return 0x499; }
            memcpy(g_ar1.param, args, sizeof g_ar1.param);
            uint64_t r[31] = {0}; svc_thread(0x45, r);
            if (r[0]) { ar_dsp_destroy(dsp); return (uint32_t)r[0]; }
            g_ar1.dsp = dsp;
            g_ar1.event_write = (uint32_t)r[1]; g_ar1.event_read = (uint32_t)r[2]; g_ar1.open = 1;
            reply->new_session = 1;
            LOG("audren legacy: opened software renderer at %u Hz, %u samples, %u voices", ar1_word(args), ar1_word(args + 4), ar1_word(args + 0x10));
            return 0;
        }
    } else if (g_ar1.open) {
        if (command <= 3 || command == 9) {
            reply->value = command < 3 ? ar1_word(g_ar1.param + 4 * command) : command == 3 ? !g_ar1.started : 100;
            reply->value_size = 4; return 0;
        }
        if (command == 4) {
            ar1_capture(in, nin, out, nout);
            if (nin != 1 || nout < 1) return 0x499;
            pthread_mutex_lock(&g_ar1_dsp_mu);
            uint32_t result = ar_dsp_update(g_ar1.dsp, in[0].data, in[0].size, out[0].data, out[0].size);
            if (!result) g_ar1.updates++;
            pthread_mutex_unlock(&g_ar1_dsp_mu);
            if (result) LOG("[RED] audren legacy: rejected update (%zu input bytes, %zu output bytes)", in[0].size, out[0].size);
            if (nout > 1 && out[1].data && out[1].size) memset(out[1].data, 0, out[1].size);
            return result;
        }
        if (command == 5) {
            if (g_ar1.started) return 0;
            atomic_store(&g_ar1.run, 1);
            if (pthread_create(&g_ar1.thread, NULL, ar1_render, NULL)) { atomic_store(&g_ar1.run, 0); return RES_FAIL; }
            g_ar1.has_thread = g_ar1.started = 1; return 0;
        }
        if (command == 6) { ar1_stop(); return 0; }
        if (command == 7) { reply->copy_handle = hle_copy_event_handle(g_ar1.event_read); return reply->copy_handle ? 0 : RES_FAIL; }
        if (command == 8) return 0;
    }
    LOG("[RED] audren legacy: unimplemented %s command %u", manager ? "manager" : "renderer", command);
    return RES_FAIL;
}
uint32_t ar1_request(int manager, unsigned command, const uint8_t *args, size_t args_size,
                     const ar1_buffer *in, int nin, const ar1_buffer *out, int nout, ar1_reply *reply) {
    pthread_mutex_lock(&g_ar1_mu);
    uint32_t result = ar1_request_locked(manager, command, args, args_size, in, nin, out, nout, reply);
    pthread_mutex_unlock(&g_ar1_mu); return result;
}
