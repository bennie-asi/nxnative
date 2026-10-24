// Real HIPC replies and Mach shared-memory mappings, with no game files or GPU.
#include <sys/mman.h>
#include "../hle_sf.c"

static uint8_t *guest;
static const size_t guest_size = 0x18000;
int dm_guest_as_contains(uintptr_t a) { return a >= (uintptr_t)guest && a - (uintptr_t)guest < guest_size; }
static int checks, failures;
#define CHECK(c, msg) do { checks++; if (!(c)) { failures++; fprintf(stderr, "FAIL: %s\n", msg); } } while (0)

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
    munmap(guest, guest_size);
    printf("service/shared-memory: %d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
