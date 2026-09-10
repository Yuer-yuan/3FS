#include "common/net/cxl/CxlFabric.h"
#include "common/net/RpcTrace.h"

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <fmt/format.h>
#include <limits>
#include <sys/file.h>
#include <unistd.h>

namespace hf3fs::net::cxl {
namespace {

uint32_t lifecycleCrc(const CxlFabricLifecycleRecord &record) {
  return cxlCrc32cWithZeroedU32(record, offsetof(CxlFabricLifecycleRecord, recordCrc32c));
}

bool validLifecycle(uint32_t value) {
  return value >= static_cast<uint32_t>(CxlFabricLifecycle::Init) &&
         value <= static_cast<uint32_t>(CxlFabricLifecycle::Faulted);
}

LayoutExpectation expectationFor(const CxlLayoutManifest &manifest) {
  return LayoutExpectation{
      .sessionGeneration = manifest.sessionGeneration,
      .manifestSha256 = manifest.manifestSha256,
      .totalRegionBytes = manifest.totalRegionBytes,
      .endpointCount = manifest.endpointCount,
      .laneCount = manifest.laneCount,
      .authorityEndpoint = manifest.authorityEndpoint,
  };
}

}  // namespace

Result<std::shared_ptr<CxlFabric>> CxlFabric::start(Config config) { return start(std::move(config), {}); }

Result<std::shared_ptr<CxlFabric>> CxlFabric::start(Config config, std::vector<EndpointId> activeEndpoints) {
  auto fabric = std::shared_ptr<CxlFabric>(new CxlFabric(std::move(config), std::move(activeEndpoints)));
  auto initialized = fabric->initialize();
  if (!initialized) {
    fabric->releaseResources();
    return makeError(std::move(initialized.error()));
  }
  return fabric;
}

CxlFabric::~CxlFabric() {
  if (running()) {
    (void)stopAndJoin();
  }
  releaseResources();
}

Result<Void> CxlFabric::initialize() {
  if (config_.regionPath.empty() || config_.manifest.sessionGeneration == 0 || config_.endpoint.value == 0 ||
      config_.heartbeatInterval <= Duration::zero() || config_.authorityStaleTimeout <= config_.heartbeatInterval ||
      config_.shutdownTimeout <= Duration::zero()) {
    return makeError(StatusCode::kInvalidArg, "invalid CXL fabric configuration");
  }
  RETURN_ON_ERROR(participantPosition(config_.endpoint));
  if (config_.mode == StartMode::InitializeAuthority) {
    if (config_.endpoint != config_.manifest.authorityEndpoint) {
      return makeError(StatusCode::kInvalidArg, "only the manifest authority endpoint may initialize CXL fabric");
    }
    RETURN_ON_ERROR(acquireAuthorityLock());
  } else if (!config_.authorityOwnerLock.empty() || !config_.authorityReceipt.empty()) {
    return makeError(StatusCode::kInvalidArg, "a CXL attacher must not request the authority owner lock");
  }

  auto mapped = config_.regionType == RegionType::Dax
                    ? CxlRegion::mapDax(config_.regionPath, config_.regionOffset, config_.regionLength)
                    : CxlRegion::mapFile(config_.regionPath, config_.regionLength, config_.regionOffset);
  if (!mapped) {
    return makeError(std::move(mapped.error()));
  }
  region_.emplace(std::move(*mapped));

  Result<CxlLayout> layoutResult =
      config_.mode == StartMode::InitializeAuthority
          ? CxlLayout::initializeAsAuthority(*region_, config_.manifest, config_.endpoint)
          : CxlLayout::attach(*region_, expectationFor(config_.manifest), config_.attachTimeout);
  if (!layoutResult) {
    return makeError(std::move(layoutResult.error()));
  }
  layout_.emplace(std::move(*layoutResult));
  lifecycleSequence_ = loadLe64(&layout_->lifecycle().recordSequence);
  lifecycleHeartbeat_ = loadLe64(&layout_->lifecycle().heartbeat);
  lifecycle_ = static_cast<CxlFabricLifecycle>(loadLe32(&layout_->lifecycle().lifecycle));

  auto endpoint =
      CxlEndpointState::claim(*layout_, config_.endpoint, config_.endpointGeneration, config_.capabilityBits);
  if (!endpoint) {
    return makeError(std::move(endpoint.error()));
  }
  config_.endpointGeneration = endpoint->endpointGeneration();
  endpointState_.emplace(std::move(*endpoint));

  progressEngine_ = std::make_shared<CxlProgressEngine>(config_.poll);
  auto progressStarted = progressEngine_->start();
  if (!progressStarted) {
    (void)endpointState_->fault();
    return makeError(std::move(progressStarted.error()));
  }
  auto ready = endpointState_->publish(CxlEndpointLifecycle::Ready);
  if (!ready) {
    progressEngine_->stopAndJoin();
    (void)endpointState_->fault();
    return makeError(std::move(ready.error()));
  }

  acceptingSubmissions_.store(true, std::memory_order_release);
  stopping_.store(false, std::memory_order_release);
  running_.store(true, std::memory_order_release);
  try {
    heartbeatThread_ = std::thread(&CxlFabric::heartbeatLoop, this);
  } catch (...) {
    acceptingSubmissions_.store(false, std::memory_order_release);
    running_.store(false, std::memory_order_release);
    progressEngine_->stopAndJoin();
    (void)endpointState_->fault();
    return makeError(RPCCode::kDataPlaneInitFailed, "failed to start CXL fabric heartbeat thread");
  }
  return Void{};
}

Result<CxlFabric::ParticipantPosition> CxlFabric::participantPosition(EndpointId endpoint) const {
  if (endpoint.value == 0 || endpoint.value > config_.manifest.endpointCount) {
    return makeError(StatusCode::kInvalidConfig, "CXL endpoint is outside the manifest");
  }
  if (activeEndpoints_.empty()) {
    return ParticipantPosition{endpoint.value - 1U, config_.manifest.endpointCount};
  }
  if (activeEndpoints_.size() > config_.manifest.endpointCount ||
      activeEndpoints_.size() > std::numeric_limits<uint32_t>::max()) {
    return makeError(StatusCode::kInvalidConfig, "CXL active participant set is invalid");
  }
  auto found = std::find(activeEndpoints_.begin(), activeEndpoints_.end(), endpoint);
  if (found == activeEndpoints_.end() ||
      std::find(found + 1, activeEndpoints_.end(), endpoint) != activeEndpoints_.end()) {
    return makeError(StatusCode::kInvalidConfig, "CXL endpoint is absent or duplicated in the participant set");
  }
  for (auto current = activeEndpoints_.begin(); current != activeEndpoints_.end(); ++current) {
    const auto participant = *current;
    if (participant.value == 0 || participant.value > config_.manifest.endpointCount) {
      return makeError(StatusCode::kInvalidConfig, "CXL participant is outside the manifest");
    }
    if (std::find(activeEndpoints_.begin(), current, participant) != current) {
      return makeError(StatusCode::kInvalidConfig, "CXL participant set contains a duplicate endpoint");
    }
  }
  return ParticipantPosition{static_cast<uint32_t>(found - activeEndpoints_.begin()),
                             static_cast<uint32_t>(activeEndpoints_.size())};
}

Result<Void> CxlFabric::acquireAuthorityLock() {
  if (config_.authorityOwnerLock.empty() || config_.authorityReceipt.empty()) {
    return makeError(StatusCode::kInvalidArg, "CXL authority requires a runner-owned lock and receipt");
  }
  const int fd = ::open(config_.authorityOwnerLock.c_str(), O_RDONLY | O_CLOEXEC);
  if (fd < 0) {
    return makeError(RPCCode::kDataPlaneOpenFailed,
                     fmt::format("open CXL authority lock failed: {}", std::strerror(errno)));
  }
  if (::flock(fd, LOCK_EX | LOCK_NB) != 0) {
    ::close(fd);
    return makeError(StatusCode::kQueueConflict, "another local CXL fabric authority owns the run lock");
  }

  std::string contents;
  std::array<char, 512> buffer{};
  while (true) {
    const auto count = ::read(fd, buffer.data(), buffer.size());
    if (count > 0) {
      contents.append(buffer.data(), static_cast<size_t>(count));
      continue;
    }
    if (count < 0 && errno == EINTR) {
      continue;
    }
    if (count < 0) {
      ::flock(fd, LOCK_UN);
      ::close(fd);
      return makeError(RPCCode::kDataPlaneOpenFailed,
                       fmt::format("read CXL authority receipt failed: {}", std::strerror(errno)));
    }
    break;
  }
  if (contents != config_.authorityReceipt) {
    ::flock(fd, LOCK_UN);
    ::close(fd);
    return makeError(StatusCode::kInvalidConfig, "CXL authority receipt does not match the runner-owned lock");
  }
  authorityLockFd_ = fd;
  return Void{};
}

Result<Void> CxlFabric::stopAndJoin() {
  bool expected = false;
  if (!stopping_.compare_exchange_strong(expected, true, std::memory_order_acq_rel)) {
    return Void{};
  }
  acceptingSubmissions_.store(false, std::memory_order_release);

  waitCondition_.notify_all();
  if (heartbeatThread_.joinable()) {
    heartbeatThread_.join();
  }

  const auto backgroundCode = backgroundError_.load(std::memory_order_acquire);
  Result<Void> shutdownResult = backgroundCode == StatusCode::kOK
                                    ? Result<Void>{Void{}}
                                    : makeError(backgroundCode, "CXL fabric background lifecycle monitor failed");
  if (config_.mode == StartMode::InitializeAuthority) {
    auto draining = publishLifecycle(CxlFabricLifecycle::Draining);
    if (!draining && shutdownResult) {
      shutdownResult = makeError(std::move(draining.error()));
    }
  }

  if (endpointState_ && endpointState_->lifecycle() == CxlEndpointLifecycle::Ready) {
    auto draining = endpointState_->publish(CxlEndpointLifecycle::Draining);
    if (!draining && shutdownResult) {
      shutdownResult = makeError(std::move(draining.error()));
    }
  }

  if (config_.mode == StartMode::InitializeAuthority && shutdownResult) {
    auto peers = waitForPeerRetirement();
    if (!peers) {
      shutdownResult = makeError(std::move(peers.error()));
    }
  }

  if (progressEngine_) {
    progressEngine_->stopAndJoin();
  }
  if (endpointState_ && endpointState_->lifecycle() == CxlEndpointLifecycle::Draining) {
    auto retired = endpointState_->publish(CxlEndpointLifecycle::Retired);
    if (!retired && shutdownResult) {
      shutdownResult = makeError(std::move(retired.error()));
    }
  }

  if (config_.mode == StartMode::InitializeAuthority) {
    auto finalState = publishLifecycle(shutdownResult ? CxlFabricLifecycle::Retired : CxlFabricLifecycle::Faulted);
    if (!finalState && shutdownResult) {
      shutdownResult = makeError(std::move(finalState.error()));
    }
  } else if (!shutdownResult && endpointState_ && endpointState_->lifecycle() != CxlEndpointLifecycle::Retired) {
    (void)endpointState_->fault();
  }

  running_.store(false, std::memory_order_release);
  releaseResources();
  return shutdownResult;
}

Result<Void> CxlFabric::check() const {
  const auto code = backgroundError_.load(std::memory_order_acquire);
  if (code != StatusCode::kOK) {
    return makeError(code, "CXL fabric background lifecycle monitor failed");
  }
  if (!running()) {
    return makeError(RPCCode::kDataPlaneNotInitialized, "CXL fabric is not running");
  }
  return Void{};
}

Result<CxlFabricLifecycleRecord> CxlFabric::lifecycleSnapshot() const {
  if (!region_ || !layout_) {
    return makeError(RPCCode::kDataPlaneNotInitialized, "CXL fabric mapping is unavailable");
  }
  const uint64_t offset = loadLe64(&layout_->superblock().header.lifecycleRecordOffset);
  auto bytes = region_->checkedRange(offset, sizeof(CxlFabricLifecycleRecord), alignof(CxlFabricLifecycleRecord));
  if (!bytes) {
    return makeError(std::move(bytes.error()));
  }
  auto record = loadCxlOwnerRecord<CxlFabricLifecycleRecord>(*bytes);
  if (!record) {
    return makeError(RPCCode::kTimeout, "CXL fabric lifecycle publication is not yet stable");
  }
  if (loadLe64(&record->sessionGeneration) != config_.manifest.sessionGeneration ||
      loadLe64(&record->authorityGeneration) != config_.manifest.authorityGeneration ||
      loadLe32(&record->authorityEndpoint) != config_.manifest.authorityEndpoint.value ||
      loadLe32(&record->superblockCrc32c) != loadLe32(&layout_->superblock().header.superblockCrc32c) ||
      !validLifecycle(loadLe32(&record->lifecycle)) ||
      std::any_of(record->reserved.begin(),
                  record->reserved.end(),
                  [](std::byte value) { return value != std::byte{}; }) ||
      loadLe32(&record->recordCrc32c) != lifecycleCrc(*record)) {
    return makeError(StatusCode::kDataCorruption, "untrustworthy CXL fabric lifecycle snapshot");
  }
  return *record;
}

Result<Void> CxlFabric::publishLifecycle(CxlFabricLifecycle lifecycle) {
  std::lock_guard lock(lifecycleMutex_);
  if (config_.mode != StartMode::InitializeAuthority || !region_ || !layout_) {
    return makeError(StatusCode::kInvalidArg, "only the live CXL authority may publish fabric lifecycle");
  }
  if (lifecycleSequence_ > std::numeric_limits<uint64_t>::max() - 2U) {
    return makeError(RPCCode::kStaleGeneration, "CXL fabric lifecycle sequence is exhausted");
  }
  if (lifecycleHeartbeat_ == std::numeric_limits<uint64_t>::max()) {
    return makeError(RPCCode::kStaleGeneration, "CXL fabric heartbeat is exhausted");
  }
  const uint64_t offset = loadLe64(&layout_->superblock().header.lifecycleRecordOffset);
  auto bytes = region_->checkedRange(offset, sizeof(CxlFabricLifecycleRecord), alignof(CxlFabricLifecycleRecord));
  if (!bytes) {
    return makeError(std::move(bytes.error()));
  }
  CxlFabricLifecycleRecord record{};
  storeLe64(&record.recordSequence, lifecycleSequence_ + 2U);
  storeLe64(&record.sessionGeneration, config_.manifest.sessionGeneration);
  storeLe64(&record.authorityGeneration, config_.manifest.authorityGeneration);
  storeLe64(&record.heartbeat, lifecycleHeartbeat_ + 1U);
  storeLe32(&record.authorityEndpoint, config_.manifest.authorityEndpoint.value);
  storeLe32(&record.superblockCrc32c, loadLe32(&layout_->superblock().header.superblockCrc32c));
  storeLe32(&record.lifecycle, static_cast<uint32_t>(lifecycle));
  storeLe32(&record.recordCrc32c, lifecycleCrc(record));
  if (!publishCxlOwnerRecord(*bytes, record)) {
    return makeError(StatusCode::kDataCorruption, "failed to publish CXL fabric lifecycle record");
  }
  lifecycleSequence_ += 2U;
  ++lifecycleHeartbeat_;
  lifecycle_ = lifecycle;
  return Void{};
}

Result<Void> CxlFabric::waitForPeerRetirement() {
  const auto deadline = std::chrono::steady_clock::now() + config_.shutdownTimeout;
  uint32_t lastBlockedEndpoint = 0;
  while (std::chrono::steady_clock::now() < deadline) {
    bool allRetired = true;
    const auto participantCount = activeEndpoints_.empty() ? config_.manifest.endpointCount : activeEndpoints_.size();
    for (size_t index = 0; index < participantCount; ++index) {
      const auto endpoint = activeEndpoints_.empty() ? EndpointId{static_cast<uint32_t>(index + 1U)}
                                                     : activeEndpoints_[index];
      if (endpoint == config_.endpoint) {
        continue;
      }
      auto peer = endpointState_->snapshot(endpoint);
      if (!peer || static_cast<CxlEndpointLifecycle>(loadLe32(&peer->lifecycle)) != CxlEndpointLifecycle::Retired) {
        if (RpcTrace::enabled() && endpoint.value != lastBlockedEndpoint) {
          lastBlockedEndpoint = endpoint.value;
          rpcTrace(0, 0, 0, "retirement_wait", peer ? 0 : peer.error().code(),
                   fmt::format("endpoint={} generation={} lifecycle={} heartbeat={}", endpoint.value,
                               peer ? loadLe64(&peer->endpointGeneration) : 0,
                               peer ? loadLe32(&peer->lifecycle) : 0,
                               peer ? loadLe64(&peer->heartbeat) : 0));
        }
        allRetired = false;
        break;
      }
    }
    if (allRetired) {
      return Void{};
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  }
  return makeError(RPCCode::kDataPlaneHandshakeFailed,
                   "timed out waiting for CXL endpoint retirement acknowledgements");
}

void CxlFabric::heartbeatLoop() noexcept {
  uint64_t observedAuthorityHeartbeat = lifecycleHeartbeat_;
  auto authorityProgress = std::chrono::steady_clock::now();
  while (!stopping_.load(std::memory_order_acquire)) {
    auto endpointHeartbeat = endpointState_->heartbeat();
    if (!endpointHeartbeat) {
      recordBackgroundError(endpointHeartbeat.error());
      return;
    }
    if (config_.mode == StartMode::InitializeAuthority) {
      auto globalHeartbeat = publishLifecycle(CxlFabricLifecycle::Ready);
      if (!globalHeartbeat) {
        recordBackgroundError(globalHeartbeat.error());
        (void)endpointState_->fault();
        return;
      }
    } else {
      auto global = lifecycleSnapshot();
      if (!global && global.error().code() != RPCCode::kTimeout) {
        recordBackgroundError(global.error());
        (void)endpointState_->fault();
        return;
      }
      const auto now = std::chrono::steady_clock::now();
      if (global) {
        const auto globalState = static_cast<CxlFabricLifecycle>(loadLe32(&global->lifecycle));
        if (globalState != CxlFabricLifecycle::Ready) {
          recordBackgroundError(Status(RPCCode::kStaleGeneration, "CXL authority left READY state"));
          return;
        }
        const uint64_t heartbeat = loadLe64(&global->heartbeat);
        if (heartbeat != observedAuthorityHeartbeat) {
          observedAuthorityHeartbeat = heartbeat;
          authorityProgress = now;
        }
      }
      // A preempted single writer may leave an odd sequence across several
      // observations. Only a validated heartbeat extends its liveness budget;
      // a permanently unfinished publication still expires at the deadline.
      if (now - authorityProgress >= config_.authorityStaleTimeout) {
        recordBackgroundError(Status(RPCCode::kStaleGeneration, "CXL authority heartbeat stopped"));
        (void)endpointState_->fault();
        return;
      }
    }

    std::unique_lock lock(waitMutex_);
    waitCondition_.wait_for(lock, std::chrono::nanoseconds(config_.heartbeatInterval), [this] {
      return stopping_.load(std::memory_order_acquire);
    });
  }
}

void CxlFabric::recordBackgroundError(Status status) noexcept {
  // Observing a background error must also observe the closed submission gate.
  acceptingSubmissions_.store(false, std::memory_order_release);
  auto expected = StatusCode::kOK;
  if (backgroundError_.compare_exchange_strong(expected, status.code(), std::memory_order_acq_rel)) {
    std::fprintf(stderr,
                 "CXL fabric endpoint=%u background error: %s\n",
                 config_.endpoint.value,
                 status.describe().c_str());
  }
}

void CxlFabric::releaseResources() noexcept {
  if (heartbeatThread_.joinable()) {
    stopping_.store(true, std::memory_order_release);
    waitCondition_.notify_all();
    heartbeatThread_.join();
  }
  if (progressEngine_) {
    progressEngine_->stopAndJoin();
    progressEngine_.reset();
  }
  endpointState_.reset();
  layout_.reset();
  region_.reset();
  if (authorityLockFd_ >= 0) {
    (void)::flock(authorityLockFd_, LOCK_UN);
    (void)::close(authorityLockFd_);
    authorityLockFd_ = -1;
  }
}

}  // namespace hf3fs::net::cxl
