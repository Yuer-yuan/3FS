#pragma once

#include <cstdint>
#include <mutex>
#include <vector>

#include "common/net/CompletionDisposition.h"
#include "common/net/WriteItem.h"
#include "common/utils/Result.h"

namespace hf3fs::net {

class PublicationLedger {
 public:
  struct Resolution {
    size_t uuid{};
    PublicationRange range;
    CompletionDisposition disposition{CompletionDisposition::LaneRetired};
    WriteItemPtr retryable;
    std::shared_ptr<void> requestLifetime;
  };

  explicit PublicationLedger(uint64_t laneGeneration, uint64_t initialOffset = 0)
      : laneGeneration_(laneGeneration),
        nextOffset_(initialOffset),
        lastAccepted_(initialOffset),
        lastPublished_(initialOffset),
        lastDelivered_(initialOffset) {}

  Result<PublicationRange> reserve(uint64_t length);
  Result<Void> retain(WriteItemPtr item);
  Result<Void> observe(const PublicationSnapshot &snapshot);
  Result<Resolution> complete(size_t uuid);
  std::vector<Resolution> retire(const PublicationSnapshot &snapshot);

  size_t size() const;
  size_t retainedBufferCount() const;

 private:
  struct Entry {
    size_t uuid{};
    PublicationRange range;
    WriteItemPtr buffer;
    std::shared_ptr<void> requestLifetime;
  };

  bool validSnapshotLocked(const PublicationSnapshot &snapshot) const;
  void observeLocked(const PublicationSnapshot &snapshot);

  const uint64_t laneGeneration_;
  mutable std::mutex mutex_;
  uint64_t nextOffset_{};
  uint64_t lastAccepted_{};
  uint64_t lastPublished_{};
  uint64_t lastDelivered_{};
  std::vector<Entry> entries_;
};

}  // namespace hf3fs::net
