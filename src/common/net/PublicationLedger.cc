#include "common/net/PublicationLedger.h"

#include <algorithm>
#include <limits>

namespace hf3fs::net {

Result<PublicationRange> PublicationLedger::reserve(uint64_t length) {
  std::lock_guard lock(mutex_);
  if (length == 0 || length > std::numeric_limits<uint64_t>::max() - nextOffset_) {
    return makeError(StatusCode::kInvalidArg, "invalid CXL publication range length");
  }
  PublicationRange range{laneGeneration_, nextOffset_, nextOffset_ + length};
  nextOffset_ = range.endOffset;
  return range;
}

Result<Void> PublicationLedger::retain(WriteItemPtr item) {
  std::lock_guard lock(mutex_);
  if (!item || !item->isReq() || !item->publication || item->publication->laneGeneration != laneGeneration_ ||
      item->publication->beginOffset >= item->publication->endOffset || item->publication->endOffset > nextOffset_) {
    return makeError(StatusCode::kInvalidArg, "write item has no valid CXL publication reservation");
  }
  if (std::find_if(entries_.begin(), entries_.end(), [&](const Entry &entry) { return entry.uuid == item->uuid; }) !=
      entries_.end()) {
    return makeError(StatusCode::kInvalidArg, "duplicate request UUID in CXL publication ledger");
  }
  auto requestLifetime = item->requestLifetime;
  entries_.push_back(Entry{item->uuid, *item->publication, std::move(item), std::move(requestLifetime)});
  return Void{};
}

Result<Void> PublicationLedger::observe(const PublicationSnapshot &snapshot) {
  std::lock_guard lock(mutex_);
  if (snapshot.pending) {
    if (snapshot.laneGeneration != laneGeneration_ || snapshot.trustworthy) {
      return makeError(RPCCode::kStaleGeneration, "invalid pending CXL publication identity");
    }
    // No stable delivery evidence: neither advance cursors nor release a
    // request buffer. A response or a later stable observation may do that.
    return Void{};
  }
  if (!validSnapshotLocked(snapshot)) {
    return makeError(RPCCode::kStaleGeneration, "invalid or non-monotonic CXL publication snapshot");
  }
  observeLocked(snapshot);
  return Void{};
}

Result<PublicationLedger::Resolution> PublicationLedger::complete(size_t uuid) {
  std::lock_guard lock(mutex_);
  auto entry =
      std::find_if(entries_.begin(), entries_.end(), [&](const Entry &candidate) { return candidate.uuid == uuid; });
  if (entry == entries_.end()) {
    return makeError(StatusCode::kInvalidArg, "request UUID is absent from CXL publication ledger");
  }
  Resolution resolution{entry->uuid, entry->range, CompletionDisposition::Completed, nullptr, nullptr};
  entries_.erase(entry);
  return resolution;
}

std::vector<PublicationLedger::Resolution> PublicationLedger::retire(const PublicationSnapshot &snapshot) {
  std::lock_guard lock(mutex_);
  const bool trustworthy = validSnapshotLocked(snapshot);
  if (trustworthy) {
    observeLocked(snapshot);
  }

  std::vector<Resolution> resolutions;
  resolutions.reserve(entries_.size());
  for (auto &entry : entries_) {
    CompletionDisposition disposition = CompletionDisposition::LaneRetired;
    WriteItemPtr retryable;
    if (trustworthy) {
      if (snapshot.peerDeliveredOffset > entry.range.beginOffset) {
        disposition = CompletionDisposition::OutcomeUnknown;
      } else if (entry.buffer) {
        disposition = CompletionDisposition::RejectedBeforeExecute;
        retryable = std::move(entry.buffer);
      }
    }
    resolutions.push_back(
        Resolution{entry.uuid, entry.range, disposition, std::move(retryable), std::move(entry.requestLifetime)});
  }
  entries_.clear();
  return resolutions;
}

size_t PublicationLedger::size() const {
  std::lock_guard lock(mutex_);
  return entries_.size();
}

size_t PublicationLedger::retainedBufferCount() const {
  std::lock_guard lock(mutex_);
  return std::count_if(entries_.begin(), entries_.end(), [](const Entry &entry) { return bool(entry.buffer); });
}

bool PublicationLedger::validSnapshotLocked(const PublicationSnapshot &snapshot) const {
  return !snapshot.pending && snapshot.trustworthy && snapshot.laneGeneration == laneGeneration_ &&
         snapshot.peerDeliveredOffset <= snapshot.publishedOffset &&
         snapshot.publishedOffset <= snapshot.acceptedOffset && snapshot.acceptedOffset <= nextOffset_ &&
         snapshot.acceptedOffset >= lastAccepted_ && snapshot.publishedOffset >= lastPublished_ &&
         snapshot.peerDeliveredOffset >= lastDelivered_;
}

void PublicationLedger::observeLocked(const PublicationSnapshot &snapshot) {
  lastAccepted_ = snapshot.acceptedOffset;
  lastPublished_ = snapshot.publishedOffset;
  lastDelivered_ = snapshot.peerDeliveredOffset;
  for (auto &entry : entries_) {
    if (snapshot.peerDeliveredOffset >= entry.range.endOffset) {
      entry.buffer.reset();
    }
  }
}

}  // namespace hf3fs::net
