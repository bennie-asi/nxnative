// Host service shared memory. IRS clients embedded in a game's main (Revelations,
// SDK 1.6) request a copied kernel handle, map it with SVC 0x13, and read camera
// status directly. Mach remaps preserve sharing between the service and its guest
// views; returning success without mapping leaves the SDK reading inaccessible AS.
#include "dm.h"
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <sys/mman.h>

#define SHM_MAX 64
#define SHM_MAP_MAX 128
static struct { uint32_t handle; void *base; size_t size; } g_shm[SHM_MAX];
static struct { uint32_t handle, perm; uint64_t addr, size; } g_shm_maps[SHM_MAP_MAX];
static uint32_t g_shm_next = 0xe0000;
static pthread_mutex_t g_shm_mu = PTHREAD_MUTEX_INITIALIZER;

uint32_t shared_memory_create(size_t size, void **backing) {
    if (!size || size % vm_page_size) return 0;
    void *base = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
    if (base == MAP_FAILED) return 0;
    pthread_mutex_lock(&g_shm_mu);
    int i = 0; while (i < SHM_MAX && g_shm[i].handle) i++;
    uint32_t handle = i < SHM_MAX ? g_shm_next++ : 0;
    if (handle) { g_shm[i].handle = handle; g_shm[i].base = base; g_shm[i].size = size; *backing = base; }
    pthread_mutex_unlock(&g_shm_mu);
    if (!handle) munmap(base, size);
    return handle;
}

int shared_memory_query(uint64_t addr, uint64_t *base, uint64_t *size, uint32_t *perm) {
    int found = 0;
    pthread_mutex_lock(&g_shm_mu);
    for (int i = 0; i < SHM_MAP_MAX; i++) if (g_shm_maps[i].handle && addr >= g_shm_maps[i].addr && addr - g_shm_maps[i].addr < g_shm_maps[i].size) {
        *base = g_shm_maps[i].addr; *size = g_shm_maps[i].size; *perm = g_shm_maps[i].perm; found = 1; break;
    }
    pthread_mutex_unlock(&g_shm_mu);
    return found;
}

void shared_memory_free_bounds(uint64_t addr, uint64_t *lo, uint64_t *hi) {
    pthread_mutex_lock(&g_shm_mu);
    for (int i = 0; i < SHM_MAP_MAX; i++) if (g_shm_maps[i].handle) {
        uint64_t end = g_shm_maps[i].addr + g_shm_maps[i].size;
        if (end <= addr && end > *lo) *lo = end;
        if (g_shm_maps[i].addr > addr && g_shm_maps[i].addr < *hi) *hi = g_shm_maps[i].addr;
    }
    pthread_mutex_unlock(&g_shm_mu);
}

int shared_memory_svc(int num, uint64_t *r) {
    if (num != 0x13 && num != 0x14 && num != 0x16) return 0;
    uint32_t handle = (uint32_t)r[0], result = 0;
    pthread_mutex_lock(&g_shm_mu);
    int i = 0; while (i < SHM_MAX && g_shm[i].handle != handle) i++;
    if (!handle || i == SHM_MAX) {
        pthread_mutex_unlock(&g_shm_mu);
        if (num == 0x16) return 0;   // another kernel object's CloseHandle
        r[0] = 0xe401; return 1;     // ResultInvalidHandle
    }
    if (num == 0x16) {
        munmap(g_shm[i].base, g_shm[i].size); memset(&g_shm[i], 0, sizeof g_shm[i]);
    } else {
        uint64_t addr = r[1], size = r[2];
        int dm_guest_as_contains(uintptr_t);
        if (!size || size != g_shm[i].size || addr % vm_page_size || size % vm_page_size ||
            !dm_guest_as_contains(addr) || !dm_guest_as_contains(addr + size - 1) || addr + size < addr) result = 0xd401;
        else if (num == 0x13) {
            uint32_t perm = (uint32_t)r[3]; int slot = -1, overlap = 0;
            for (int k = 0; k < SHM_MAP_MAX; k++) {
                if (!g_shm_maps[k].handle) { if (slot < 0) slot = k; continue; }
                if (addr < g_shm_maps[k].addr + g_shm_maps[k].size && g_shm_maps[k].addr < addr + size) overlap = 1;
            }
            if ((perm != 1 && perm != 3) || slot < 0 || overlap) result = 0xd401;
            else {
                mach_vm_address_t dst = addr; vm_prot_t cur = 0, max = 0;
                kern_return_t kr = mach_vm_remap(mach_task_self(), &dst, size, 0, VM_FLAGS_FIXED | VM_FLAGS_OVERWRITE,
                    mach_task_self(), (mach_vm_address_t)g_shm[i].base, FALSE, &cur, &max, VM_INHERIT_SHARE);
                if (kr == KERN_SUCCESS) kr = mach_vm_protect(mach_task_self(), dst, size, FALSE, perm == 1 ? VM_PROT_READ : VM_PROT_READ | VM_PROT_WRITE);
                if (kr != KERN_SUCCESS) { LOG("[RED] shared memory map %#x failed: Mach %d", handle, kr); result = 0xd401; }
                else { g_shm_maps[slot].handle = handle; g_shm_maps[slot].addr = addr; g_shm_maps[slot].size = size; g_shm_maps[slot].perm = perm; }
            }
        } else {
            int slot = 0; while (slot < SHM_MAP_MAX && !(g_shm_maps[slot].handle == handle && g_shm_maps[slot].addr == addr && g_shm_maps[slot].size == size)) slot++;
            if (slot == SHM_MAP_MAX) result = 0xd401;
            else if (mmap((void *)addr, size, PROT_NONE, MAP_PRIVATE | MAP_ANON | MAP_FIXED, -1, 0) == MAP_FAILED) result = 0xd401;
            else memset(&g_shm_maps[slot], 0, sizeof g_shm_maps[slot]);
        }
    }
    pthread_mutex_unlock(&g_shm_mu);
    r[0] = result;
    return 1;
}
