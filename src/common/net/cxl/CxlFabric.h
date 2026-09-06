#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <filesystem>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

#include "common/net/cxl/CxlEndpointState.h"
#include "common/net/cxl/CxlProgressEngine.h"
#include "common/net/cxl/CxlRegion.h"

namespace hf3fs::net::cxl {

class CxlFabric {
 public:
  struct ParticipantPosition {
    uint32_t index{};
    uint32_t count{};
  };

  enum class StartMode {
    InitializeAuthority,
    Attach,
  };

  enum class RegionType {
    File,
    Dax,
  };

  struct Config {
    StartMode mode{StartMode::Attach};
    RegionType regionType{RegionType::Dax};
    std::filesystem::path regionPath;
    uint64_t regionOffset{};
    uint64_t regionLength{};
    CxlLayoutManifest manifest;
    EndpointId endpoint;
    uint64_t endpointGeneration{};
    uint64_t capabilityBits{};
    std::chrono::milliseconds attachTimeout{1000};
    Duration heartbeatInterval{std::chrono::milliseconds(10)};
    Duration authorityStaleTimeout{std::chrono::seconds(1)};
    Duration shutdownTimeout{std::chrono::seconds(1)};
    CxlPollConfig poll{};
    std::filesystem::path authorityOwnerLock;
    std::string authorityReceipt;
  };

  static Result<std::shared_ptr<CxlFabric>> start(Config config);
  static Result<std::shared_ptr<CxlFabric>> start(Config config, std::vector<EndpointId> activeEndpoints);
  ~CxlFabric();

  CxlFabric(const CxlFabric &) = delete;
  CxlFabric &operator=(const CxlFabric &) = delete;

  Result<Void> stopAndJoin();
  Result<Void> check() const;
  // kTimeout means no stable publication was observed; stable malformed
  // records return kDataCorruption and must never extend authority liveness.
  Result<CxlFabricLifecycleRecord> lifecycleSnapshot() const;
  Result<ParticipantPosition> participantPosition(EndpointId endpoint) const;

  bool running() const noexcept { return running_.load(std::memory_order_acquire); }
  bool acceptingSubmissions() const noexcept { return acceptingSubmissions_.load(std::memory_order_acquire); }
  bool mapped() const noexcept { return region_.has_value(); }
  CxlRegion &region() { return *region_; }
  CxlLayout &layout() { return *layout_; }
  CxlEndpointState &endpointState() { return *endpointState_; }
  const std::shared_ptr<CxlProgressEngine> &progressEngine() const noexcept { return progressEngine_; }
  const Config &config() const noexcept { return config_; }

 private:
  explicit CxlFabric(Config config, std::vector<EndpointId> activeEndpoints)
      : config_(std::move(config)),
        activeEndpoints_(std::move(activeEndpoints)) {}

  Result<Void> initialize();
  Result<Void> acquireAuthorityLock();
  Result<Void> publishLifecycle(CxlFabricLifecycle lifecycle);
  Result<Void> waitForPeerRetirement();
  void heartbeatLoop() noexcept;
  void recordBackgroundError(Status status) noexcept;
  void releaseResources() noexcept;

  Config config_;
  std::vector<EndpointId> activeEndpoints_;
  int authorityLockFd_{-1};
  std::optional<CxlRegion> region_;
  std::optional<CxlLayout> layout_;
  std::optional<CxlEndpointState> endpointState_;
  std::shared_ptr<CxlProgressEngine> progressEngine_;
  std::thread heartbeatThread_;
  mutable std::mutex waitMutex_;
  std::condition_variable waitCondition_;
  std::mutex lifecycleMutex_;
  uint64_t lifecycleSequence_{};
  uint64_t lifecycleHeartbeat_{};
  CxlFabricLifecycle lifecycle_{CxlFabricLifecycle::Uninitialized};
  std::atomic<status_code_t> backgroundError_{StatusCode::kOK};
  std::atomic<bool> acceptingSubmissions_{};
  std::atomic<bool> stopping_{};
  std::atomic<bool> running_{};
};

}  // namespace hf3fs::net::cxl
