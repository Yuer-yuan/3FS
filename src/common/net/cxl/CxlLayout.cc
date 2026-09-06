#include "common/net/cxl/CxlLayout.h"

#include <algorithm>
#include <atomic>
#include <cstring>
#include <fmt/format.h>
#include <limits>
#include <string_view>
#include <thread>
#include <utility>

namespace hf3fs::net::cxl {
namespace {

struct ValidatedSuperblock {
  CxlSuperblock superblock;
  std::array<CxlRange, kCxlRangeCount> ranges;
};

bool allZero(std::span<const std::byte> bytes) {
  return std::all_of(bytes.begin(), bytes.end(), [](std::byte byte) { return byte == std::byte{0}; });
}

Result<uint64_t> checkedEnd(uint64_t offset, uint64_t length, std::string_view label) {
  if (length > std::numeric_limits<uint64_t>::max() - offset) {
    return makeError(StatusCode::kInvalidArg, fmt::format("{} range overflows uint64_t", label));
  }
  return offset + length;
}

bool contains(const CxlRange &outer, uint64_t offset, uint64_t length) {
  if (length > std::numeric_limits<uint64_t>::max() - offset ||
      outer.length > std::numeric_limits<uint64_t>::max() - outer.offset) {
    return false;
  }
  return offset >= outer.offset && offset + length <= outer.offset + outer.length;
}

Result<ValidatedSuperblock> validateSuperblock(const CxlSuperblock &snapshot,
                                               uint64_t mappedBytes,
                                               const LayoutExpectation &expectation) {
  const auto &header = snapshot.header;
  if (header.magic != kCxlMagic) {
    return makeError(StatusCode::kInvalidFormat, "invalid CXL superblock magic");
  }
  if (loadLe16(&header.abiMajor) != kCxlAbiVersion || loadLe16(&header.abiMinor) != kCxlAbiMinorVersion) {
    return makeError(StatusCode::kInvalidFormat, "unsupported CXL ABI version");
  }
  if (loadLe32(&header.superblockBytes) != kCxlSuperblockBytes || loadLe32(&header.endianMarker) != kCxlEndianMarker ||
      loadLe32(&header.cacheLineBytes) != kCxlCacheLineBytes || loadLe32(&header.rangeCount) != kCxlRangeCount) {
    return makeError(StatusCode::kInvalidFormat, "invalid CXL superblock geometry");
  }
  if (loadLe32(&header.reserved0) != 0 || !allZero(header.reserved1) || !allZero(snapshot.reserved)) {
    return makeError(StatusCode::kInvalidFormat, "nonzero reserved CXL superblock bytes");
  }

  const uint64_t sessionGeneration = loadLe64(&header.sessionGeneration);
  const uint64_t totalRegionBytes = loadLe64(&header.totalRegionBytes);
  const uint32_t endpointCount = loadLe32(&header.endpointCount);
  const uint32_t laneCount = loadLe32(&header.laneCount);
  if (sessionGeneration == 0 || endpointCount == 0 || laneCount == 0 || totalRegionBytes < kCxlSuperblockBytes ||
      totalRegionBytes > mappedBytes) {
    return makeError(StatusCode::kInvalidArg, "invalid CXL generation, count or total size");
  }
  if (sessionGeneration != expectation.sessionGeneration || header.manifestSha256 != expectation.manifestSha256) {
    return makeError(RPCCode::kStaleGeneration, "CXL session generation or manifest digest mismatch");
  }
  if ((expectation.totalRegionBytes && *expectation.totalRegionBytes != totalRegionBytes) ||
      (expectation.endpointCount && *expectation.endpointCount != endpointCount) ||
      (expectation.laneCount && *expectation.laneCount != laneCount)) {
    return makeError(StatusCode::kInvalidArg, "CXL layout expectation mismatch");
  }

  const uint32_t storedCrc = loadLe32(&header.superblockCrc32c);
  const uint32_t calculatedCrc =
      cxlCrc32cWithZeroedU32(snapshot,
                             offsetof(CxlSuperblock, header) + offsetof(CxlSuperblockHeader, superblockCrc32c));
  if (storedCrc == 0 || storedCrc != calculatedCrc) {
    return makeError(StatusCode::kDataCorruption, "CXL superblock CRC32C mismatch");
  }

  std::array<CxlRange, kCxlRangeCount> ranges;
  uint64_t previousEnd = kCxlSuperblockBytes;
  for (size_t index = 0; index < ranges.size(); ++index) {
    const auto &encoded = snapshot.ranges[index];
    const auto expectedKind = static_cast<uint16_t>(index + 1U);
    const auto kind = loadLe16(&encoded.kind);
    const uint64_t offset = loadLe64(&encoded.offset);
    const uint64_t length = loadLe64(&encoded.length);
    if (kind != expectedKind || loadLe16(&encoded.flags) != 0 || loadLe32(&encoded.reserved) != 0) {
      return makeError(StatusCode::kInvalidFormat, "invalid CXL range kind, flags or reserved bytes");
    }
    if (length == 0 || offset % kCxlCacheLineBytes != 0 || length % kCxlCacheLineBytes != 0) {
      return makeError(StatusCode::kInvalidArg, "CXL ranges must be nonempty and cache-line aligned");
    }
    auto end = checkedEnd(offset, length, "CXL layout");
    if (!end || offset < previousEnd || *end > totalRegionBytes) {
      return makeError(StatusCode::kInvalidArg, "CXL ranges overlap or exceed the region");
    }
    ranges[index] = CxlRange{static_cast<CxlRangeKind>(kind), offset, length};
    previousEnd = *end;
  }

  if (ranges[0].length < static_cast<uint64_t>(endpointCount) * sizeof(CxlEndpointRecord)) {
    return makeError(StatusCode::kInvalidArg, "CXL endpoint directory is too small");
  }

  const uint64_t lifecycleOffset = loadLe64(&header.lifecycleRecordOffset);
  if (lifecycleOffset % kCxlCacheLineBytes != 0 ||
      !contains(ranges[static_cast<size_t>(CxlRangeKind::EvidenceCounters) - 1U],
                lifecycleOffset,
                sizeof(CxlFabricLifecycleRecord))) {
    return makeError(StatusCode::kInvalidArg, "CXL lifecycle record is outside the evidence range");
  }
  return ValidatedSuperblock{snapshot, ranges};
}

uint32_t lifecycleCrc(const CxlFabricLifecycleRecord &record) {
  return cxlCrc32cWithZeroedU32(record, offsetof(CxlFabricLifecycleRecord, recordCrc32c));
}

Result<CxlFabricLifecycleRecord> loadStableLifecycle(std::span<std::byte> bytes) {
  if (bytes.size() != sizeof(CxlFabricLifecycleRecord) ||
      reinterpret_cast<uintptr_t>(bytes.data()) % alignof(CxlFabricLifecycleRecord) != 0) {
    return makeError(StatusCode::kInvalidArg, "unaligned CXL lifecycle record");
  }

  auto *sharedWords = reinterpret_cast<uint64_t *>(bytes.data());
  CxlFabricLifecycleRecord snapshot{};
  auto *snapshotWords = reinterpret_cast<uint64_t *>(&snapshot);
  for (unsigned attempt = 0; attempt < 16; ++attempt) {
    const uint64_t before = std::atomic_ref<uint64_t>(sharedWords[0]).load(std::memory_order_acquire);
    if (before == 0 || (before & 1U) != 0) {
      continue;
    }
    for (size_t word = 1; word < sizeof(snapshot) / sizeof(uint64_t); ++word) {
      snapshotWords[word] = std::atomic_ref<uint64_t>(sharedWords[word]).load(std::memory_order_relaxed);
    }
    std::atomic_thread_fence(std::memory_order_acquire);
    const uint64_t after = std::atomic_ref<uint64_t>(sharedWords[0]).load(std::memory_order_acquire);
    if (before == after && (after & 1U) == 0) {
      snapshotWords[0] = after;
      return snapshot;
    }
  }
  return makeError(StatusCode::kDataCorruption, "unstable CXL lifecycle record");
}

Result<Void> validateLifecycle(const CxlFabricLifecycleRecord &lifecycle,
                               const CxlSuperblock &superblock,
                               const LayoutExpectation &expectation) {
  const uint64_t sequence = loadLe64(&lifecycle.recordSequence);
  if (sequence == 0 || (sequence & 1U) != 0 ||
      loadLe64(&lifecycle.sessionGeneration) != loadLe64(&superblock.header.sessionGeneration) ||
      loadLe64(&lifecycle.authorityGeneration) == 0 ||
      loadLe32(&lifecycle.superblockCrc32c) != loadLe32(&superblock.header.superblockCrc32c) ||
      loadLe32(&lifecycle.lifecycle) != static_cast<uint32_t>(CxlFabricLifecycle::Ready) ||
      !allZero(lifecycle.reserved)) {
    return makeError(RPCCode::kDataPlaneHandshakeFailed, "CXL fabric lifecycle is not a stable READY record");
  }
  if (expectation.authorityEndpoint && loadLe32(&lifecycle.authorityEndpoint) != expectation.authorityEndpoint->value) {
    return makeError(RPCCode::kDataPlaneHandshakeFailed, "CXL fabric authority endpoint mismatch");
  }
  const uint32_t storedCrc = loadLe32(&lifecycle.recordCrc32c);
  if (storedCrc == 0 || storedCrc != lifecycleCrc(lifecycle)) {
    return makeError(StatusCode::kDataCorruption, "CXL lifecycle CRC32C mismatch");
  }
  return Void{};
}

Result<Void> publishLifecycle(std::span<std::byte> bytes, const CxlFabricLifecycleRecord &record) {
  if (bytes.size() != sizeof(record) || reinterpret_cast<uintptr_t>(bytes.data()) % alignof(decltype(record)) != 0) {
    return makeError(StatusCode::kInvalidArg, "unaligned CXL lifecycle publication");
  }
  const uint64_t finalSequence = loadLe64(&record.recordSequence);
  if (finalSequence == 0 || (finalSequence & 1U) != 0) {
    return makeError(StatusCode::kInvalidArg, "CXL lifecycle sequence must be nonzero and even");
  }

  auto *sharedWords = reinterpret_cast<uint64_t *>(bytes.data());
  const auto *recordWords = reinterpret_cast<const uint64_t *>(&record);
  std::atomic_ref<uint64_t>(sharedWords[0]).store(finalSequence - 1U, std::memory_order_relaxed);
  std::atomic_thread_fence(std::memory_order_seq_cst);
  for (size_t word = 1; word < sizeof(record) / sizeof(uint64_t); ++word) {
    std::atomic_ref<uint64_t>(sharedWords[word]).store(recordWords[word], std::memory_order_relaxed);
  }
  std::atomic_ref<uint64_t>(sharedWords[0]).store(finalSequence, std::memory_order_release);
  return Void{};
}

CxlSuperblock encodeSuperblock(const CxlLayoutManifest &manifest) {
  CxlSuperblock block{};
  block.header.magic = kCxlMagic;
  storeLe16(&block.header.abiMajor, kCxlAbiVersion);
  storeLe16(&block.header.abiMinor, kCxlAbiMinorVersion);
  storeLe32(&block.header.superblockBytes, kCxlSuperblockBytes);
  storeLe32(&block.header.endianMarker, kCxlEndianMarker);
  storeLe32(&block.header.cacheLineBytes, kCxlCacheLineBytes);
  storeLe32(&block.header.rangeCount, kCxlRangeCount);
  storeLe32(&block.header.endpointCount, manifest.endpointCount);
  storeLe32(&block.header.laneCount, manifest.laneCount);
  storeLe64(&block.header.totalRegionBytes, manifest.totalRegionBytes);
  storeLe64(&block.header.sessionGeneration, manifest.sessionGeneration);
  storeLe64(&block.header.lifecycleRecordOffset, manifest.lifecycleRecordOffset);
  block.header.manifestSha256 = manifest.manifestSha256;
  for (size_t index = 0; index < manifest.ranges.size(); ++index) {
    storeLe16(&block.ranges[index].kind, static_cast<uint16_t>(manifest.ranges[index].kind));
    storeLe64(&block.ranges[index].offset, manifest.ranges[index].offset);
    storeLe64(&block.ranges[index].length, manifest.ranges[index].length);
  }
  const uint32_t crc =
      cxlCrc32cWithZeroedU32(block, offsetof(CxlSuperblock, header) + offsetof(CxlSuperblockHeader, superblockCrc32c));
  storeLe32(&block.header.superblockCrc32c, crc);
  return block;
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

Result<CxlLayout> CxlLayout::initializeAsAuthority(CxlRegion &region,
                                                   const CxlLayoutManifest &manifest,
                                                   EndpointId authority) {
  if (authority != manifest.authorityEndpoint || authority.value == 0 || manifest.authorityGeneration == 0) {
    return makeError(StatusCode::kInvalidArg, "caller is not the manifest-declared CXL fabric authority");
  }

  auto encoded = encodeSuperblock(manifest);
  auto validated = validateSuperblock(encoded, region.size(), expectationFor(manifest));
  if (!validated) {
    return makeError(std::move(validated.error()));
  }

  auto destination = region.checkedRange(0, sizeof(CxlSuperblock), alignof(CxlSuperblock));
  if (!destination) {
    return makeError(std::move(destination.error()));
  }
  std::array<std::byte, kCxlMagic.size()> currentMagic;
  std::memcpy(currentMagic.data(), destination->data(), currentMagic.size());
  if (currentMagic == kCxlMagic) {
    return makeError(StatusCode::kInvalidArg, "CXL region already contains an initialized superblock");
  }

  auto lifecycleBytes = region.checkedRange(manifest.lifecycleRecordOffset,
                                            sizeof(CxlFabricLifecycleRecord),
                                            alignof(CxlFabricLifecycleRecord));
  if (!lifecycleBytes) {
    return makeError(std::move(lifecycleBytes.error()));
  }

  std::memcpy(destination->data(), &encoded, sizeof(encoded));
  std::atomic_thread_fence(std::memory_order_release);

  CxlFabricLifecycleRecord lifecycle{};
  storeLe64(&lifecycle.recordSequence, 2);
  storeLe64(&lifecycle.sessionGeneration, manifest.sessionGeneration);
  storeLe64(&lifecycle.authorityGeneration, manifest.authorityGeneration);
  storeLe32(&lifecycle.authorityEndpoint, authority.value);
  storeLe32(&lifecycle.superblockCrc32c, loadLe32(&encoded.header.superblockCrc32c));
  storeLe32(&lifecycle.lifecycle, static_cast<uint32_t>(CxlFabricLifecycle::Init));
  storeLe32(&lifecycle.recordCrc32c, lifecycleCrc(lifecycle));
  RETURN_ON_ERROR(publishLifecycle(*lifecycleBytes, lifecycle));

  storeLe64(&lifecycle.recordSequence, 4);
  storeLe64(&lifecycle.heartbeat, 1);
  storeLe32(&lifecycle.lifecycle, static_cast<uint32_t>(CxlFabricLifecycle::Ready));
  storeLe32(&lifecycle.recordCrc32c, lifecycleCrc(lifecycle));
  RETURN_ON_ERROR(publishLifecycle(*lifecycleBytes, lifecycle));
  return loadAndValidate(region, expectationFor(manifest));
}

Result<CxlLayout> CxlLayout::attach(CxlRegion &region,
                                    const LayoutExpectation &expectation,
                                    std::chrono::milliseconds timeout) {
  const auto deadline = std::chrono::steady_clock::now() + std::max(timeout, std::chrono::milliseconds::zero());
  do {
    auto result = loadAndValidate(region, expectation);
    if (result) {
      return result;
    }
    std::this_thread::yield();
  } while (std::chrono::steady_clock::now() < deadline);
  return makeError(RPCCode::kDataPlaneHandshakeFailed,
                   "timed out waiting for the exact CXL layout and READY lifecycle");
}

Result<CxlLayout> CxlLayout::loadAndValidate(CxlRegion &region, const LayoutExpectation &expectation) {
  auto source = region.checkedRange(0, sizeof(CxlSuperblock), alignof(CxlSuperblock));
  if (!source) {
    return makeError(std::move(source.error()));
  }
  CxlSuperblock snapshot{};
  std::memcpy(&snapshot, source->data(), sizeof(snapshot));

  auto validated = validateSuperblock(snapshot, region.size(), expectation);
  if (!validated) {
    return makeError(std::move(validated.error()));
  }

  const uint64_t lifecycleOffset = loadLe64(&validated->superblock.header.lifecycleRecordOffset);
  auto lifecycleBytes =
      region.checkedRange(lifecycleOffset, sizeof(CxlFabricLifecycleRecord), alignof(CxlFabricLifecycleRecord));
  if (!lifecycleBytes) {
    return makeError(std::move(lifecycleBytes.error()));
  }
  auto lifecycle = loadStableLifecycle(*lifecycleBytes);
  if (!lifecycle) {
    return makeError(std::move(lifecycle.error()));
  }
  RETURN_ON_ERROR(validateLifecycle(*lifecycle, validated->superblock, expectation));
  return CxlLayout(region, std::move(validated->superblock), std::move(*lifecycle), std::move(validated->ranges));
}

Result<std::span<std::byte>> CxlLayout::range(CxlRangeKind kind, size_t alignment) {
  const auto index = static_cast<size_t>(kind);
  if (index == 0 || index > ranges_.size()) {
    return makeError(StatusCode::kInvalidArg, "unknown CXL range kind");
  }
  const auto &descriptor = ranges_[index - 1U];
  return region_->checkedRange(descriptor.offset, descriptor.length, alignment);
}

Result<std::span<const std::byte>> CxlLayout::range(CxlRangeKind kind, size_t alignment) const {
  const auto index = static_cast<size_t>(kind);
  if (index == 0 || index > ranges_.size()) {
    return makeError(StatusCode::kInvalidArg, "unknown CXL range kind");
  }
  const auto &descriptor = ranges_[index - 1U];
  return std::as_const(*region_).checkedRange(descriptor.offset, descriptor.length, alignment);
}

}  // namespace hf3fs::net::cxl
