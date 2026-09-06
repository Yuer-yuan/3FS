#pragma once

#include <folly/Synchronized.h>
#include <folly/executors/CPUThreadPoolExecutor.h>
#include <folly/fibers/Semaphore.h>
#include <limits>

#include "common/net/Buffer.h"
#include "common/utils/CPUExecutorGroup.h"
#include "common/utils/ConfigBase.h"
#include "common/utils/ConstructLog.h"
#include "common/utils/Coroutine.h"
#include "common/utils/Size.h"

namespace hf3fs::storage {

struct BufferIndex {
  uint32_t registerIndex;
  net::SharedBuffer buffer;
};

class BufferPool {
 public:
  class Config : public ConfigBase<Config> {
    CONFIG_ITEM(buffer_size, 4_MB);
    CONFIG_ITEM(buffer_count, 1024u);
    CONFIG_ITEM(big_buffer_size, 64_MB);
    CONFIG_ITEM(big_buffer_count, 64u);
    CONFIG_ITEM(rdmabuf_size, 0_B);      // deprecated config alias
    CONFIG_ITEM(rdmabuf_count, 0u);      // deprecated config alias
    CONFIG_ITEM(big_rdmabuf_size, 0_B);  // deprecated config alias
    CONFIG_ITEM(big_rdmabuf_count, 0u);  // deprecated config alias

   public:
    Size effectiveBufferSize() const { return rdmabuf_size() ? rdmabuf_size() : buffer_size(); }
    uint32_t effectiveBufferCount() const { return rdmabuf_count() ? rdmabuf_count() : buffer_count(); }
    Size effectiveBigBufferSize() const { return big_rdmabuf_size() ? big_rdmabuf_size() : big_buffer_size(); }
    uint32_t effectiveBigBufferCount() const { return big_rdmabuf_count() ? big_rdmabuf_count() : big_buffer_count(); }
  };
  BufferPool(const Config &config)
      : config_(config),
        bufferSize_(config_.effectiveBufferSize()),
        semaphore_(config_.effectiveBufferCount()),
        bigBufferSize_(config_.effectiveBigBufferSize()),
        bigSemaphore_(config_.effectiveBigBufferCount()) {}

  Result<Void> init(CPUExecutorGroup &executor);

  auto &iovecs() const { return iovecs_; }

  class Buffer {
   public:
    explicit Buffer(BufferPool &pool)
        : pool_(&pool) {}
    Buffer(const Buffer &) = delete;
    Buffer(Buffer &&other) = default;
    Buffer &operator=(Buffer &&other) = default;
    ~Buffer();

    Result<net::SharedBuffer> tryAllocate(uint32_t size);

    CoTryTask<net::SharedBuffer> allocate(uint32_t size);

    auto index() const { return indices_.back().registerIndex; }

   private:
    BufferPool *pool_{};
    std::vector<BufferIndex> indices_;
    net::SharedBuffer current_;
  };
  auto get() { return Buffer{*this}; }

  void clear(CPUExecutorGroup &executor);

 protected:
  static Result<std::vector<BufferIndex>> initBuffers(CPUExecutorGroup &executor,
                                                      Size bufferSize,
                                                      uint32_t bufferCount,
                                                      uint32_t limit,
                                                      std::vector<net::SharedBuffer> &outBuffers);

  BufferIndex allocate();

  BufferIndex allocateBig();

  void deallocate(const BufferIndex &index);

 private:
  ConstructLog<"storage::BufferPool"> constructLog_;
  const Config &config_;
  Size bufferSize_;
  std::vector<net::SharedBuffer> buffers_;
  std::vector<struct iovec> iovecs_;
  folly::fibers::Semaphore semaphore_;
  folly::Synchronized<std::vector<BufferIndex>, std::mutex> freeIndex_;

  Size bigBufferSize_;
  uint32_t bigBufferRegisterIndexStart_ = 0;
  folly::fibers::Semaphore bigSemaphore_;
  folly::Synchronized<std::vector<BufferIndex>, std::mutex> bigFreeIndex_;
};

}  // namespace hf3fs::storage
