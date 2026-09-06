#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <span>
#include <vector>

#include "common/net/Buffer.h"
#include "common/net/cxl/CxlFabric.h"

namespace hf3fs::net::cxl {

uint32_t cxlAllocationRecordChecksum(const CxlAllocationRecord &record);

class CxlBufferArena : public std::enable_shared_from_this<CxlBufferArena> {
 public:
  static Result<std::shared_ptr<CxlBufferArena>> create(std::shared_ptr<CxlFabric> fabric);

  Result<SharedBuffer> tryAllocate(size_t length);
  Result<RemoteExport> exportReadOnlyCopy(std::span<const uint8_t> bytes);
  Result<bool> isAllocated(const RemoteBufferHandle &handle) const;

  uint32_t arenaId() const noexcept { return arenaId_; }
  uint64_t capacity() const noexcept { return arenaBytes_.size(); }

 private:
  struct SlotState {
    uint64_t recordSequence{};
    uint64_t allocationGeneration{};
    uint64_t baseOffset{};
    uint64_t requestedLength{};
    uint64_t reservedLength{};
    uint32_t permissions{};
    bool inUse{};
    bool exported{};
  };

  struct FreeRange {
    uint64_t offset{};
    uint64_t length{};
  };

  struct Allocation {
    std::shared_ptr<CxlBufferArena> arena;
    uint32_t slot{};
    uint64_t generation{};
    uint64_t baseOffset{};
    uint64_t requestedLength{};
    uint64_t reservedLength{};

    ~Allocation();
  };

  CxlBufferArena(std::shared_ptr<CxlFabric> fabric,
                 std::span<std::byte> directoryBytes,
                 std::span<std::byte> arenaBytes,
                 uint64_t arenaBaseOffset,
                 uint32_t firstDirectorySlot,
                 uint32_t slotCount,
                 uint32_t arenaId);

  Result<Void> initializeSlots();
  Result<RemoteBufferHandle> exportAllocation(const std::shared_ptr<Allocation> &allocation,
                                              RemoteAccess access,
                                              uint64_t offset,
                                              uint64_t length);
  Result<Void> publishSlotLocked(uint32_t slot, CxlAllocationState state);
  Result<std::span<std::byte>> slotBytes(uint32_t slot) const;
  void release(const Allocation &allocation) noexcept;
  void insertFreeRangeLocked(FreeRange range);

  std::shared_ptr<CxlFabric> fabric_;
  std::span<std::byte> directoryBytes_;
  std::span<std::byte> arenaBytes_;
  uint64_t arenaBaseOffset_{};
  uint32_t firstDirectorySlot_{};
  uint32_t slotCount_{};
  uint32_t arenaId_{};
  uint64_t sessionGeneration_{};
  uint64_t ownerGeneration_{};
  mutable std::mutex mutex_;
  std::vector<SlotState> slots_;
  std::vector<FreeRange> freeRanges_;
};

}  // namespace hf3fs::net::cxl
