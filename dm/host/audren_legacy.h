// Legacy audio renderer IPC, for SDKs without an in-process CPU renderer.
#pragma once
#include <stddef.h>
#include <stdint.h>
typedef struct { void *data; size_t size; } ar1_buffer;
typedef struct { uint64_t value; size_t value_size; uint32_t copy_handle; int new_session; } ar1_reply;
uint32_t ar1_request(int manager, unsigned command, const uint8_t *args, size_t args_size,
                     const ar1_buffer *in, int nin, const ar1_buffer *out, int nout, ar1_reply *reply);
void ar1_close(void);
