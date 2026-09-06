#pragma once

#include <chrono>
#include <cstdint>
#include <mutex>
#include <optional>
#include <span>
#include <unordered_map>

#include "common/net/cxl/CxlLayout.h"
#include "common/utils/Duration.h"
#include "common/utils/Result.h"

namespace hf3fs::net::cxl {

class CxlEndpointState {
 public:
  static Result<CxlEndpointState> claim(CxlLayout &layout,
                                        EndpointId endpoint,
                                        uint64_t endpointGeneration,
                                        uint64_t capabilityBits);

  CxlEndpointState(CxlEndpointState &&other) noexcept;
  CxlEndpointState &operator=(CxlEndpointState &&other) noexcept;
  CxlEndpointState(const CxlEndpointState &) = delete;
  CxlEndpointState &operator=(const CxlEndpointState &) = delete;

  Result<Void> publish(CxlEndpointLifecycle lifecycle);
  Result<Void> heartbeat();
  Result<Void> fault();

  Result<CxlEndpointRecord> snapshot(EndpointId endpoint,
                                     std::optional<uint64_t> expectedGeneration = std::nullopt) const;
  Result<CxlEndpointRecord> snapshot() const { return snapshot(endpoint_, endpointGeneration_); }

  Result<bool> peerStale(EndpointId endpoint,
                         Duration timeout,
                         std::chrono::steady_clock::time_point observationTime = std::chrono::steady_clock::now());

  EndpointId endpoint() const noexcept { return endpoint_; }
  uint64_t endpointGeneration() const noexcept { return endpointGeneration_; }
  CxlEndpointLifecycle lifecycle() const noexcept { return lifecycle_; }

 private:
  struct PeerObservation {
    uint64_t generation{};
    uint64_t heartbeat{};
    std::chrono::steady_clock::time_point lastProgress;
  };

  CxlEndpointState(std::span<std::byte> directory,
                   uint32_t endpointCount,
                   uint64_t sessionGeneration,
                   EndpointId endpoint,
                   uint64_t endpointGeneration,
                   uint64_t capabilityBits,
                   uint64_t recordSequence) noexcept;

  Result<std::span<std::byte>> recordBytes(EndpointId endpoint) const;
  Result<Void> publishLocked(CxlEndpointLifecycle lifecycle);

  std::span<std::byte> directory_;
  uint32_t endpointCount_{};
  uint64_t sessionGeneration_{};
  EndpointId endpoint_{};
  uint64_t endpointGeneration_{};
  uint64_t capabilityBits_{};
  uint64_t recordSequence_{};
  uint64_t heartbeat_{};
  CxlEndpointLifecycle lifecycle_{CxlEndpointLifecycle::Free};
  mutable std::mutex mutex_;
  std::unordered_map<uint32_t, PeerObservation> observations_;
};

}  // namespace hf3fs::net::cxl
