#include "storage/service/BufferPool.h"

#include <boost/core/ignore_unused.hpp>
#include <sys/uio.h>

#include "common/monitor/Recorder.h"
#include "common/utils/MagicEnum.hpp"
#include "fbs/storage/Common.h"

namespace hf3fs::storage {
namespace {

Result<net::SharedBuffer> takeAligned(net::SharedBuffer &buffer, uint32_t size) {
  auto result = buffer.subrange(0, size);
  if (!result) {
    return makeError(std::move(result.error()));
  }
  const size_t consumed = ALIGN_UPPER(size, kAIOAlignSize);
  if (consumed >= buffer.size()) {
    buffer = {};
  } else {
    auto remaining = buffer.subrange(consumed, buffer.size() - consumed);
    if (!remaining) {
      return makeError(std::move(remaining.error()));
    }
    buffer = std::move(*remaining);
  }
  return result;
}

}  // namespace

Result<Void> BufferPool::init(CPUExecutorGroup &executor) {
  buffers_.clear();
  buffers_.reserve(UIO_MAXIOV);

  auto smallBufferResult =
      initBuffers(executor, config_.effectiveBufferSize(), config_.effectiveBufferCount(), UIO_MAXIOV / 2, buffers_);
  RETURN_AND_LOG_ON_ERROR(smallBufferResult);
  *freeIndex_.lock() = std::move(*smallBufferResult);

  bigBufferRegisterIndexStart_ = buffers_.size();

  auto bigBufferResult = initBuffers(executor,
                                     config_.effectiveBigBufferSize(),
                                     config_.effectiveBigBufferCount(),
                                     UIO_MAXIOV / 2,
                                     buffers_);
  RETURN_AND_LOG_ON_ERROR(bigBufferResult);
  *bigFreeIndex_.lock() = std::move(*bigBufferResult);

  iovecs_.clear();
  iovecs_.reserve(buffers_.size());
  for (auto &buf : buffers_) {
    iovecs_.push_back({buf.data(), buf.size()});
  }
  return Void{};
}

Result<std::vector<BufferIndex>> BufferPool::initBuffers(CPUExecutorGroup &executor,
                                                         Size bufferSize,
                                                         uint32_t requestedBufferCount,
                                                         uint32_t limit,
                                                         std::vector<net::SharedBuffer> &outBuffers) {
  boost::ignore_unused(executor);
  if (bufferSize == 0 || requestedBufferCount == 0 || limit == 0) {
    return makeError(StatusCode::kInvalidConfig, "storage buffer pool cannot be empty");
  }
  const size_t totalSize = bufferSize * requestedBufferCount;
  const size_t allocationCount = std::min<size_t>(limit, requestedBufferCount);
  const size_t slicesPerAllocation = (totalSize / allocationCount + bufferSize - 1) / bufferSize;
  const size_t allocationSize = slicesPerAllocation * bufferSize;

  std::vector<BufferIndex> freeIndex;
  freeIndex.reserve(requestedBufferCount);
  XLOGF(INFO, "allocate {} * {} shared buffers started", allocationCount, Size{allocationSize});
  for (size_t index = 0; index < allocationCount; ++index) {
    // These addresses are registered with disk AIO and can reach device DMA.
    // CXL transport mappings are CPU-accessible shared memory, not disk DMA
    // buffers. Forwarding exports take a separate stable copy in the arena.
    auto allocated = net::SharedBuffer::allocateAligned(allocationSize, kAIOAlignSize);
    if (!allocated) {
      return makeError(StorageCode::kStorageInitFailed, allocated.error().describe());
    }
    const uint32_t registerIndex = outBuffers.size();
    outBuffers.push_back(*allocated);
    for (size_t offset = 0; offset + bufferSize <= allocated->size(); offset += bufferSize) {
      auto slice = allocated->subrange(offset, bufferSize);
      if (!slice) {
        return makeError(std::move(slice.error()));
      }
      freeIndex.push_back(BufferIndex{registerIndex, std::move(*slice)});
    }
  }
  XLOGF(INFO, "allocate {} * {} shared buffers finished", allocationCount, Size{allocationSize});
  return freeIndex;
}

BufferPool::Buffer::~Buffer() {
  for (auto &index : indices_) {
    pool_->deallocate(index);
  }
}

Result<net::SharedBuffer> BufferPool::Buffer::tryAllocate(uint32_t size) {
  if (indices_.empty() || current_.size() < size) {
    if (UNLIKELY(size > pool_->bufferSize_)) {
      return makeError(StorageCode::kBufferSizeExceeded);
    }
    if (LIKELY(pool_->semaphore_.try_wait())) {
      auto index = pool_->allocate();
      indices_.push_back(index);
      current_ = index.buffer;
    } else {
      return makeError(RPCCode::kNoSharedBuffer);
    }
  }
  return takeAligned(current_, size);
}

CoTryTask<net::SharedBuffer> BufferPool::Buffer::allocate(uint32_t size) {
  if (indices_.empty() || current_.size() < size) {
    if (UNLIKELY(size > pool_->bigBufferSize_)) {
      co_return makeError(StorageCode::kBufferSizeExceeded);
    } else if (UNLIKELY(size > pool_->bufferSize_)) {
      co_await pool_->bigSemaphore_.co_wait();
      auto index = pool_->allocateBig();
      indices_.push_back(index);
      current_ = index.buffer;
    } else {
      co_await pool_->semaphore_.co_wait();
      auto index = pool_->allocate();
      indices_.push_back(index);
      current_ = index.buffer;
    }
  }
  co_return takeAligned(current_, size);
}

void BufferPool::clear(CPUExecutorGroup &executor) {
  boost::ignore_unused(executor);
  XLOGF(INFO, "deallocate {} shared buffers", buffers_.size());
  freeIndex_.lock()->clear();
  bigFreeIndex_.lock()->clear();
  iovecs_.clear();
  buffers_.clear();
}

BufferIndex BufferPool::allocate() {
  auto guard = freeIndex_.lock();
  assert(!guard->empty());
  auto ret = guard->back();
  guard->pop_back();
  return ret;
}

BufferIndex BufferPool::allocateBig() {
  auto guard = bigFreeIndex_.lock();
  assert(!guard->empty());
  auto ret = guard->back();
  guard->pop_back();
  return ret;
}

void BufferPool::deallocate(const BufferIndex &index) {
  if (UNLIKELY(index.registerIndex >= bigBufferRegisterIndexStart_)) {
    bigFreeIndex_.lock()->push_back(index);
    bigSemaphore_.signal();
  } else {
    freeIndex_.lock()->push_back(index);
    semaphore_.signal();
  }
}

}  // namespace hf3fs::storage
