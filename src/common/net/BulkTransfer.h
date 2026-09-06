#pragma once

#include <span>
#include <vector>

#include "common/net/Buffer.h"
#include "common/utils/Coroutine.h"

namespace hf3fs::net {

class BulkTransfer {
 public:
  virtual ~BulkTransfer() = default;
  virtual CoTryTask<void> pull(const RemoteBufferHandle &remote, std::span<SharedBuffer> local) = 0;
  virtual CoTryTask<void> push(const RemoteBufferHandle &remote, std::span<SharedBuffer> local) = 0;
};

enum class BulkDirection { Pull, Push };

class BulkTransferBatch {
 public:
  BulkTransferBatch(BulkTransfer *transfer, BulkDirection direction)
      : transfer_(transfer),
        direction_(direction) {}

  Result<Void> add(RemoteBufferHandle remote, SharedBuffer local);
  Result<Void> add(RemoteBufferHandle remote, std::span<SharedBuffer> local);
  CoTryTask<void> post();

  size_t size() const noexcept { return entries_.size(); }

 private:
  struct Entry {
    RemoteBufferHandle remote;
    std::vector<SharedBuffer> local;
  };

  BulkTransfer *transfer_{};
  BulkDirection direction_{};
  std::vector<Entry> entries_;
};

}  // namespace hf3fs::net
