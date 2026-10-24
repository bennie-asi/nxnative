// SDK 1.6 has no ExecuteAudioRendererRendering or DFC server. Its audio renderer
// talks to audren:u. The capture mode records the real update packet before a
// legacy DSP backend is enabled; it never reports captured requests as playback.
// IPC protocol reference: switchbrew/libnx nx/source/services/audren.c.
#include "hle.h"
#include "audren_legacy.h"
#include <unistd.h>

static struct { uint8_t param[0x34]; uint32_t event_read, event_write; int open, started; } g_ar1;
int svc_thread(int num, uint64_t *regs);

static void ar1_event(int num, uint32_t h) { uint64_t r[31] = {h}; svc_thread(num, r); }
void ar1_close(void) {
    if (!g_ar1.open) return;
    ar1_event(0x16, g_ar1.event_read); ar1_event(0x16, g_ar1.event_write);
    memset(&g_ar1, 0, sizeof g_ar1);
}
static uint32_t ar1_word(const uint8_t *p) { uint32_t v; memcpy(&v, p, 4); return v; }
static int ar1_param(const uint8_t *p, size_t n) {
    return p && n >= 0x34 && ar1_word(p + 0x30) == 0x31564552 &&
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
uint32_t ar1_request(int manager, unsigned command, const uint8_t *args, size_t args_size,
                     const ar1_buffer *in, int nin, const ar1_buffer *out, int nout, ar1_reply *reply) {
    memset(reply, 0, sizeof *reply);
    if (manager) {
        if (command == 0 || command == 1) {
            if (!ar1_param(args, args_size)) { LOG("[RED] audren legacy: unsupported renderer parameter (size %zu)", args_size); return 0x499; }
            if (command == 1) { reply->value = 0x10000; reply->value_size = 8; return 0; }
            if (g_ar1.open || !getenv("DM_AUDREN_LEGACY_CAPTURE")) {
                LOG("[RED] audren legacy: playback backend is not yet enabled; use the diagnostic capture to inspect this SDK"); return RES_FAIL;
            }
            memcpy(g_ar1.param, args, sizeof g_ar1.param);
            uint64_t r[31] = {0}; svc_thread(0x45, r);
            if (r[0]) return (uint32_t)r[0];
            g_ar1.event_write = (uint32_t)r[1]; g_ar1.event_read = (uint32_t)r[2]; g_ar1.open = 1;
            reply->new_session = 1;
            LOG("audren legacy: opened capture renderer at %u Hz, %u samples, %u voices", ar1_word(args), ar1_word(args + 4), ar1_word(args + 0x10));
            return 0;
        }
    } else if (g_ar1.open) {
        if (command <= 3 || command == 9) {
            reply->value = command < 3 ? ar1_word(g_ar1.param + 4 * command) : command == 3 ? !g_ar1.started : 100;
            reply->value_size = 4; return 0;
        }
        if (command == 4) { ar1_capture(in, nin, out, nout); return RES_FAIL; }
        if (command == 5 || command == 6) { g_ar1.started = command == 5; if (g_ar1.started) ar1_event(0x11, g_ar1.event_write); return 0; }
        if (command == 7) { reply->copy_handle = hle_copy_event_handle(g_ar1.event_read); return reply->copy_handle ? 0 : RES_FAIL; }
        if (command == 8) return 0;
    }
    LOG("[RED] audren legacy: unimplemented %s command %u", manager ? "manager" : "renderer", command);
    return RES_FAIL;
}
