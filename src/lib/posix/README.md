# Experimental POSIX / shared CXL IOV frontend

This repository-added adapter retains scalar POSIX application calls while using
the existing shared CXL IOV/IOR internally. It is not an upstream 3FS feature,
not a kernel FUSE zero-copy implementation, and not a production-complete POSIX
interceptor. The storage client/server CXL bulk path still simulates RDMA.

## Use

Build on the execution server with
`fast-paper/artifact/mlperf-storage/comparison/build_posix_iov.py --repo REPO --label LABEL`.
The builder records exact inputs, commands and artifacts in a small `/tmp`
directory. It does not replace the API or service binaries. Preload its
`libhf3fs_posix.so` into the application only:

```sh
export HF3FS_CXL_POSIX_MOUNT=/path/to/3fs/mount
export HF3FS_CXL_NATIVE_IOV=1
export HF3FS_CXL_POSIX_MODE=strict
export LD_PRELOAD=/path/to/libhf3fs_posix.so
```

Use `hf3fs_posix_alloc(size)` / `hf3fs_posix_free(base)` for application I/O
buffers; continue using `read`, `write`, `pread`, and `pwrite` for data. A single
call may use a subrange of an allocation. Requests crossing its end fail rather
than silently entering the compatibility path. Do not call ordinary `free` or
`munmap` on these buffers, change their page protections, or mutate a buffer
while an I/O using it is outstanding. A buffer must remain valid until its call
has started; the adapter then pins ownership until completion.

Modes:

- `strict`: only registered shared IOV buffers are accepted for target data I/O.
- `iov`: registered buffers are direct; ordinary buffers use a thread-private,
  lazily allocated, 32 MiB shared IOV and separately counted copies.
- `fuse`: the same POSIX probe goes through ordinary kernel FUSE, including when
  its application buffer was allocated from the shared arena.
- Unset: scalar operations are not redirected to IOR.

Heap compatibility copying uses Linux self `process_vm_readv/writev` so invalid
user memory returns EFAULT without a signal-handler trick. It includes a kernel
entry as well as the copy; its performance is not a pure memcpy-cost estimate.
No global malloc override, arbitrary page remapping or uncounted staging is used.

## Scope and lifetime

Only trusted UID 0 callers with the existing regular-file-backed fabric are
supported. Buffer allocation does not change fabric access permissions. This is
not a range-isolated mapping capability or a validated BI/DAX implementation.
NUMA policy is supplied by the existing launcher; shared fabric page placement
is inherited from the fabric allocation, not changed by an application mmap.

The experiment uses read-cache=false, writeback-cache=false and FUSE
`io_bufs.write_buf_size=0`. Do not mix kernel buffered data access, direct syscalls
or hidden stdio operations with this frontend on the same files. The adapter
checks the effective read-cache at initialization; changing configuration during
the run is unsupported. The controller records the other cache/buffering options.

open/openat and pathname metadata stay with the normal filesystem. On first use
a target regular descriptor gets an internal owning FD and API registration.
dup/dup2/dup3 and fcntl/fcntl64 duplication share its offset lock. The internal FD
prevents application FD reuse or close from invalidating outstanding I/O;
read/write advance by the completed byte count, while positioned I/O does not.
fsync/fdatasync/ftruncate are serialized with that open description's data calls.
Independent descriptions have independent locks. This is not a complete audit
of asynchronous cancellation or every Linux descriptor-management extension.

O_APPEND, vector I/O (including positioned variants), file mmap and use of inherited state after fork
are explicitly rejected on target files. Static programs, direct `syscall`,
hidden libc stdio entry points, descriptor passing between processes and concurrent descriptor replacement are
outside the tested interface surface; do not claim they are intercepted.
The parent rejects close_range while the frontend is enabled, since it could
close internal buffer leases. The fork child may exec or exit; the parent remains usable.

One IOR per direction per active application thread is created lazily. No new
spin/yield/sleep policy is added. Existing IOR completion waiting is reused.
An uncertain submit/wait failure poisons the thread context and retains its
buffer/FD/ring resources until process exit: returning an error is not proof that
the server has stopped accessing the buffer. The experiment's outer hard
deadline bounds hangs; crash/restart recovery is not claimed.

`hf3fs_posix_get_stats` reports monotonic direct/staged I/O and explicit copy
bytes. Snapshot at a quiescent boundary; the fields are not a transactional
snapshot during concurrent I/O. Zero adapter copies must be checked together
with StorageClient shadow-copy counters and nonzero CXL bulk traffic.

For unmodified applications, `HF3FS_CXL_POSIX_REPORT=1` emits one
`HF3FS_POSIX_COUNTERS` JSON record to stderr at normal process exit. It adds no
per-operation log and does not replace application allocators. These are process
totals including setup/teardown, not isolated benchmark timing-window counters.
Abnormal exits may not emit a record and must not be treated as zero traffic.

Ordinary Python/NumPy buffers do not become shared simply by enabling this
library. They can run unchanged in `iov` mode, with the explicit staging copy.
`deploy/giga-native/posix_macro.py` selects this FS path around the existing,
hash-locked macro runner; it does not modify benchmark, NumPy or DLIO behavior.
The user explicitly excluded changing benchmark buffer allocation in this
experiment. The earlier C POSIX shared-buffer probe is a distinct experiment.
