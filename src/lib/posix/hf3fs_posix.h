#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// Opt-in, trusted file-backed CXL adapter. Allocation changes, POSIX I/O does not.
// Configure HF3FS_CXL_POSIX_MOUNT, HF3FS_CXL_NATIVE_IOV=1 and MODE=fuse|iov|strict.
void *hf3fs_posix_alloc(size_t size);
// Only an exact allocation base is accepted. Outstanding calls retain ownership.
int hf3fs_posix_free(void *base);

struct hf3fs_posix_stats {
  uint64_t direct_read_bytes;
  uint64_t direct_write_bytes;
  uint64_t staged_read_bytes;
  uint64_t staged_write_bytes;
  uint64_t staging_copy_in_bytes;
  uint64_t staging_copy_out_bytes;
  uint64_t read_calls;
  uint64_t write_calls;
  uint64_t rejected_calls;
};

// Monotonic process totals. No counter reset or unmeasured copy suppression.
int hf3fs_posix_get_stats(struct hf3fs_posix_stats *out);

#ifdef __cplusplus
}
#endif
