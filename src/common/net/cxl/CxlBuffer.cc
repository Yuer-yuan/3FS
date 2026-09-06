#include "common/net/cxl/CxlBuffer.h"

#include <algorithm>
#include <cstring>
#include <limits>

namespace hf3fs::net::cxl {
Result<RemoteExport> CxlBufferArena::exportReadOnlyCopy(std::span<const uint8_t> bytes) {
  auto copy = tryAllocate(bytes.size());
  if (!copy) {
    return makeError(std::move(copy.error()));
  }
  std::memcpy(copy->data(), bytes.data(), bytes.size());
  return copy->exportRemote(RemoteAccess::Read);
}

namespace {

constexpr uint64_t alignDown(uint64_t value, uint64_t alignment) { return value / alignment * alignment; }

Result<uint64_t> alignAllocationLength(size_t length) {
  if (length == 0 || length > std::numeric_limits<uint64_t>::max() - (kCxlCacheLineBytes - 1U)) {
    return makeError(StatusCode::kInvalidArg, "invalid CXL allocation length");
  }
  return (static_cast<uint64_t>(length) + kCxlCacheLineBytes - 1U) & ~(uint64_t{kCxlCacheLineBytes} - 1U);
}

bool allZero(std::span<const std::byte> bytes) {
  return std::all_of(bytes.begin(), bytes.end(), [](std::byte value) { return value == std::byte{}; });
}

Result<Void> validatePriorRecord(const CxlAllocationRecord &record,
                                 uint64_t sessionGeneration,
                                 EndpointId owner,
                                 uint32_t arenaId) {
  const auto stateAndPermissions = loadLe32(&record.stateAndPermissions);
  const auto state = stateAndPermissions & kCxlAllocationStateMask;
  if (loadLe64(&record.recordSequence) == 0 || (loadLe64(&record.recordSequence) & 1U) != 0 ||
      loadLe64(&record.sessionGeneration) != sessionGeneration || loadLe64(&record.ownerGeneration) == 0 ||
      loadLe32(&record.ownerEndpoint) != owner.value || loadLe32(&record.arenaId) != arenaId) {
    return makeError(RPCCode::kStaleGeneration, "CXL allocation record identity or generation mismatch");
  }
  if ((state != static_cast<uint32_t>(CxlAllocationState::Free) &&
       state != static_cast<uint32_t>(CxlAllocationState::Exported) &&
       state != static_cast<uint32_t>(CxlAllocationState::Retiring)) ||
      (stateAndPermissions & ~kCxlAllocationKnownBits) != 0 ||
      loadLe32(&record.recordCrc32c) != cxlAllocationRecordChecksum(record)) {
    return makeError(StatusCode::kDataCorruption, "invalid CXL allocation record state or CRC32C");
  }
  return Void{};
}

}  // namespace

uint32_t cxlAllocationRecordChecksum(const CxlAllocationRecord &record) {
  return cxlCrc32cWithZeroedU32(record, offsetof(CxlAllocationRecord, recordCrc32c));
}

CxlBufferArena::Allocation::~Allocation() {
  if (arena) {
    arena->release(*this);
  }
}

CxlBufferArena::CxlBufferArena(std::shared_ptr<CxlFabric> fabric,
                               std::span<std::byte> directoryBytes,
                               std::span<std::byte> arenaBytes,
                               uint64_t arenaBaseOffset,
                               uint32_t firstDirectorySlot,
                               uint32_t slotCount,
                               uint32_t arenaId)
    : fabric_(std::move(fabric)),
      directoryBytes_(directoryBytes),
      arenaBytes_(arenaBytes),
      arenaBaseOffset_(arenaBaseOffset),
      firstDirectorySlot_(firstDirectorySlot),
      slotCount_(slotCount),
      arenaId_(arenaId),
      sessionGeneration_(fabric_->layout().sessionGeneration()),
      ownerGeneration_(fabric_->config().endpointGeneration),
      slots_(slotCount) {
  freeRanges_.push_back(FreeRange{.offset = 0, .length = arenaBytes_.size()});
}

Result<std::shared_ptr<CxlBufferArena>> CxlBufferArena::create(std::shared_ptr<CxlFabric> fabric) {
  if (!fabric || !fabric->mapped() || !fabric->running()) {
    return makeError(RPCCode::kDataPlaneNotInitialized, "CXL buffer arena requires a running fabric");
  }
  const EndpointId endpoint = fabric->config().endpoint;
  auto participant = fabric->participantPosition(endpoint);
  if (!participant) {
    return makeError(std::move(participant.error()));
  }

  auto directoryResult = fabric->layout().range(CxlRangeKind::AllocationDirectory, alignof(CxlAllocationRecord));
  if (!directoryResult) {
    return makeError(std::move(directoryResult.error()));
  }
  auto allArenaBytesResult = fabric->layout().range(CxlRangeKind::BulkArenas, kCxlCacheLineBytes);
  if (!allArenaBytesResult) {
    return makeError(std::move(allArenaBytesResult.error()));
  }
  auto directory = *directoryResult;
  auto allArenaBytes = *allArenaBytesResult;
  const uint64_t totalSlots = directory.size() / sizeof(CxlAllocationRecord);
  const uint64_t slotsPerEndpoint = totalSlots / participant->count;
  const uint64_t arenaBytesPerEndpoint = alignDown(allArenaBytes.size() / participant->count, kCxlCacheLineBytes);
  if (slotsPerEndpoint == 0 || slotsPerEndpoint > std::numeric_limits<uint32_t>::max() || arenaBytesPerEndpoint == 0) {
    return makeError(StatusCode::kInvalidConfig, "CXL allocation directory or bulk arena is too small");
  }

  const uint64_t ownerIndex = participant->index;
  const uint64_t firstSlot = ownerIndex * slotsPerEndpoint;
  const uint64_t arenaRelativeOffset = ownerIndex * arenaBytesPerEndpoint;
  if (firstSlot > std::numeric_limits<uint32_t>::max() || arenaRelativeOffset > allArenaBytes.size() ||
      arenaBytesPerEndpoint > allArenaBytes.size() - arenaRelativeOffset) {
    return makeError(StatusCode::kInvalidConfig, "CXL owner arena partition is out of bounds");
  }

  uint64_t bulkRangeOffset = 0;
  bool foundBulkRange = false;
  for (const auto &range : fabric->layout().ranges()) {
    if (range.kind == CxlRangeKind::BulkArenas) {
      bulkRangeOffset = range.offset;
      foundBulkRange = true;
      break;
    }
  }
  if (!foundBulkRange || bulkRangeOffset > std::numeric_limits<uint64_t>::max() - arenaRelativeOffset) {
    return makeError(StatusCode::kInvalidConfig, "CXL bulk arena range is missing or overflowing");
  }

  auto arena = std::shared_ptr<CxlBufferArena>(
      new CxlBufferArena(std::move(fabric),
                         directory,
                         allArenaBytes.subspan(arenaRelativeOffset, arenaBytesPerEndpoint),
                         bulkRangeOffset + arenaRelativeOffset,
                         static_cast<uint32_t>(firstSlot),
                         static_cast<uint32_t>(slotsPerEndpoint),
                         endpoint.value));
  RETURN_ON_ERROR(arena->initializeSlots());
  return arena;
}

Result<Void> CxlBufferArena::initializeSlots() {
  std::lock_guard lock(mutex_);
  for (uint32_t slot = 0; slot < slotCount_; ++slot) {
    auto bytesResult = slotBytes(slot);
    if (!bytesResult) {
      return makeError(std::move(bytesResult.error()));
    }
    auto bytes = *bytesResult;
    if (allZero(std::span<const std::byte>(bytes.data(), bytes.size()))) {
      continue;
    }
    auto previous = loadCxlOwnerRecord<CxlAllocationRecord>(bytes);
    if (!previous) {
      return makeError(StatusCode::kDataCorruption, "unstable prior CXL allocation record");
    }
    RETURN_ON_ERROR(validatePriorRecord(*previous, sessionGeneration_, fabric_->config().endpoint, arenaId_));
    auto &state = slots_[slot];
    state.recordSequence = loadLe64(&previous->recordSequence);
    state.allocationGeneration = loadLe64(&previous->allocationGeneration);
    if (state.allocationGeneration == std::numeric_limits<uint64_t>::max()) {
      return makeError(RPCCode::kStaleGeneration, "CXL allocation generation is exhausted");
    }
    RETURN_ON_ERROR(publishSlotLocked(slot, CxlAllocationState::Free));
  }
  return Void{};
}

Result<SharedBuffer> CxlBufferArena::tryAllocate(size_t length) {
  auto reservedLengthResult = alignAllocationLength(length);
  if (!reservedLengthResult) {
    return makeError(std::move(reservedLengthResult.error()));
  }
  const uint64_t reservedLength = *reservedLengthResult;

  uint32_t selectedSlot = slotCount_;
  uint64_t selectedRange = freeRanges_.size();
  std::shared_ptr<Allocation> allocation;
  {
    std::lock_guard lock(mutex_);
    if (!fabric_->mapped() || !fabric_->acceptingSubmissions()) {
      return makeError(RPCCode::kDataPlaneNotInitialized, "CXL fabric is not accepting buffer allocations");
    }
    for (uint32_t slot = 0; slot < slotCount_; ++slot) {
      if (!slots_[slot].inUse && slots_[slot].allocationGeneration != std::numeric_limits<uint64_t>::max()) {
        selectedSlot = slot;
        break;
      }
    }
    for (size_t index = 0; index < freeRanges_.size(); ++index) {
      if (freeRanges_[index].length >= reservedLength) {
        selectedRange = index;
        break;
      }
    }
    if (selectedSlot == slotCount_ || selectedRange == freeRanges_.size()) {
      return makeError(StatusCode::kNotEnoughMemory, "CXL allocation slots or arena bytes are exhausted");
    }

    auto &range = freeRanges_[selectedRange];
    const uint64_t relativeBase = range.offset;
    range.offset += reservedLength;
    range.length -= reservedLength;
    if (range.length == 0) {
      freeRanges_.erase(freeRanges_.begin() + selectedRange);
    }

    auto &slot = slots_[selectedSlot];
    ++slot.allocationGeneration;
    slot.baseOffset = arenaBaseOffset_ + relativeBase;
    slot.requestedLength = length;
    slot.reservedLength = reservedLength;
    slot.permissions = 0;
    slot.inUse = true;
    slot.exported = false;
    allocation = std::make_shared<Allocation>();
    allocation->arena = shared_from_this();
    allocation->slot = selectedSlot;
    allocation->generation = slot.allocationGeneration;
    allocation->baseOffset = slot.baseOffset;
    allocation->requestedLength = length;
    allocation->reservedLength = reservedLength;
  }

  std::weak_ptr<Allocation> weakAllocation = allocation;
  SharedBuffer::Exporter exporter =
      [weakAllocation](RemoteAccess access, uint64_t offset, uint64_t exportLength) -> Result<RemoteBufferHandle> {
    auto owned = weakAllocation.lock();
    if (!owned || !owned->arena) {
      return makeError(RPCCode::kStaleGeneration, "CXL allocation has already been released");
    }
    return owned->arena->exportAllocation(owned, access, offset, exportLength);
  };
  const uint64_t relativeBase = allocation->baseOffset - arenaBaseOffset_;
  return SharedBuffer::fromStorage(reinterpret_cast<uint8_t *>(arenaBytes_.data() + relativeBase),
                                   length,
                                   0,
                                   std::move(allocation),
                                   std::move(exporter));
}

Result<RemoteBufferHandle> CxlBufferArena::exportAllocation(const std::shared_ptr<Allocation> &allocation,
                                                            RemoteAccess access,
                                                            uint64_t offset,
                                                            uint64_t length) {
  const uint32_t requestedPermissions = remoteAccessBits(access);
  if (requestedPermissions == 0 ||
      (requestedPermissions & ~(kCxlAllocationRead >> 8U | kCxlAllocationWrite >> 8U)) != 0 || length == 0 ||
      offset > allocation->requestedLength || length > allocation->requestedLength - offset) {
    return makeError(StatusCode::kInvalidArg, "invalid CXL remote export range or permissions");
  }

  std::lock_guard lock(mutex_);
  if (!fabric_->mapped() || !fabric_->acceptingSubmissions() || allocation->slot >= slots_.size()) {
    return makeError(RPCCode::kDataPlaneNotInitialized, "CXL fabric is not accepting remote exports");
  }
  auto &slot = slots_[allocation->slot];
  if (!slot.inUse || slot.allocationGeneration != allocation->generation || slot.baseOffset != allocation->baseOffset ||
      slot.requestedLength != allocation->requestedLength) {
    return makeError(RPCCode::kStaleGeneration, "CXL allocation was released or reused");
  }
  slot.permissions |= requestedPermissions;
  RETURN_ON_ERROR(publishSlotLocked(allocation->slot, CxlAllocationState::Exported));
  slot.exported = true;

  RemoteBufferHandle handle{};
  handle.abiVersion = kCxlAbiVersion;
  handle.transportKind = static_cast<uint8_t>(TransportKind::CXL);
  handle.permissions = static_cast<uint8_t>(requestedPermissions);
  handle.ownerEndpoint = fabric_->config().endpoint.value;
  handle.arenaId = arenaId_;
  handle.allocationSlot = allocation->slot;
  handle.sessionGeneration = sessionGeneration_;
  handle.ownerGeneration = ownerGeneration_;
  handle.allocationGeneration = allocation->generation;
  handle.offset = allocation->baseOffset + offset;
  handle.length = length;
  sealRemoteBufferHandle(handle);
  return handle;
}

Result<bool> CxlBufferArena::isAllocated(const RemoteBufferHandle &handle) const {
  RETURN_ON_ERROR(validateRemoteBufferHandle(handle));
  std::lock_guard lock(mutex_);
  if (handle.transportKind != static_cast<uint8_t>(TransportKind::CXL) ||
      handle.ownerEndpoint != fabric_->config().endpoint.value || handle.arenaId != arenaId_ ||
      handle.sessionGeneration != sessionGeneration_ || handle.ownerGeneration != ownerGeneration_ ||
      handle.allocationSlot >= slots_.size()) {
    return false;
  }
  const auto &slot = slots_[handle.allocationSlot];
  return slot.inUse && slot.exported && slot.allocationGeneration == handle.allocationGeneration &&
         handle.offset >= slot.baseOffset && handle.length <= slot.requestedLength &&
         handle.offset - slot.baseOffset <= slot.requestedLength - handle.length;
}

Result<Void> CxlBufferArena::publishSlotLocked(uint32_t slotIndex, CxlAllocationState allocationState) {
  if (slotIndex >= slots_.size()) {
    return makeError(StatusCode::kInvalidArg, "CXL allocation slot is out of bounds");
  }
  auto &slot = slots_[slotIndex];
  if (slot.recordSequence > std::numeric_limits<uint64_t>::max() - 2U) {
    return makeError(RPCCode::kStaleGeneration, "CXL allocation record sequence is exhausted");
  }
  auto bytes = slotBytes(slotIndex);
  if (!bytes) {
    return makeError(std::move(bytes.error()));
  }

  CxlAllocationRecord record{};
  storeLe64(&record.recordSequence, slot.recordSequence + 2U);
  storeLe64(&record.sessionGeneration, sessionGeneration_);
  storeLe64(&record.ownerGeneration, ownerGeneration_);
  storeLe64(&record.allocationGeneration, slot.allocationGeneration);
  storeLe64(&record.allocationBaseOffset, allocationState == CxlAllocationState::Free ? 0 : slot.baseOffset);
  storeLe64(&record.allocationLength, allocationState == CxlAllocationState::Free ? 0 : slot.requestedLength);
  storeLe32(&record.ownerEndpoint, fabric_->config().endpoint.value);
  storeLe32(&record.arenaId, arenaId_);
  uint32_t stateAndPermissions = static_cast<uint32_t>(allocationState);
  if (allocationState != CxlAllocationState::Free) {
    if ((slot.permissions & remoteAccessBits(RemoteAccess::Read)) != 0) {
      stateAndPermissions |= kCxlAllocationRead;
    }
    if ((slot.permissions & remoteAccessBits(RemoteAccess::Write)) != 0) {
      stateAndPermissions |= kCxlAllocationWrite;
    }
  }
  storeLe32(&record.stateAndPermissions, stateAndPermissions);
  storeLe32(&record.recordCrc32c, cxlAllocationRecordChecksum(record));
  if (!publishCxlOwnerRecord(*bytes, record)) {
    return makeError(StatusCode::kDataCorruption, "failed to publish CXL allocation record");
  }
  slot.recordSequence += 2U;
  return Void{};
}

Result<std::span<std::byte>> CxlBufferArena::slotBytes(uint32_t slot) const {
  if (slot >= slotCount_) {
    return makeError(StatusCode::kInvalidArg, "CXL allocation slot is outside the owner partition");
  }
  const uint64_t globalSlot = static_cast<uint64_t>(firstDirectorySlot_) + slot;
  const uint64_t offset = globalSlot * sizeof(CxlAllocationRecord);
  if (offset > directoryBytes_.size() || sizeof(CxlAllocationRecord) > directoryBytes_.size() - offset) {
    return makeError(StatusCode::kInvalidConfig, "CXL allocation record is outside the directory");
  }
  return directoryBytes_.subspan(offset, sizeof(CxlAllocationRecord));
}

void CxlBufferArena::release(const Allocation &allocation) noexcept {
  std::lock_guard lock(mutex_);
  if (allocation.slot >= slots_.size()) {
    return;
  }
  auto &slot = slots_[allocation.slot];
  if (!slot.inUse || slot.allocationGeneration != allocation.generation || slot.baseOffset != allocation.baseOffset) {
    return;
  }
  if (fabric_->mapped() && slot.exported) {
    (void)publishSlotLocked(allocation.slot, CxlAllocationState::Free);
  }
  const uint64_t relativeBase = allocation.baseOffset - arenaBaseOffset_;
  slot.baseOffset = 0;
  slot.requestedLength = 0;
  slot.reservedLength = 0;
  slot.permissions = 0;
  slot.inUse = false;
  slot.exported = false;
  insertFreeRangeLocked(FreeRange{.offset = relativeBase, .length = allocation.reservedLength});
}

void CxlBufferArena::insertFreeRangeLocked(FreeRange range) {
  auto position =
      std::lower_bound(freeRanges_.begin(),
                       freeRanges_.end(),
                       range.offset,
                       [](const FreeRange &candidate, uint64_t offset) { return candidate.offset < offset; });
  position = freeRanges_.insert(position, range);
  if (position != freeRanges_.begin()) {
    auto previous = position - 1;
    if (previous->offset + previous->length == position->offset) {
      previous->length += position->length;
      position = freeRanges_.erase(position);
      position = previous;
    }
  }
  auto next = position + 1;
  if (next != freeRanges_.end() && position->offset + position->length == next->offset) {
    position->length += next->length;
    freeRanges_.erase(next);
  }
}

}  // namespace hf3fs::net::cxl
