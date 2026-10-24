// Real HIPC replies and Mach shared-memory mappings, with no game files or GPU.
#include <sys/mman.h>
#include <stdatomic.h>
#include <time.h>
#include "../hle_sf.c"

static uint8_t *guest;
static const size_t guest_size = 0x18000;
int dm_guest_as_contains(uintptr_t a) { return a >= (uintptr_t)guest && a - (uintptr_t)guest < guest_size; }
static int checks, failures;
#define CHECK(c, msg) do { checks++; if (!(c)) { failures++; fprintf(stderr, "FAIL: %s\n", msg); } } while (0)
static _Atomic int audio_live, audio_closed, event_live, event_signals, audio_appends;
int svc_thread(int num, uint64_t *regs) {
    if (num == 0x45) { regs[0] = 0; regs[1] = 0x7000; regs[2] = 0x7001; atomic_fetch_add(&event_live, 2); }
    else if (num == 0x11) { regs[0] = 0; atomic_fetch_add(&event_signals, 1); }
    else if (num == 0x16) { regs[0] = 0; atomic_fetch_sub(&event_live, 1); }
    else { regs[0] = 0xe401; }
    return 1;
}
uint32_t hle_copy_event_handle(uint32_t handle) { atomic_fetch_add(&event_live, 1); return handle + 1; }
typedef struct { void *queue[4]; unsigned head, tail; } fake_pcm;
void *dm_pcmout_open(int ch) { (void)ch; atomic_fetch_add(&audio_live, 1); return calloc(1, sizeof(fake_pcm)); }
void dm_pcmout_start(void *k) { (void)k; }
int dm_pcmout_append(void *k, void *b) { fake_pcm *p = k; p->queue[p->tail++ % 4] = b; atomic_fetch_add(&audio_appends, 1); return 1; }
void *dm_pcmout_released(void *k) {
    struct timespec pause = {0, 1000000}; nanosleep(&pause, NULL);
    fake_pcm *p = k; return p->head != p->tail ? p->queue[p->head++ % 4] : NULL;
}
void dm_pcmout_wait(void *k, int ms) { (void)k; struct timespec pause = {0, ms * 1000000L}; nanosleep(&pause, NULL); }
void dm_pcmout_close(void *k) { free(k); atomic_fetch_sub(&audio_live, 1); atomic_fetch_add(&audio_closed, 1); }

static Result request(uint32_t handle, unsigned command, uint32_t arg, int domain, uint32_t *m) {
    memset(m, 0, 0x100);
    m[0] = 4; m[1] = 32;
    unsigned cm = domain ? 8 : 4;
    if (domain) { m[4] = 1; m[5] = 1; }
    m[cm] = 0x49434653; m[cm + 2] = command; m[cm + 4] = arg;
    Result rc = h_sf_Send(handle, m, 0x100);
    CHECK(rc == 0, "transport succeeds");
    CHECK(m[cm] == 0x4f434653, "response has SFCO magic");
    return m[cm + 2];
}

static Result memory_call(int num, uint32_t handle, void *addr, size_t size, unsigned perm) {
    uint64_t r[4] = {handle, (uintptr_t)addr, size, perm};
    CHECK(shared_memory_svc(num, r) == 1, "shared-memory SVC is handled");
    return (Result)r[0];
}

int main(void) {
    guest = mmap(NULL, guest_size, PROT_NONE, MAP_PRIVATE | MAP_ANON, -1, 0);
    if (guest == MAP_FAILED) return 2;
    uint8_t *view = guest + 0x4000;
    uint32_t session = 0, m[0x100 / 4];
    CHECK(!h_sf_Connect(&session, "irs") && session, "IRS exists without physical cameras");
    CHECK(!request(session, 302, 0, 0, m), "IRS activation succeeds");
    CHECK(!request(session, 304, 0, 0, m), "IRS returns shared memory");
    CHECK((m[1] >> 31) && m[2] == 2 && m[3], "HIPC response copies one kernel handle");
    uint32_t handle = m[3];
    Result mapped = memory_call(0x13, handle, view, 0x8000, 1);
    CHECK(!mapped, "read-only shared memory maps into guest AS");
    if (!mapped) {
        for (unsigned i = 0; i < 9; i++) {
            CHECK(*(uint32_t *)(view + i * 0xe30) == 2, "camera is disconnected");
            CHECK(*(uint32_t *)(view + i * 0xe30 + 8) == 0, "camera processor is stopped");
        }
        uint64_t base = 0, size = 0, lo = (uintptr_t)guest, hi = lo + guest_size; uint32_t perm = 0;
        CHECK(shared_memory_query((uintptr_t)view + 1, &base, &size, &perm) && base == (uintptr_t)view && size == 0x8000 && perm == 1, "QueryMemory sees the mapped extent and permissions");
        shared_memory_free_bounds((uintptr_t)guest, &lo, &hi);
        CHECK(hi == (uintptr_t)view, "free ranges end at shared mappings");
        CHECK(memory_call(0x14, handle, view, 0x4000, 0) != 0, "wrong-size unmap is rejected");
        CHECK(!memory_call(0x14, handle, view, 0x8000, 0), "shared mapping unmaps");
        CHECK(!shared_memory_query((uintptr_t)view, &base, &size, &perm), "unmapped view leaves the mapping registry");
    }
    CHECK(!memory_call(0x16, handle, NULL, 0, 0), "shared handle closes");
    CHECK(memory_call(0x13, handle, view, 0x8000, 1) == 0xe401, "closed handles cannot map");
    uint32_t object = 0;
    CHECK(!h_sf_ToDomain(&object, session) && object == 1, "IRS session converts to a domain");
    CHECK(!request(session, 311, 0x20, 1, m) && m[12] == 8, "domain camera handle preserves handheld slot 8");
    CHECK(request(session, 307, 0, 1, m) == 0xdccd, "camera processing reports disconnected hardware");
    h_sf_Close(session);
    CHECK(!h_sf_Connect(&session, "olsc:u"), "existing cloud settings service connects");
    CHECK(!request(session, 0, 0, 0, m) && !(m[1] >> 31), "existing zero-output service reply is unchanged");
    h_sf_Close(session);
    CHECK(!h_sf_Connect(&session, "irs"), "a short-lived IRS client connects");
    m[0] = 2; m[1] = 0;
    CHECK(!h_sf_Send(session, m, 8), "HIPC Close needs no CMIF payload");
    CHECK(h_sf_Dup(&handle, session) != 0, "HIPC Close releases its session");
    h_sf_Close(session);
    CHECK(h_sf_Connect(&session, "unknown") == 0xe15, "unregistered services remain unavailable");

    void *backing = NULL; handle = shared_memory_create(0x8000, &backing);
    CHECK(handle && backing, "a service can create shared backing");
    if (handle) {
        memset(backing, 0x31, 0x8000);
        CHECK(memory_call(0x13, handle, view, 0x8000, 5) != 0, "executable shared mapping is rejected");
        mapped = memory_call(0x13, handle, view, 0x8000, 3);
        CHECK(!mapped, "read-write shared view maps");
        if (!mapped) {
            CHECK(view[0] == 0x31, "guest sees service data");
            view[0] = 0x52; CHECK(*(uint8_t *)backing == 0x52, "guest writes are shared with the service");
            *(uint8_t *)backing = 0x63; CHECK(view[0] == 0x63, "service writes remain visible in the guest");
            CHECK(memory_call(0x13, handle, view, 0x8000, 3) != 0, "overlapping mappings are rejected");
            CHECK(!memory_call(0x14, handle, view, 0x8000, 0), "read-write view unmaps");
        }
        CHECK(!memory_call(0x16, handle, NULL, 0, 0), "backing handle closes after unmap");
    }
    CHECK(!mprotect(guest + 0x10000, 0x4000, PROT_READ | PROT_WRITE), "transfer-memory owner buffer is accessible");
    guest[0x10000] = 0x75;
    uint64_t transfer[4] = {0, (uintptr_t)guest + 0x10000, 0x4000, 0};
    CHECK(shared_memory_svc(0x15, transfer) && !transfer[0] && transfer[1], "CreateTransferMemory returns a kernel handle");
    CHECK(!memory_call(0x16, (uint32_t)transfer[1], NULL, 0, 0), "transfer-memory handle closes");
    CHECK(guest[0x10000] == 0x75, "closing transfer memory preserves the owner's allocation");
    uint32_t parameter[0x34 / 4] = {48000, 240}; parameter[0x30 / 4] = 0x31564552;
    ar1_reply reply;
    CHECK(!ar1_request(1, 1, (const uint8_t *)parameter, sizeof parameter, NULL, 0, NULL, 0, &reply) && reply.value_size == 8 && reply.value >= 0x1000,
          "legacy renderer accepts its original REV1 parameter");
    parameter[0x30 / 4] = 0x32564552;
    CHECK(!ar1_request(1, 1, (const uint8_t *)parameter, sizeof parameter, NULL, 0, NULL, 0, &reply),
          "legacy renderer accepts the SDK 1.6 REV2 parameter");
    parameter[0x30 / 4] = 1;
    CHECK(ar1_request(1, 1, (const uint8_t *)parameter, sizeof parameter, NULL, 0, NULL, 0, &reply) != 0,
          "corrupting the old revision with a modern execution mode is rejected");
    parameter[0x30 / 4] = 0x32564552; parameter[2] = 2; parameter[4] = 1; parameter[5] = 1;
    CHECK(!ar1_request(1, 0, (const uint8_t *)parameter, sizeof parameter, NULL, 0, NULL, 0, &reply) && reply.new_session,
          "legacy renderer opens the real software backend");
    CHECK(!ar1_request(0, 7, NULL, 0, NULL, 0, NULL, 0, &reply) && reply.copy_handle && atomic_load(&event_live) == 3,
          "renderer event reply owns a copied read handle");
    CHECK(!ar1_request(0, 5, NULL, 0, NULL, 0, NULL, 0, &reply), "legacy renderer starts its output thread");
    for (int i = 0; i < 100 && atomic_load(&audio_appends) < 8; i++) { struct timespec pause = {0, 1000000}; nanosleep(&pause, NULL); }
    CHECK(atomic_load(&audio_appends) >= 8 && atomic_load(&event_signals) >= 4, "output is replenished and frame events are signaled");
    CHECK(!ar1_request(0, 6, NULL, 0, NULL, 0, NULL, 0, &reply) && !atomic_load(&audio_live) && atomic_load(&audio_closed) == 1,
          "Stop joins the renderer before closing its PCM buffers");
    CHECK(!ar1_request(0, 3, NULL, 0, NULL, 0, NULL, 0, &reply) && reply.value == 1, "stopped state is reported");
    CHECK(!ar1_request(0, 5, NULL, 0, NULL, 0, NULL, 0, &reply), "stopped renderer can restart");
    ar1_close();
    CHECK(!atomic_load(&audio_live) && atomic_load(&audio_closed) == 2 && atomic_load(&event_live) == 1,
          "Close joins the restarted output and preserves the client's event copy");
    uint64_t close_copy[31] = {0x7002}; svc_thread(0x16, close_copy);
    CHECK(!atomic_load(&event_live), "client releases the last event reference");
    munmap(guest, guest_size);
    printf("service/shared-memory: %d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
