#pragma once

#include <cstdint>

namespace hf3fs::storage::client {
struct IOBufferCopyStats {
  uint64_t inBytes{};
  uint64_t outBytes{};
};

// Process-lifetime counters, independent of periodic metric collection/reset.
IOBufferCopyStats ioBufferCopyStats() noexcept;
}  // namespace hf3fs::storage::client
