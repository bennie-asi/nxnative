// Expected mappings were captured from the original NVN command writer using
// synthetic rectangles. No game code or shader data is part of this test.
#include <stdio.h>
#include "../../gfx/nvn/nvn_draw_texture.h"

static int checks, failures;
#define CHECK(ok) do { checks++; if (!(ok)) { failures++; printf("DrawTexture check failed at line %d\n", __LINE__); } } while (0)

static void mapping(const float dst[4], const float src[4], const int32_t d[4], const int32_t s[2], const int64_t step[2]) {
    nvn_draw_texture_map m = {0};
    CHECK(nvn_dt_prepare(&m, dst, src) == 1);
    for (int i = 0; i < 4; i++) CHECK(m.dst[i] == d[i]);
    for (int i = 0; i < 2; i++) { CHECK(m.src[i] == s[i]); CHECK(m.step[i] == step[i]); }
}

int main(void) {
    mapping((float[]){0, 0, 1280, 720}, (float[]){0, 0, 1280, 720},
            (int32_t[]){0, 0, 5242880, 2949120}, (int32_t[]){0, 0},
            (int64_t[]){4294967296LL, 4294967296LL});
    mapping((float[]){10.1f, 20.2f, 650.3f, 380.4f}, (float[]){3.1f, 4.2f, 1283.5f, 724.6f},
            (int32_t[]){41376, 82736, 2622256, 1475376}, (int32_t[]){12704, 17200},
            (int64_t[]){8589945238LL, 8589952786LL});
    mapping((float[]){640, 0, 0, 360}, (float[]){0, 0, 1280, 720},
            (int32_t[]){2621440, 0, -2621440, 1474560}, (int32_t[]){0, 0},
            (int64_t[]){-8589934592LL, 8589934592LL});
    mapping((float[]){0, 0, 640, 360}, (float[]){1280, 720, 0, 0},
            (int32_t[]){0, 0, 2621440, 1474560}, (int32_t[]){5242880, 2949120},
            (int64_t[]){-8589934592LL, -8589934592LL});
    mapping((float[]){-.501f, -.499f, 10.501f, 11.499f}, (float[]){2.501f, 3.499f, 20.501f, 21.499f},
            (int32_t[]){-2048, -2048, 45056, 49152}, (int32_t[]){10240, 14336},
            (int64_t[]){7028128303LL, 6442450944LL});
    mapping((float[]){0, 0, 640, 360}, (float[]){0, 0, 0, 720},
            (int32_t[]){0, 0, 2621440, 1474560}, (int32_t[]){0, 0},
            (int64_t[]){0, 8589934592LL});
    nvn_draw_texture_map m;
    CHECK(nvn_dt_prepare(&m, (float[]){0, 0, 0, 1}, (float[]){0, 0, 1, 1}) == 0);
    CHECK(nvn_dt_prepare(&m, (float[]){0, 0, .001f, 1}, (float[]){0, 0, 1, 1}) == 0);
    CHECK(nvn_dt_prepare(&m, (float[]){NAN, 0, 1, 1}, (float[]){0, 0, 1, 1}) == -1);
    CHECK(nvn_dt_position(.001953125f) == 16);
    CHECK(nvn_dt_position(-.001953125f) == 0);
    CHECK(nvn_dt_position(1e10f) == INT32_MAX);
    CHECK(nvn_dt_position(-1e10f) == INT32_MIN);
    printf("DrawTexture mapping: %d checks, %d failures\n", checks, failures);
    return failures != 0;
}
