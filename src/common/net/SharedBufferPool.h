#pragma once

#include <atomic>
#include <deque>
#include <folly/Utility.h>
#include <folly/fibers/Semaphore.h>
#include <memory>
#include <mutex>
#include <optional>

#include "common/net/Buffer.h"
#include "common/utils/Coroutine.h"

namespace hf3fs::net {

class SharedBufferPool : public std::enable_shared_from_this<SharedBufferPool>, public folly::MoveOnly {
  struct PrivateTag {};
  struct Lease;

 public:
  SharedBufferPool(PrivateTag, size_t bufferSize, size_t bufferCount)
      : bufferSize_(bufferSize),
        semaphore_(bufferCount),
        bufferCount_(bufferCount) {}

  static std::shared_ptr<SharedBufferPool> create(size_t bufferSize, size_t bufferCount) {
    return std::make_shared<SharedBufferPool>(PrivateTag{}, bufferSize, bufferCount);
  }

  CoTask<SharedBuffer> allocate(std::optional<folly::Duration> timeout = std::nullopt);

  size_t bufferSize() const noexcept { return bufferSize_; }
  size_t freeCount() const { return semaphore_.getAvailableTokens(); }
  size_t totalCount() const noexcept { return bufferCount_; }

 private:
  Result<SharedBuffer> checkout(SharedBuffer backing);
  void deallocate(SharedBuffer backing) noexcept;

  size_t bufferSize_{};
  folly::fibers::Semaphore semaphore_;
  size_t bufferCount_{};
  size_t allocatedCount_{};
  std::mutex mutex_;
  std::deque<SharedBuffer> freeList_;
};

}  // namespace hf3fs::net
