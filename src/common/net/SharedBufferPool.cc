#include "common/net/SharedBufferPool.h"

#include <folly/experimental/coro/Timeout.h>

#include "common/net/TransportRuntime.h"

namespace hf3fs::net {

struct SharedBufferPool::Lease {
  std::weak_ptr<SharedBufferPool> pool;
  SharedBuffer backing;

  ~Lease() {
    if (auto owner = pool.lock()) {
      owner->deallocate(std::move(backing));
    }
  }
};

CoTask<SharedBuffer> SharedBufferPool::allocate(std::optional<folly::Duration> timeout) {
  if (UNLIKELY(!semaphore_.try_wait())) {
    if (timeout) {
      auto result = co_await folly::coro::co_awaitTry(folly::coro::timeout(semaphore_.co_wait(), *timeout));
      if (result.hasException()) {
        co_return SharedBuffer{};
      }
    } else {
      co_await semaphore_.co_wait();
    }
  }

  SharedBuffer backing;
  {
    std::lock_guard lock(mutex_);
    if (!freeList_.empty()) {
      backing = std::move(freeList_.back());
      freeList_.pop_back();
    } else if (allocatedCount_ < bufferCount_) {
      ++allocatedCount_;
    }
  }

  if (!backing) {
    auto arena = TransportRuntime::cxlBufferArena();
    auto allocated =
        arena ? arena->tryAllocate(bufferSize_) : Result<SharedBuffer>{makeError(RPCCode::kDataPlaneNotInitialized)};
    if (!allocated) {
      {
        std::lock_guard lock(mutex_);
        --allocatedCount_;
      }
      semaphore_.signal();
      co_return SharedBuffer{};
    }
    backing = std::move(*allocated);
  }

  auto checkedOut = checkout(std::move(backing));
  if (!checkedOut) {
    co_return SharedBuffer{};
  }
  co_return std::move(*checkedOut);
}

Result<SharedBuffer> SharedBufferPool::checkout(SharedBuffer backing) {
  if (!backing || backing.size() != bufferSize_) {
    {
      std::lock_guard lock(mutex_);
      --allocatedCount_;
    }
    semaphore_.signal();
    return makeError(StatusCode::kInvalidArg, "shared buffer pool received an invalid backing allocation");
  }
  auto lease = std::make_shared<Lease>();
  lease->pool = weak_from_this();
  lease->backing = std::move(backing);
  std::weak_ptr<Lease> weakLease = lease;
  SharedBuffer::Exporter exporter =
      [weakLease](RemoteAccess access, uint64_t offset, uint64_t length) -> Result<RemoteBufferHandle> {
    auto owner = weakLease.lock();
    if (!owner) {
      return makeError(RPCCode::kStaleGeneration, "pooled shared buffer has already been returned");
    }
    auto range = owner->backing.subrange(offset, length);
    if (!range) {
      return makeError(std::move(range.error()));
    }
    auto exported = range->exportRemote(access);
    if (!exported) {
      return makeError(std::move(exported.error()));
    }
    return exported->handle();
  };
  return SharedBuffer::fromStorage(lease->backing.data(),
                                   lease->backing.size(),
                                   0,
                                   std::move(lease),
                                   std::move(exporter));
}

void SharedBufferPool::deallocate(SharedBuffer backing) noexcept {
  {
    std::lock_guard lock(mutex_);
    freeList_.push_back(std::move(backing));
  }
  semaphore_.signal();
}

}  // namespace hf3fs::net
