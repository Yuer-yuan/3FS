#include "common/net/cxl/CxlConnectService.h"

#include <algorithm>
#include <atomic>
#include <cstring>
#include <limits>

#include "common/net/cxl/CxlBulkTransfer.h"

namespace hf3fs::net::cxl {
namespace {

constexpr uint64_t kCursorPageBytes = 4096;

bool allZero(const CxlConnectionNonce &nonce) { return nonce.low == 0 && nonce.high == 0; }

Result<uint64_t> laneStride(const CxlRange &range, uint32_t laneCount, uint64_t required, uint64_t alignment) {
  if (laneCount == 0 || range.length % laneCount != 0) {
    return makeError(StatusCode::kInvalidConfig, "CXL lane range cannot be partitioned exactly");
  }
  const uint64_t stride = range.length / laneCount;
  if (stride < required || stride % alignment != 0) {
    return makeError(StatusCode::kInvalidConfig, "CXL lane partition is too small or misaligned");
  }
  return stride;
}

uint32_t ownerRecordCrc(const CxlLaneOwnerRecord &record) {
  return cxlCrc32cWithZeroedU32(record, offsetof(CxlLaneOwnerRecord, crc32c));
}

Result<std::span<std::byte>> checkedOwnerRecord(CxlFabric &fabric, uint64_t offset) {
  const auto &range = fabric.layout().ranges()[static_cast<size_t>(CxlRangeKind::LaneDirectory) - 1U];
  if (offset < range.offset || sizeof(CxlLaneOwnerRecord) > range.length ||
      offset - range.offset > range.length - sizeof(CxlLaneOwnerRecord)) {
    return makeError(StatusCode::kInvalidArg, "CXL lane owner record is outside the lane directory");
  }
  return fabric.region().checkedRange(offset, sizeof(CxlLaneOwnerRecord), alignof(CxlLaneOwnerRecord));
}

Result<std::optional<CxlLaneOwnerRecord>> ownerRecordSnapshot(CxlFabric &fabric, uint64_t offset) {
  auto bytes = checkedOwnerRecord(fabric, offset);
  if (!bytes) {
    return makeError(std::move(bytes.error()));
  }
  auto *words = reinterpret_cast<uint64_t *>(bytes->data());
  const uint64_t sequence = std::atomic_ref<uint64_t>(words[0]).load(std::memory_order_acquire);
  if (sequence == 0) {
    for (size_t word = 1; word < sizeof(CxlLaneOwnerRecord) / sizeof(uint64_t); ++word) {
      if (std::atomic_ref<uint64_t>(words[word]).load(std::memory_order_relaxed) != 0) {
        return makeError(StatusCode::kDataCorruption, "zero-sequence CXL lane owner record has nonzero body");
      }
    }
    return std::optional<CxlLaneOwnerRecord>{};
  }
  auto record = loadCxlOwnerRecord<CxlLaneOwnerRecord>(*bytes);
  if (!record || loadLe32(&record->crc32c) != ownerRecordCrc(*record)) {
    return makeError(StatusCode::kDataCorruption, "unstable or corrupt CXL lane owner record");
  }
  return std::optional<CxlLaneOwnerRecord>{*record};
}

bool terminalOwner(const std::optional<CxlLaneOwnerRecord> &record) {
  if (!record) {
    return true;
  }
  const auto lifecycle = static_cast<CxlLaneLifecycle>(loadLe32(&record->lifecycle));
  return lifecycle == CxlLaneLifecycle::Retired || lifecycle == CxlLaneLifecycle::Faulted;
}

Result<uint64_t> nextOwnerSequence(const std::optional<CxlLaneOwnerRecord> &record) {
  if (!record) {
    return 2U;
  }
  const uint64_t sequence = loadLe64(&record->recordSequence);
  if (sequence > std::numeric_limits<uint64_t>::max() - 2U) {
    return makeError(RPCCode::kStaleGeneration, "CXL lane owner sequence is exhausted");
  }
  return sequence + 2U;
}

Result<Void> publishOwnerRecord(CxlFabric &fabric,
                                uint64_t offset,
                                const CxlConnectRsp &descriptor,
                                uint64_t endpointGeneration,
                                CxlLaneLifecycle lifecycle,
                                uint64_t sequence) {
  auto bytes = checkedOwnerRecord(fabric, offset);
  if (!bytes) {
    return makeError(std::move(bytes.error()));
  }
  CxlLaneOwnerRecord record{};
  storeLe64(&record.recordSequence, sequence);
  storeLe64(&record.sessionGeneration, descriptor.session_generation);
  storeLe64(&record.laneGeneration, descriptor.lane_generation);
  storeLe64(record.connectionNonce.data(), descriptor.connection_nonce.low);
  storeLe64(record.connectionNonce.data() + sizeof(uint64_t), descriptor.connection_nonce.high);
  storeLe64(&record.capabilityBits, descriptor.capability_bits);
  storeLe32(&record.lifecycle, static_cast<uint32_t>(lifecycle));
  storeLe64(&record.endpointGeneration, endpointGeneration);
  storeLe32(&record.crc32c, ownerRecordCrc(record));
  if (!publishCxlOwnerRecord(*bytes, record)) {
    return makeError(StatusCode::kDataCorruption, "failed to publish CXL lane owner record");
  }
  return Void{};
}

Result<Void> validateOwnerRecord(CxlFabric &fabric,
                                 uint64_t offset,
                                 const CxlConnectRsp &descriptor,
                                 uint64_t endpointGeneration) {
  auto bytes = checkedOwnerRecord(fabric, offset);
  if (!bytes) {
    return makeError(std::move(bytes.error()));
  }
  auto record = loadCxlOwnerRecord<CxlLaneOwnerRecord>(*bytes);
  CxlConnectionNonce nonce{};
  if (record) {
    nonce.low = loadLe64(record->connectionNonce.data());
    nonce.high = loadLe64(record->connectionNonce.data() + sizeof(uint64_t));
  }
  if (!record || loadLe64(&record->sessionGeneration) != descriptor.session_generation ||
      loadLe64(&record->laneGeneration) != descriptor.lane_generation || nonce != descriptor.connection_nonce ||
      loadLe64(&record->capabilityBits) != descriptor.capability_bits ||
      loadLe32(&record->lifecycle) != static_cast<uint32_t>(CxlLaneLifecycle::Ready) ||
      loadLe64(&record->endpointGeneration) != endpointGeneration ||
      loadLe32(&record->crc32c) != ownerRecordCrc(*record)) {
    return makeError(RPCCode::kDataPlaneHandshakeFailed, "CXL lane owner record does not match bootstrap binding");
  }
  return Void{};
}

bool matchesOwnerRecord(const CxlLaneOwnerRecord &record,
                        const CxlConnectRsp &descriptor,
                        uint64_t endpointGeneration) {
  CxlConnectionNonce nonce{};
  nonce.low = loadLe64(record.connectionNonce.data());
  nonce.high = loadLe64(record.connectionNonce.data() + sizeof(uint64_t));
  return loadLe64(&record.sessionGeneration) == descriptor.session_generation &&
         loadLe64(&record.laneGeneration) == descriptor.lane_generation && nonce == descriptor.connection_nonce &&
         loadLe64(&record.capabilityBits) == descriptor.capability_bits &&
         loadLe64(&record.endpointGeneration) == endpointGeneration;
}

Result<Void> transitionOwnerRecord(CxlFabric &fabric,
                                   uint64_t offset,
                                   const CxlConnectRsp &descriptor,
                                   uint64_t endpointGeneration,
                                   CxlLaneLifecycle lifecycle) {
  auto bytes = checkedOwnerRecord(fabric, offset);
  if (!bytes) {
    return makeError(std::move(bytes.error()));
  }
  auto previous = loadCxlOwnerRecord<CxlLaneOwnerRecord>(*bytes);
  if (!previous || !matchesOwnerRecord(*previous, descriptor, endpointGeneration) ||
      loadLe32(&previous->crc32c) != ownerRecordCrc(*previous)) {
    return makeError(RPCCode::kStaleGeneration, "CXL lane owner changed before lifecycle transition");
  }
  const auto previousLifecycle = static_cast<CxlLaneLifecycle>(loadLe32(&previous->lifecycle));
  if (previousLifecycle == CxlLaneLifecycle::Retired || previousLifecycle == CxlLaneLifecycle::Faulted) {
    return Void{};
  }
  const uint64_t previousSequence = loadLe64(&previous->recordSequence);
  if (previousSequence > std::numeric_limits<uint64_t>::max() - 2U) {
    return makeError(RPCCode::kStaleGeneration, "CXL lane owner sequence is exhausted");
  }
  return publishOwnerRecord(fabric, offset, descriptor, endpointGeneration, lifecycle, previousSequence + 2U);
}

bool matchesActivation(const CxlConnectRsp &descriptor, const CxlActivateReq &req) {
  return descriptor.session_generation == req.session_generation && descriptor.lane_id == req.lane_id &&
         descriptor.lane_generation == req.lane_generation && descriptor.requester_endpoint == req.requester_endpoint &&
         descriptor.requester_endpoint_generation == req.requester_endpoint_generation &&
         descriptor.target_endpoint == req.target_endpoint &&
         descriptor.target_endpoint_generation == req.target_endpoint_generation &&
         descriptor.service_plane == req.service_plane && descriptor.connection_nonce == req.connection_nonce;
}

CxlSocket::PeerCheck peerCheck(std::shared_ptr<CxlFabric> fabric,
                               const CxlConnectRsp &descriptor,
                               bool requester) {
  return [fabric = std::move(fabric), descriptor, requester]() -> Result<Void> {
    const auto offset = requester ? descriptor.acceptor_owner_record_offset : descriptor.requester_owner_record_offset;
    const auto generation = requester ? descriptor.target_endpoint_generation : descriptor.requester_endpoint_generation;
    auto record = ownerRecordSnapshot(*fabric, offset);
    if (!record) {
      return makeError(std::move(record.error()));
    }
    if (!*record || !matchesOwnerRecord(**record, descriptor, generation)) {
      return makeError(RPCCode::kStaleGeneration, "CXL peer lane binding changed");
    }
    const auto lifecycle = static_cast<CxlLaneLifecycle>(loadLe32(&(**record).lifecycle));
    if (lifecycle == CxlLaneLifecycle::Retired) {
      return makeError(RPCCode::kSocketClosed);
    }
    if (lifecycle != CxlLaneLifecycle::Ready) {
      return makeError(RPCCode::kBulkTransferError, "CXL peer lane is not ready");
    }
    return Void{};
  };
}

}  // namespace

CxlLaneConfig CxlConnectRsp::laneConfig() const {
  CxlLaneConfig config{
      .sessionGeneration = session_generation,
      .laneGeneration = lane_generation,
      .depth = queue_depth,
      .cellBytes = cell_bytes,
  };
  const std::array<const CxlConnectDirection *, 2> directions{&submission, &completion};
  for (size_t index = 0; index < config.directions.size(); ++index) {
    config.directions[index] = CxlLaneDirectionLayout{
        .producerCursorOffset = directions[index]->producer_cursor_offset,
        .consumerCursorOffset = directions[index]->consumer_cursor_offset,
        .deliveredRecordOffset = directions[index]->delivered_record_offset,
        .frameRingOffset = directions[index]->frame_ring_offset,
        .payloadCellsOffset = directions[index]->payload_cells_offset,
    };
  }
  config.laneId = lane_id;
  config.requesterEndpoint = requester_endpoint;
  config.targetEndpoint = target_endpoint;
  return config;
}

CxlActivateReq CxlConnectService::activationRequest(const CxlConnectRsp &rsp) {
  CxlActivateReq req;
  req.session_generation = rsp.session_generation;
  req.lane_id = rsp.lane_id;
  req.lane_generation = rsp.lane_generation;
  req.requester_endpoint = rsp.requester_endpoint;
  req.requester_endpoint_generation = rsp.requester_endpoint_generation;
  req.target_endpoint = rsp.target_endpoint;
  req.target_endpoint_generation = rsp.target_endpoint_generation;
  req.service_plane = rsp.service_plane;
  req.connection_nonce = rsp.connection_nonce;
  return req;
}

void CxlConnectMetrics::accepted(size_t metadataBytes) noexcept {
  metadataRequests_.fetch_add(1, std::memory_order_relaxed);
  metadataBytes_.fetch_add(metadataBytes, std::memory_order_relaxed);
}

void CxlConnectMetrics::rejected() noexcept { rejectedRequests_.fetch_add(1, std::memory_order_relaxed); }

CxlConnectMetricsSnapshot CxlConnectMetrics::snapshot() const noexcept {
  return CxlConnectMetricsSnapshot{
      .metadataRequests = metadataRequests_.load(std::memory_order_relaxed),
      .metadataBytes = metadataBytes_.load(std::memory_order_relaxed),
      .bootstrapServingBytes = 0,
      .rejectedRequests = rejectedRequests_.load(std::memory_order_relaxed),
  };
}

CxlConnectService::CxlConnectService(std::shared_ptr<CxlFabric> fabric,
                                     AcceptFn accept,
                                     std::shared_ptr<CxlConnectMetrics> metrics)
    : fabric_(std::move(fabric)),
      bulkTransfer_(fabric_ ? std::make_shared<CxlBulkTransfer>(fabric_) : nullptr),
      accept_(std::move(accept)),
      metrics_(metrics ? std::move(metrics) : std::make_shared<CxlConnectMetrics>()),
      leases_(std::make_shared<LaneLeaseRegistry>(fabric_ ? fabric_->config().manifest.laneCount : 0)) {}

CxlSocket::CloseHook CxlConnectService::acceptorCloseHook(const CxlConnectRsp &descriptor) const {
  auto fabric = fabric_;
  auto leases = leases_;
  return [fabric = std::move(fabric), leases = std::move(leases), descriptor](bool faulted) noexcept {
    if (fabric) {
      (void)transitionOwnerRecord(*fabric,
                                  descriptor.acceptor_owner_record_offset,
                                  descriptor,
                                  descriptor.target_endpoint_generation,
                                  faulted ? CxlLaneLifecycle::Faulted : CxlLaneLifecycle::Retired);
    }
    std::lock_guard lock(leases->mutex);
    if (descriptor.lane_id != 0 && descriptor.lane_id <= leases->activeLanes.size()) {
      leases->activeLanes[descriptor.lane_id - 1U] = false;
    }
    const ConnectionKey key{descriptor.requester_endpoint,
                            descriptor.requester_endpoint_generation,
                            descriptor.connection_nonce};
    std::erase(leases->activeConnections, key);
  };
}

CoTryTask<CxlConnectRsp> CxlConnectService::connect(serde::CallContext &ctx, const CxlConnectReq &req) {
  if (!ctx.transport() || ctx.transport()->kind() != TransportKind::TCP ||
      ctx.transport()->servicePlane() != ServicePlane::Control) {
    co_return makeError(StatusCode::kInvalidArg, "CXL bootstrap is accepted only over the TCP Control plane");
  }
  co_return connectForTest(req);
}

CoTryTask<CxlActivateRsp> CxlConnectService::activate(serde::CallContext &ctx, const CxlActivateReq &req) {
  if (!ctx.transport() || ctx.transport()->kind() != TransportKind::TCP ||
      ctx.transport()->servicePlane() != ServicePlane::Control) {
    co_return makeError(StatusCode::kInvalidArg, "CXL activation is accepted only over the TCP Control plane");
  }
  co_return activateForTest(req);
}

Result<CxlConnectRsp> CxlConnectService::connectForTest(const CxlConnectReq &req) {
  retireCanceledPending();
  auto result = validateAndAllocate(req);
  if (!result) {
    metrics_->rejected();
    return makeError(std::move(result.error()));
  }

  auto lane = CxlLane::create(fabric_->region(), fabric_->layout(), result->laneConfig(), CxlLaneRole::Acceptor);
  if (!lane) {
    acceptorCloseHook (*result)(true);
    metrics_->rejected();
    return makeError(std::move(lane.error()));
  }
  auto socket = CxlSocket::createUnique(std::move(*lane),
                                        Address(req.requester_address),
                                        fabric_->progressEngine(),
                                        bulkTransfer_);
  if (!socket) {
    acceptorCloseHook (*result)(true);
    metrics_->rejected();
    return makeError(std::move(socket.error()));
  }
  (*socket)->setCloseHook(acceptorCloseHook(*result));
  {
    std::lock_guard lock(pendingMutex_);
    pendingConnections_.push_back(PendingConnection{*result, std::move(*socket)});
  }
  metrics_->accepted(sizeof(CxlConnectReq) + sizeof(CxlConnectRsp));
  return result;
}

void CxlConnectService::retireCanceledPending() {
  if (!fabric_ || !fabric_->running()) return;
  std::lock_guard lock(pendingMutex_);
  std::erase_if(pendingConnections_, [&](auto &pending) {
    const auto &descriptor = pending.descriptor;
    auto owner = ownerRecordSnapshot(*fabric_, descriptor.requester_owner_record_offset);
    // A previous incarnation's terminal record, an absent owner, or an
    // unstable/corrupt record is not a cancellation of this bootstrap.
    if (!owner || !*owner ||
        !matchesOwnerRecord(**owner, descriptor, descriptor.requester_endpoint_generation) ||
        !terminalOwner(*owner)) return false;
    // The socket has never reached an I/O worker. Closing it retires the
    // acceptor owner and releases the allocator lease under the same binding.
    pending.socket->close();
    return true;
  });
}

Result<CxlActivateRsp> CxlConnectService::activateForTest(const CxlActivateReq &req) {
  CxlConnectRsp descriptor;
  std::unique_ptr<CxlSocket> socket;
  {
    std::lock_guard lock(pendingMutex_);
    auto pending = std::find_if(pendingConnections_.begin(), pendingConnections_.end(), [&](const auto &candidate) {
      return matchesActivation(candidate.descriptor, req);
    });
    if (pending == pendingConnections_.end()) {
      metrics_->rejected();
      return makeError(RPCCode::kDataPlaneHandshakeFailed, "CXL activation has no exact pending bootstrap");
    }
    auto requester =
        fabric_->endpointState().snapshot(EndpointId{req.requester_endpoint}, req.requester_endpoint_generation);
    auto owner = validateOwnerRecord(*fabric_,
                                     pending->descriptor.requester_owner_record_offset,
                                     pending->descriptor,
                                     pending->descriptor.requester_endpoint_generation);
    if (!requester || loadLe32(&requester->lifecycle) != static_cast<uint32_t>(CxlEndpointLifecycle::Ready) || !owner) {
      metrics_->rejected();
      return makeError(RPCCode::kDataPlaneHandshakeFailed, "CXL requester is not ready for lane activation");
    }
    descriptor = pending->descriptor;
    socket = std::move(pending->socket);
    pendingConnections_.erase(pending);
  }

  socket->setPeerCheck(peerCheck(fabric_, descriptor, false));
  auto accepted =
      accept_ ? accept_(std::move(socket), static_cast<ServicePlane>(descriptor.service_plane)) : Result<Void>{Void{}};
  if (!accepted) {
    acceptorCloseHook(descriptor)(true);
    metrics_->rejected();
    return makeError(std::move(accepted.error()));
  }
  metrics_->accepted(sizeof(CxlActivateReq) + sizeof(CxlActivateRsp));
  CxlActivateRsp rsp;
  rsp.activated = true;
  return rsp;
}

Result<std::shared_ptr<CxlSocket>> CxlConnectService::finishRequester(std::shared_ptr<CxlFabric> fabric,
                                                                      const CxlConnectReq &req,
                                                                      const CxlConnectRsp &rsp,
                                                                      Address peer) {
  auto unique = finishRequesterUnique(std::move(fabric), req, rsp, peer);
  if (!unique) {
    return makeError(std::move(unique.error()));
  }
  return std::shared_ptr<CxlSocket>(std::move(*unique));
}

Result<std::unique_ptr<CxlSocket>> CxlConnectService::finishRequesterUnique(std::shared_ptr<CxlFabric> fabric,
                                                                            const CxlConnectReq &req,
                                                                            const CxlConnectRsp &rsp,
                                                                            Address peer) {
  if (!fabric || !peer.isCXL() || rsp.session_generation != req.session_generation ||
      rsp.session_generation != fabric->layout().sessionGeneration() ||
      rsp.requester_endpoint != req.requester_endpoint ||
      rsp.requester_endpoint_generation != req.requester_endpoint_generation ||
      rsp.target_endpoint != req.target_endpoint || rsp.service_plane != req.service_plane ||
      rsp.queue_depth != req.queue_depth || rsp.cell_bytes != req.cell_bytes ||
      rsp.connection_nonce != req.connection_nonce || rsp.lane_id == 0 || rsp.lane_generation == 0) {
    return makeError(RPCCode::kDataPlaneHandshakeFailed, "CXL bootstrap response does not match the request");
  }
  if (fabric->config().endpoint.value != req.requester_endpoint ||
      fabric->config().endpointGeneration != req.requester_endpoint_generation) {
    return makeError(RPCCode::kDataPlaneHandshakeFailed, "wrong local CXL requester endpoint incarnation");
  }
  RETURN_ON_ERROR(fabric->endpointState().snapshot(EndpointId{rsp.target_endpoint}, rsp.target_endpoint_generation));
  RETURN_ON_ERROR(validateOwnerRecord(*fabric, rsp.acceptor_owner_record_offset, rsp, rsp.target_endpoint_generation));
  auto previousRequester = ownerRecordSnapshot(*fabric, rsp.requester_owner_record_offset);
  if (!previousRequester) {
    return makeError(std::move(previousRequester.error()));
  }
  if (!terminalOwner(*previousRequester)) {
    return makeError(RPCCode::kDataPlaneHandshakeFailed, "CXL requester owner slot is still active");
  }
  auto requesterSequence = nextOwnerSequence(*previousRequester);
  if (!requesterSequence) {
    return makeError(std::move(requesterSequence.error()));
  }
  RETURN_ON_ERROR(publishOwnerRecord(*fabric,
                                     rsp.requester_owner_record_offset,
                                     rsp,
                                     rsp.requester_endpoint_generation,
                                     CxlLaneLifecycle::Ready,
                                     *requesterSequence));

  auto lane = CxlLane::create(fabric->region(), fabric->layout(), rsp.laneConfig(), CxlLaneRole::Requester);
  if (!lane) {
    (void)faultRequester(fabric, rsp);
    return makeError(std::move(lane.error()));
  }
  auto socket = CxlSocket::createUnique(std::move(*lane),
                                        peer,
                                        fabric->progressEngine(),
                                        std::make_shared<CxlBulkTransfer>(fabric));
  if (!socket) {
    (void)faultRequester(fabric, rsp);
    return makeError(std::move(socket.error()));
  }
  auto ownerFabric = fabric;
  (*socket)->setCloseHook([ownerFabric = std::move(ownerFabric), rsp](bool faulted) noexcept {
    (void)transitionOwnerRecord(*ownerFabric,
                                rsp.requester_owner_record_offset,
                                rsp,
                                rsp.requester_endpoint_generation,
                                faulted ? CxlLaneLifecycle::Faulted : CxlLaneLifecycle::Retired);
  });
  (*socket)->setPeerCheck(peerCheck(fabric, rsp, true));
  return socket;
}

Result<Void> CxlConnectService::faultRequester(const std::shared_ptr<CxlFabric> &fabric, const CxlConnectRsp &rsp) {
  if (!fabric) {
    return makeError(StatusCode::kInvalidArg, "cannot fault a CXL requester without a fabric");
  }
  return transitionOwnerRecord(*fabric,
                               rsp.requester_owner_record_offset,
                               rsp,
                               rsp.requester_endpoint_generation,
                               CxlLaneLifecycle::Faulted);
}

Result<CxlConnectRsp> CxlConnectService::validateAndAllocate(const CxlConnectReq &req) {
  if (fabric_) {
    RETURN_ON_ERROR(fabric_->check());
  }
  if (!fabric_ || !fabric_->running() || !fabric_->acceptingSubmissions()) {
    return makeError(RPCCode::kDataPlaneNotInitialized, "CXL fabric is unavailable");
  }
  if (req.abi_major != kCxlAbiVersion || req.abi_minor != kCxlAbiMinorVersion ||
      req.session_generation != fabric_->layout().sessionGeneration()) {
    return makeError(RPCCode::kDataPlaneHandshakeFailed, "CXL ABI or session generation mismatch");
  }
  if (req.requester_endpoint == 0 || req.requester_endpoint == req.target_endpoint ||
      req.target_endpoint != fabric_->config().endpoint.value || req.requester_endpoint_generation == 0 ||
      req.service_plane > static_cast<uint8_t>(ServicePlane::Data) || req.queue_depth == 0 || req.cell_bytes == 0 ||
      allZero(req.connection_nonce) || !Address(req.requester_address).isCXL()) {
    return makeError(RPCCode::kDataPlaneHandshakeFailed, "invalid CXL bootstrap identity, plane, nonce or geometry");
  }
  auto requester =
      fabric_->endpointState().snapshot(EndpointId{req.requester_endpoint}, req.requester_endpoint_generation);
  if (!requester || loadLe32(&requester->lifecycle) != static_cast<uint32_t>(CxlEndpointLifecycle::Ready) ||
      (req.capability_bits & ~loadLe64(&requester->capabilityBits)) != 0) {
    return makeError(RPCCode::kDataPlaneHandshakeFailed, "CXL requester endpoint is not READY or lacks capabilities");
  }
  const uint64_t negotiatedCapabilities = req.capability_bits & fabric_->config().capabilityBits;

  std::lock_guard lock(leases_->mutex);
  const ConnectionKey key{req.requester_endpoint, req.requester_endpoint_generation, req.connection_nonce};
  if (std::find(leases_->activeConnections.begin(), leases_->activeConnections.end(), key) !=
      leases_->activeConnections.end()) {
    return makeError(StatusCode::kQueueConflict, "duplicate active CXL bootstrap tuple");
  }
  auto allocated = allocateLocked(req, negotiatedCapabilities);
  if (allocated) {
    leases_->activeConnections.push_back(key);
  }
  return allocated;
}

Result<CxlConnectRsp> CxlConnectService::allocateLocked(const CxlConnectReq &req, uint64_t negotiatedCapabilities) {
  const uint32_t endpointCount = loadLe32(&fabric_->layout().superblock().header.endpointCount);
  if (endpointCount == 0 || req.target_endpoint == 0 || req.target_endpoint > endpointCount) {
    return makeError(StatusCode::kInvalidConfig, "CXL target endpoint cannot own a lane partition");
  }

  const uint32_t laneCount = static_cast<uint32_t>(leases_->activeLanes.size());
  const auto &ranges = fabric_->layout().ranges();
  const auto &laneRange = ranges[static_cast<size_t>(CxlRangeKind::LaneDirectory) - 1U];
  const auto &cursorRange = ranges[static_cast<size_t>(CxlRangeKind::CursorPages) - 1U];
  const auto &frameRange = ranges[static_cast<size_t>(CxlRangeKind::FrameRings) - 1U];
  const auto &payloadRange = ranges[static_cast<size_t>(CxlRangeKind::RpcPayloadCells) - 1U];
  const uint64_t frameDirectionBytes = static_cast<uint64_t>(req.queue_depth) * sizeof(CxlFrameEntry);
  const uint64_t payloadDirectionBytes = static_cast<uint64_t>(req.queue_depth) * req.cell_bytes;
  auto laneDirectoryStride = laneStride(laneRange, laneCount, 2U * sizeof(CxlLaneOwnerRecord), kCxlCacheLineBytes);
  auto cursorStride = laneStride(cursorRange, laneCount, 4U * kCursorPageBytes, kCursorPageBytes);
  auto frameStride = laneStride(frameRange, laneCount, 2U * frameDirectionBytes, kCxlCacheLineBytes);
  auto payloadStride = laneStride(payloadRange, laneCount, 2U * payloadDirectionBytes, kCxlCacheLineBytes);
  if (!laneDirectoryStride || !cursorStride || !frameStride || !payloadStride) {
    return makeError(StatusCode::kInvalidConfig, "manifest ranges cannot satisfy requested CXL lane geometry");
  }

  // Each acceptor is the sole allocator for its deterministic lane subset.
  // A slot is reusable only after both owner records are terminal. A crashed
  // peer therefore quarantines its slot until the fabric session is rebuilt;
  // this deliberately prefers isolation over guessing that stale writes ended.
  uint32_t laneIndex = laneCount;
  uint64_t laneGeneration = 1;
  std::optional<CxlLaneOwnerRecord> requesterPrevious;
  std::optional<CxlLaneOwnerRecord> acceptorPrevious;
  for (uint32_t candidate = req.target_endpoint - 1U; candidate < laneCount; candidate += endpointCount) {
    if (leases_->activeLanes[candidate]) {
      continue;
    }
    const uint64_t candidateBase = laneRange.offset + candidate * *laneDirectoryStride;
    auto requesterRecord = ownerRecordSnapshot(*fabric_, candidateBase);
    auto acceptorRecord = ownerRecordSnapshot(*fabric_, candidateBase + sizeof(CxlLaneOwnerRecord));
    if (!requesterRecord || !acceptorRecord) {
      return makeError(StatusCode::kDataCorruption, "cannot inspect prior CXL lane ownership");
    }
    if (!terminalOwner(*requesterRecord) || !terminalOwner(*acceptorRecord)) {
      continue;
    }
    uint64_t priorGeneration = 0;
    for (const auto *record : {&*requesterRecord, &*acceptorRecord}) {
      if (!*record) {
        continue;
      }
      if (loadLe64(&(**record).sessionGeneration) != req.session_generation) {
        return makeError(StatusCode::kDataCorruption, "prior CXL lane belongs to another fabric session");
      }
      priorGeneration = std::max(priorGeneration, loadLe64(&(**record).laneGeneration));
    }
    if (priorGeneration == std::numeric_limits<uint64_t>::max()) {
      return makeError(RPCCode::kStaleGeneration, "CXL lane generation is exhausted");
    }
    laneIndex = candidate;
    laneGeneration = priorGeneration + 1U;
    requesterPrevious = std::move(*requesterRecord);
    acceptorPrevious = std::move(*acceptorRecord);
    break;
  }
  if (laneIndex == laneCount) {
    return makeError(StatusCode::kQueueFull, "no reusable CXL lane slots");
  }

  const uint64_t laneBase = laneRange.offset + laneIndex * *laneDirectoryStride;
  const uint64_t cursorBase = cursorRange.offset + laneIndex * *cursorStride;
  const uint64_t frameBase = frameRange.offset + laneIndex * *frameStride;
  const uint64_t payloadBase = payloadRange.offset + laneIndex * *payloadStride;
  CxlConnectRsp rsp;
  rsp.session_generation = req.session_generation;
  rsp.lane_id = laneIndex + 1U;
  rsp.lane_generation = laneGeneration;
  rsp.requester_endpoint = req.requester_endpoint;
  rsp.requester_endpoint_generation = req.requester_endpoint_generation;
  rsp.target_endpoint = req.target_endpoint;
  rsp.target_endpoint_generation = fabric_->config().endpointGeneration;
  rsp.service_plane = req.service_plane;
  rsp.queue_depth = req.queue_depth;
  rsp.cell_bytes = req.cell_bytes;
  rsp.capability_bits = negotiatedCapabilities;
  rsp.connection_nonce = req.connection_nonce;
  rsp.requester_owner_record_offset = laneBase;
  rsp.acceptor_owner_record_offset = laneBase + sizeof(CxlLaneOwnerRecord);
  rsp.submission = CxlConnectDirection{
      .producer_cursor_offset = cursorBase,
      .consumer_cursor_offset = cursorBase + kCursorPageBytes,
      .delivered_record_offset = cursorBase + kCursorPageBytes + kCxlCacheLineBytes,
      .frame_ring_offset = frameBase,
      .payload_cells_offset = payloadBase,
  };
  rsp.completion = CxlConnectDirection{
      .producer_cursor_offset = cursorBase + 2U * kCursorPageBytes,
      .consumer_cursor_offset = cursorBase + 3U * kCursorPageBytes,
      .delivered_record_offset = cursorBase + 3U * kCursorPageBytes + kCxlCacheLineBytes,
      .frame_ring_offset = frameBase + frameDirectionBytes,
      .payload_cells_offset = payloadBase + payloadDirectionBytes,
  };

  auto cursorBytes = fabric_->region().checkedRange(cursorBase, *cursorStride, kCursorPageBytes);
  auto frameBytes = fabric_->region().checkedRange(frameBase, *frameStride, kCxlCacheLineBytes);
  auto payloadBytes = fabric_->region().checkedRange(payloadBase, *payloadStride, kCxlCacheLineBytes);
  if (!cursorBytes || !frameBytes || !payloadBytes) {
    return makeError(StatusCode::kInvalidConfig, "CXL lane storage is outside the mapped region");
  }
  std::fill(cursorBytes->begin(), cursorBytes->end(), std::byte{});
  // Frames and payload cells are unpublished until the producer overwrites
  // the exact frame/payload length and release-publishes its cursor. Readers
  // validate sequence, generation, length and CRC before exposing any bytes.
  // Clearing those ranges here needlessly writes 1 MiB per q8/64 KiB lane
  // while holding the allocator lock, including on every reconnect.
  auto acceptorSequence = nextOwnerSequence(acceptorPrevious);
  if (!acceptorSequence) {
    return makeError(std::move(acceptorSequence.error()));
  }
  RETURN_ON_ERROR(publishOwnerRecord(*fabric_,
                                     rsp.acceptor_owner_record_offset,
                                     rsp,
                                     rsp.target_endpoint_generation,
                                     CxlLaneLifecycle::Ready,
                                     *acceptorSequence));
  leases_->activeLanes[laneIndex] = true;
  return rsp;
}

}  // namespace hf3fs::net::cxl
