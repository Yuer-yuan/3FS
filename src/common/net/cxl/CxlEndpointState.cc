#include "common/net/cxl/CxlEndpointState.h"

#include <algorithm>
#include <atomic>
#include <limits>

namespace hf3fs::net::cxl {
namespace {

uint32_t endpointRecordCrc(const CxlEndpointRecord &record) {
  return cxlCrc32cWithZeroedU32(record, offsetof(CxlEndpointRecord, recordCrc32c));
}

bool validLifecycle(uint32_t encoded) {
  return encoded >= static_cast<uint32_t>(CxlEndpointLifecycle::Starting) &&
         encoded <= static_cast<uint32_t>(CxlEndpointLifecycle::Faulted);
}

Result<Void> validateEndpointRecord(const CxlEndpointRecord &record,
                                    uint64_t sessionGeneration,
                                    EndpointId endpoint,
                                    std::optional<uint64_t> expectedGeneration) {
  const uint64_t sequence = loadLe64(&record.recordSequence);
  const uint64_t generation = loadLe64(&record.endpointGeneration);
  if (sequence == 0 || (sequence & 1U) != 0 || loadLe64(&record.sessionGeneration) != sessionGeneration ||
      loadLe32(&record.endpointId) != endpoint.value || generation == 0 ||
      (expectedGeneration && generation != *expectedGeneration)) {
    return makeError(RPCCode::kStaleGeneration, "CXL endpoint identity or generation mismatch");
  }
  if (!validLifecycle(loadLe32(&record.lifecycle)) || loadLe32(&record.reserved0) != 0 ||
      loadLe64(&record.reserved1) != 0 || loadLe32(&record.recordCrc32c) != endpointRecordCrc(record)) {
    return makeError(StatusCode::kDataCorruption, "invalid CXL endpoint lifecycle, reserved bytes or CRC32C");
  }
  return Void{};
}

bool validTransition(CxlEndpointLifecycle from, CxlEndpointLifecycle to) {
  switch (from) {
    case CxlEndpointLifecycle::Starting:
      return to == CxlEndpointLifecycle::Ready || to == CxlEndpointLifecycle::Faulted;
    case CxlEndpointLifecycle::Ready:
      return to == CxlEndpointLifecycle::Draining || to == CxlEndpointLifecycle::Faulted;
    case CxlEndpointLifecycle::Draining:
      return to == CxlEndpointLifecycle::Retired || to == CxlEndpointLifecycle::Faulted;
    default:
      return false;
  }
}

}  // namespace

CxlEndpointState::CxlEndpointState(std::span<std::byte> directory,
                                   uint32_t endpointCount,
                                   uint64_t sessionGeneration,
                                   EndpointId endpoint,
                                   uint64_t endpointGeneration,
                                   uint64_t capabilityBits,
                                   uint64_t recordSequence) noexcept
    : directory_(directory),
      endpointCount_(endpointCount),
      sessionGeneration_(sessionGeneration),
      endpoint_(endpoint),
      endpointGeneration_(endpointGeneration),
      capabilityBits_(capabilityBits),
      recordSequence_(recordSequence) {}

CxlEndpointState::CxlEndpointState(CxlEndpointState &&other) noexcept {
  std::lock_guard lock(other.mutex_);
  directory_ = other.directory_;
  endpointCount_ = other.endpointCount_;
  sessionGeneration_ = other.sessionGeneration_;
  endpoint_ = other.endpoint_;
  endpointGeneration_ = other.endpointGeneration_;
  capabilityBits_ = other.capabilityBits_;
  recordSequence_ = other.recordSequence_;
  heartbeat_ = other.heartbeat_;
  lifecycle_ = other.lifecycle_;
  observations_ = std::move(other.observations_);
}

CxlEndpointState &CxlEndpointState::operator=(CxlEndpointState &&other) noexcept {
  if (this != &other) {
    std::scoped_lock lock(mutex_, other.mutex_);
    directory_ = other.directory_;
    endpointCount_ = other.endpointCount_;
    sessionGeneration_ = other.sessionGeneration_;
    endpoint_ = other.endpoint_;
    endpointGeneration_ = other.endpointGeneration_;
    capabilityBits_ = other.capabilityBits_;
    recordSequence_ = other.recordSequence_;
    heartbeat_ = other.heartbeat_;
    lifecycle_ = other.lifecycle_;
    observations_ = std::move(other.observations_);
  }
  return *this;
}

Result<CxlEndpointState> CxlEndpointState::claim(CxlLayout &layout,
                                                 EndpointId endpoint,
                                                 uint64_t endpointGeneration,
                                                 uint64_t capabilityBits) {
  const uint32_t endpointCount = loadLe32(&layout.superblock().header.endpointCount);
  if (endpoint.value == 0 || endpoint.value > endpointCount) {
    return makeError(StatusCode::kInvalidArg, "invalid CXL endpoint claim identity");
  }
  auto directoryResult = layout.range(CxlRangeKind::EndpointDirectory, alignof(CxlEndpointRecord));
  if (!directoryResult) {
    return makeError(std::move(directoryResult.error()));
  }
  auto directory = *directoryResult;
  const uint64_t byteOffset = static_cast<uint64_t>(endpoint.value - 1U) * sizeof(CxlEndpointRecord);
  if (byteOffset > directory.size() || sizeof(CxlEndpointRecord) > directory.size() - byteOffset) {
    return makeError(StatusCode::kInvalidArg, "CXL endpoint record is outside the endpoint directory");
  }
  auto bytes = directory.subspan(byteOffset, sizeof(CxlEndpointRecord));
  auto *words = reinterpret_cast<uint64_t *>(bytes.data());
  const uint64_t currentSequence = std::atomic_ref<uint64_t>(words[0]).load(std::memory_order_acquire);
  uint64_t previousSequence = 0;
  if (currentSequence == 0) {
    bool allZero = true;
    for (size_t word = 1; word < sizeof(CxlEndpointRecord) / sizeof(uint64_t); ++word) {
      allZero &= std::atomic_ref<uint64_t>(words[word]).load(std::memory_order_relaxed) == 0;
    }
    if (!allZero) {
      return makeError(StatusCode::kDataCorruption, "zero-sequence CXL endpoint record has nonzero body");
    }
    if (endpointGeneration == 0) {
      endpointGeneration = 1;
    } else if (endpointGeneration != 1) {
      return makeError(RPCCode::kStaleGeneration, "first CXL endpoint generation must be one");
    }
  } else {
    auto previous = loadCxlOwnerRecord<CxlEndpointRecord>(bytes);
    if (!previous) {
      return makeError(StatusCode::kDataCorruption, "unstable prior CXL endpoint record");
    }
    RETURN_ON_ERROR(validateEndpointRecord(*previous, layout.sessionGeneration(), endpoint, std::nullopt));
    const auto previousLifecycle = static_cast<CxlEndpointLifecycle>(loadLe32(&previous->lifecycle));
    const uint64_t previousGeneration = loadLe64(&previous->endpointGeneration);
    if (previousLifecycle != CxlEndpointLifecycle::Retired) {
      return makeError(StatusCode::kQueueConflict, "CXL endpoint already has a live or faulted owner");
    }
    if (previousGeneration == std::numeric_limits<uint64_t>::max()) {
      return makeError(RPCCode::kStaleGeneration, "CXL endpoint generation is exhausted");
    }
    if (endpointGeneration == 0) {
      endpointGeneration = previousGeneration + 1U;
    } else if (endpointGeneration != previousGeneration + 1U) {
      return makeError(RPCCode::kStaleGeneration, "CXL endpoint generation must advance exactly once");
    }
    previousSequence = loadLe64(&previous->recordSequence);
  }

  CxlEndpointState state(directory,
                         endpointCount,
                         layout.sessionGeneration(),
                         endpoint,
                         endpointGeneration,
                         capabilityBits,
                         previousSequence);
  RETURN_ON_ERROR(state.publishLocked(CxlEndpointLifecycle::Starting));
  return state;
}

Result<Void> CxlEndpointState::publish(CxlEndpointLifecycle lifecycle) {
  std::lock_guard lock(mutex_);
  if (!validTransition(lifecycle_, lifecycle)) {
    return makeError(StatusCode::kInvalidArg, "invalid CXL endpoint lifecycle transition");
  }
  return publishLocked(lifecycle);
}

Result<Void> CxlEndpointState::heartbeat() {
  std::lock_guard lock(mutex_);
  if (lifecycle_ != CxlEndpointLifecycle::Starting && lifecycle_ != CxlEndpointLifecycle::Ready &&
      lifecycle_ != CxlEndpointLifecycle::Draining) {
    return makeError(StatusCode::kInvalidArg, "cannot heartbeat an inactive CXL endpoint");
  }
  if (heartbeat_ == std::numeric_limits<uint64_t>::max()) {
    return makeError(RPCCode::kStaleGeneration, "CXL endpoint heartbeat is exhausted");
  }
  ++heartbeat_;
  return publishLocked(lifecycle_);
}

Result<Void> CxlEndpointState::fault() {
  std::lock_guard lock(mutex_);
  if (lifecycle_ == CxlEndpointLifecycle::Faulted) {
    return Void{};
  }
  if (lifecycle_ == CxlEndpointLifecycle::Free || lifecycle_ == CxlEndpointLifecycle::Retired) {
    return makeError(StatusCode::kInvalidArg, "cannot fault an inactive CXL endpoint");
  }
  return publishLocked(CxlEndpointLifecycle::Faulted);
}

Result<CxlEndpointRecord> CxlEndpointState::snapshot(EndpointId endpoint,
                                                     std::optional<uint64_t> expectedGeneration) const {
  auto bytesResult = recordBytes(endpoint);
  if (!bytesResult) {
    return makeError(std::move(bytesResult.error()));
  }
  auto snapshot = loadCxlOwnerRecord<CxlEndpointRecord>(*bytesResult);
  if (!snapshot) {
    // A preempted heartbeat writer may leave an odd sequence across all
    // bounded observations. Do not classify an unobserved record as corrupt
    // or use it to renew peer liveness. Callers retain their retry deadlines.
    return makeError(RPCCode::kTimeout, "CXL endpoint publication is not yet stable");
  }
  RETURN_ON_ERROR(validateEndpointRecord(*snapshot, sessionGeneration_, endpoint, expectedGeneration));
  return *snapshot;
}

Result<bool> CxlEndpointState::peerStale(EndpointId endpoint,
                                         Duration timeout,
                                         std::chrono::steady_clock::time_point observationTime) {
  if (timeout <= Duration::zero()) {
    return makeError(StatusCode::kInvalidArg, "CXL peer-stale timeout must be positive");
  }
  auto currentResult = snapshot(endpoint);
  if (!currentResult) {
    return makeError(std::move(currentResult.error()));
  }
  const auto &current = *currentResult;
  const auto lifecycle = static_cast<CxlEndpointLifecycle>(loadLe32(&current.lifecycle));
  if (lifecycle == CxlEndpointLifecycle::Faulted || lifecycle == CxlEndpointLifecycle::Retired) {
    return true;
  }
  const uint64_t generation = loadLe64(&current.endpointGeneration);
  const uint64_t heartbeat = loadLe64(&current.heartbeat);
  std::lock_guard lock(mutex_);
  auto [it, inserted] = observations_.try_emplace(
      endpoint.value,
      PeerObservation{.generation = generation, .heartbeat = heartbeat, .lastProgress = observationTime});
  if (inserted || it->second.generation != generation || it->second.heartbeat != heartbeat) {
    it->second = PeerObservation{.generation = generation, .heartbeat = heartbeat, .lastProgress = observationTime};
    return false;
  }
  return observationTime - it->second.lastProgress >= timeout;
}

Result<std::span<std::byte>> CxlEndpointState::recordBytes(EndpointId endpoint) const {
  if (endpoint.value == 0 || endpoint.value > endpointCount_) {
    return makeError(StatusCode::kInvalidArg, "CXL endpoint ID is outside the manifest directory");
  }
  const uint64_t byteOffset = static_cast<uint64_t>(endpoint.value - 1U) * sizeof(CxlEndpointRecord);
  if (byteOffset > directory_.size() || sizeof(CxlEndpointRecord) > directory_.size() - byteOffset) {
    return makeError(StatusCode::kInvalidArg, "CXL endpoint record is outside the endpoint directory");
  }
  return directory_.subspan(byteOffset, sizeof(CxlEndpointRecord));
}

Result<Void> CxlEndpointState::publishLocked(CxlEndpointLifecycle lifecycle) {
  if (recordSequence_ > std::numeric_limits<uint64_t>::max() - 2U) {
    return makeError(RPCCode::kStaleGeneration, "CXL endpoint record sequence is exhausted");
  }
  auto bytes = recordBytes(endpoint_);
  if (!bytes) {
    return makeError(std::move(bytes.error()));
  }
  CxlEndpointRecord record{};
  storeLe64(&record.recordSequence, recordSequence_ + 2U);
  storeLe64(&record.sessionGeneration, sessionGeneration_);
  storeLe32(&record.endpointId, endpoint_.value);
  storeLe32(&record.lifecycle, static_cast<uint32_t>(lifecycle));
  storeLe64(&record.endpointGeneration, endpointGeneration_);
  storeLe64(&record.capabilityBits, capabilityBits_);
  storeLe64(&record.heartbeat, heartbeat_);
  storeLe32(&record.recordCrc32c, endpointRecordCrc(record));
  if (!publishCxlOwnerRecord(*bytes, record)) {
    return makeError(StatusCode::kDataCorruption, "failed to publish CXL endpoint owner record");
  }
  recordSequence_ += 2U;
  lifecycle_ = lifecycle;
  return Void{};
}

}  // namespace hf3fs::net::cxl
