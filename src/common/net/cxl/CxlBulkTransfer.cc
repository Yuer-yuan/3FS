#include "common/net/cxl/CxlBulkTransfer.h"

#include <atomic>
#include <cstring>
#include <limits>
#include <folly/executors/GlobalExecutor.h>

#include "common/net/cxl/CxlBuffer.h"
#include "common/net/TransportEvidence.h"

namespace hf3fs::net::cxl {
namespace {

Result<uint64_t> aggregateLength(std::span<SharedBuffer> buffers) {
  uint64_t total = 0;
  for (const auto &buffer : buffers) {
    if (!buffer || buffer.size() > std::numeric_limits<uint64_t>::max() - total) {
      return makeError(StatusCode::kInvalidArg, "invalid or overflowing local bulk buffer list");
    }
    total += buffer.size();
  }
  return total;
}

}  // namespace

CoTryTask<void> CxlBulkTransfer::pull(const RemoteBufferHandle &remote, std::span<SharedBuffer> local) {
  // Simulated CXL memory accesses can block for seconds. Keep the RPC executor
  // available for completions and retirement while the independent worker copies.
  // A cancellation must not complete this await before the copy releases its
  // buffers: the caller still owns the remote publication/lease until we return.
  co_return co_await folly::coro::co_withCancellation(
      folly::CancellationToken{},
      copy(fabric_, remote, {local.begin(), local.end()}, Direction::Pull)
          .scheduleOn(copyExecutor_ ? copyExecutor_ : folly::getGlobalCPUExecutor()));
}

CoTryTask<void> CxlBulkTransfer::push(const RemoteBufferHandle &remote, std::span<SharedBuffer> local) {
  co_return co_await folly::coro::co_withCancellation(
      folly::CancellationToken{},
      copy(fabric_, remote, {local.begin(), local.end()}, Direction::Push)
          .scheduleOn(copyExecutor_ ? copyExecutor_ : folly::getGlobalCPUExecutor()));
}

CoTryTask<void> CxlBulkTransfer::copy(std::shared_ptr<CxlFabric> fabric,
                                   RemoteBufferHandle remote,
                                   std::vector<SharedBuffer> local,
                                   Direction direction) {
  // Own the mapping and local buffers across dispatch. Validate after dispatch
  // so a queued operation cannot use an identity that retired while it waited.
  CxlBulkTransfer transfer(std::move(fabric));
  auto mapped = transfer.validateAndMap(remote, local, direction);
  if (!mapped) {
    co_return makeError(std::move(mapped.error()));
  }
  if (direction == Direction::Pull) {
    std::atomic_thread_fence(std::memory_order_acquire);
  }
  size_t remoteOffset = 0;
  for (auto &buffer : local) {
    if (direction == Direction::Pull) {
      std::memmove(buffer.data(), mapped->data() + remoteOffset, buffer.size());
    } else {
      std::memmove(mapped->data() + remoteOffset, buffer.data(), buffer.size());
    }
    remoteOffset += buffer.size();
  }
  if (direction == Direction::Push) {
    std::atomic_thread_fence(std::memory_order_release);
  }
  TransportEvidence::process().add(
      direction == Direction::Pull ? TransportEvidence::CxlBulkReadBytes : TransportEvidence::CxlBulkWriteBytes,
      remoteOffset);
  co_return Void{};
}

Result<std::span<std::byte>> CxlBulkTransfer::validateAndMap(const RemoteBufferHandle &remote,
                                                             std::span<SharedBuffer> local,
                                                             Direction direction) const {
  RETURN_ON_ERROR(validateRemoteBufferHandle(remote));
  if (!fabric_ || !fabric_->mapped() || !fabric_->running()) {
    return makeError(RPCCode::kDataPlaneNotInitialized, "CXL bulk transfer requires a running fabric");
  }
  if (remote.transportKind != static_cast<uint8_t>(TransportKind::CXL) || remote.ownerEndpoint == 0 ||
      remote.arenaId != remote.ownerEndpoint) {
    return makeError(StatusCode::kInvalidFormat, "remote buffer is not a CXL owner-arena handle");
  }
  const uint8_t requiredPermission =
      direction == Direction::Pull ? remoteAccessBits(RemoteAccess::Read) : remoteAccessBits(RemoteAccess::Write);
  if ((remote.permissions & requiredPermission) == 0) {
    return makeError(RPCCode::kRemoteBufferAccessDenied, "remote buffer handle denies the requested CXL access");
  }
  auto totalLength = aggregateLength(local);
  if (!totalLength) {
    return makeError(std::move(totalLength.error()));
  }
  if (*totalLength != remote.length) {
    return makeError(StatusCode::kInvalidArg, "local bulk buffers do not exactly match the remote range length");
  }
  if (remote.sessionGeneration != fabric_->layout().sessionGeneration()) {
    return makeError(RPCCode::kStaleGeneration, "remote buffer belongs to another CXL session");
  }

  auto endpoint = fabric_->endpointState().snapshot(EndpointId{remote.ownerEndpoint}, remote.ownerGeneration);
  if (!endpoint) {
    return makeError(std::move(endpoint.error()));
  }
  const auto endpointLifecycle = static_cast<CxlEndpointLifecycle>(loadLe32(&endpoint->lifecycle));
  if (endpointLifecycle != CxlEndpointLifecycle::Ready && endpointLifecycle != CxlEndpointLifecycle::Draining) {
    return makeError(RPCCode::kStaleGeneration, "remote CXL buffer owner is not active");
  }

  const uint32_t endpointCount = loadLe32(&fabric_->layout().superblock().header.endpointCount);
  if (remote.ownerEndpoint > endpointCount) {
    return makeError(StatusCode::kInvalidFormat, "remote CXL buffer owner is outside the manifest");
  }
  auto participant = fabric_->participantPosition(EndpointId{remote.ownerEndpoint});
  if (!participant) {
    return makeError(std::move(participant.error()));
  }
  auto directoryResult = fabric_->layout().range(CxlRangeKind::AllocationDirectory, alignof(CxlAllocationRecord));
  if (!directoryResult) {
    return makeError(std::move(directoryResult.error()));
  }
  auto directory = *directoryResult;
  const uint64_t totalSlots = directory.size() / sizeof(CxlAllocationRecord);
  const uint64_t slotsPerEndpoint = totalSlots / participant->count;
  if (slotsPerEndpoint == 0 || remote.allocationSlot >= slotsPerEndpoint) {
    return makeError(StatusCode::kInvalidFormat, "remote CXL allocation slot is outside its owner partition");
  }
  const uint64_t globalSlot = static_cast<uint64_t>(participant->index) * slotsPerEndpoint + remote.allocationSlot;
  const uint64_t recordOffset = globalSlot * sizeof(CxlAllocationRecord);
  if (recordOffset > directory.size() || sizeof(CxlAllocationRecord) > directory.size() - recordOffset) {
    return makeError(StatusCode::kInvalidConfig, "remote CXL allocation record is outside the directory");
  }
  auto recordBytes = directory.subspan(recordOffset, sizeof(CxlAllocationRecord));
  auto record = loadCxlOwnerRecord<CxlAllocationRecord>(recordBytes);
  if (!record) {
    // Another export can republish an allocation's permissions while existing
    // leases still pin it. Copy nothing until a stable record is validated.
    return makeError(RPCCode::kTimeout, "remote CXL allocation publication is not yet stable");
  }

  const uint32_t stateAndPermissions = loadLe32(&record->stateAndPermissions);
  const uint32_t requiredRecordPermission = direction == Direction::Pull ? kCxlAllocationRead : kCxlAllocationWrite;
  if (loadLe64(&record->recordSequence) == 0 || (loadLe64(&record->recordSequence) & 1U) != 0 ||
      loadLe32(&record->recordCrc32c) != cxlAllocationRecordChecksum(*record) ||
      (stateAndPermissions & ~kCxlAllocationKnownBits) != 0) {
    return makeError(StatusCode::kDataCorruption, "remote CXL allocation record failed sequence, CRC or flags");
  }
  if (loadLe64(&record->sessionGeneration) != remote.sessionGeneration ||
      loadLe64(&record->ownerGeneration) != remote.ownerGeneration ||
      loadLe64(&record->allocationGeneration) != remote.allocationGeneration ||
      loadLe32(&record->ownerEndpoint) != remote.ownerEndpoint || loadLe32(&record->arenaId) != remote.arenaId ||
      (stateAndPermissions & kCxlAllocationStateMask) != static_cast<uint32_t>(CxlAllocationState::Exported)) {
    return makeError(RPCCode::kStaleGeneration, "remote CXL allocation identity, generation or state is stale");
  }
  if ((stateAndPermissions & requiredRecordPermission) == 0) {
    return makeError(RPCCode::kRemoteBufferAccessDenied, "remote CXL allocation record denies access");
  }

  const uint64_t allocationBase = loadLe64(&record->allocationBaseOffset);
  const uint64_t allocationLength = loadLe64(&record->allocationLength);
  if (allocationLength == 0 || remote.offset < allocationBase || remote.length > allocationLength ||
      remote.offset - allocationBase > allocationLength - remote.length) {
    return makeError(StatusCode::kInvalidFormat, "remote CXL buffer range escapes its published allocation");
  }
  return fabric_->region().checkedRange(remote.offset, remote.length);
}

}  // namespace hf3fs::net::cxl
