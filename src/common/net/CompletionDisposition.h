#pragma once

#include <cstdint>

namespace hf3fs::net {

enum class CompletionDisposition : uint8_t {
  Completed,
  RejectedBeforeExecute,
  OutcomeUnknown,
  LaneRetired,
};

struct PublicationSnapshot {
  uint64_t laneGeneration;
  uint64_t acceptedOffset;
  uint64_t publishedOffset;
  uint64_t peerDeliveredOffset;
  bool trustworthy;
};

struct PublicationRange {
  uint64_t laneGeneration{};
  uint64_t beginOffset{};
  uint64_t endOffset{};

  bool operator==(const PublicationRange &) const = default;
};

}  // namespace hf3fs::net
