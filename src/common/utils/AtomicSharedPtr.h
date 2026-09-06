#pragma once

#ifndef HF3FS_USE_STD_ATOMIC_SHARED_PTR
#if defined(__riscv) && __riscv_xlen == 64
#define HF3FS_USE_STD_ATOMIC_SHARED_PTR 1
#else
#define HF3FS_USE_STD_ATOMIC_SHARED_PTR 0
#endif
#endif

#if HF3FS_USE_STD_ATOMIC_SHARED_PTR
#include <atomic>
#include <memory>
#else
#include <folly/concurrency/AtomicSharedPtr.h>
#endif

namespace hf3fs {

// Process-local ownership only: this type must never enter the CXL shared ABI.
// RV64 libstdc++ can use _S_mutex for shared_ptr, whereas this Folly revision
// assumes _S_atomic internals and 48-bit pointers. Use the standard API there;
// its implementation may lock locally. Shared CXL cursors remain lock-free.
template <typename T>
using AtomicSharedPtr =
#if HF3FS_USE_STD_ATOMIC_SHARED_PTR
    std::atomic<std::shared_ptr<T>>;
#else
    folly::atomic_shared_ptr<T>;
#endif

}  // namespace hf3fs
