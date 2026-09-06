#pragma once

#include <array>
#include <atomic>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <optional>
#include <span>
#include <type_traits>

namespace hf3fs::net::cxl {

using le16 = uint16_t;
using le32 = uint32_t;
using le64 = uint64_t;

static_assert(std::endian::native == std::endian::little, "CXL ABI requires a little-endian process");
static_assert(std::atomic_ref<uint32_t>::is_always_lock_free,
              "CXL ABI requires lock-free shared 32-bit atomic loads and stores");
static_assert(std::atomic_ref<uint64_t>::is_always_lock_free,
              "CXL ABI requires lock-free shared 64-bit atomic loads and stores");

inline constexpr uint16_t kCxlAbiVersion = 1;
inline constexpr uint16_t kCxlAbiMinorVersion = 0;
inline constexpr uint32_t kCxlSuperblockBytes = 4096;
inline constexpr uint32_t kCxlCacheLineBytes = 64;
inline constexpr uint32_t kCxlEndianMarker = 0x01020304U;
inline constexpr size_t kCxlRangeCount = 8;
inline constexpr std::array<std::byte, 8> kCxlMagic{
    std::byte{'H'},
    std::byte{'F'},
    std::byte{'3'},
    std::byte{'F'},
    std::byte{'S'},
    std::byte{'C'},
    std::byte{'X'},
    std::byte{'L'},
};

struct EndpointId {
  uint32_t value{};
  bool operator==(const EndpointId &) const = default;
};

enum class CxlRangeKind : uint16_t {
  EndpointDirectory = 1,
  LaneDirectory = 2,
  CursorPages = 3,
  FrameRings = 4,
  RpcPayloadCells = 5,
  AllocationDirectory = 6,
  BulkArenas = 7,
  EvidenceCounters = 8,
};

enum class CxlFabricLifecycle : uint32_t {
  Uninitialized = 0,
  Init = 1,
  Ready = 2,
  Draining = 3,
  Retired = 4,
  Faulted = 5,
};

enum class CxlEndpointLifecycle : uint32_t {
  Free = 0,
  Starting = 1,
  Ready = 2,
  Draining = 3,
  Retired = 4,
  Faulted = 5,
};

enum class CxlLaneLifecycle : uint32_t {
  Free = 0,
  Starting = 1,
  Ready = 2,
  Draining = 3,
  Retired = 4,
  Faulted = 5,
};

enum class CxlFrameFlags : uint32_t {
  Data = 1U << 0U,
  Error = 1U << 1U,
  Retire = 1U << 2U,
};

enum class CxlAllocationState : uint32_t {
  Free = 0,
  Exported = 1,
  Retiring = 2,
};

inline constexpr uint32_t kCxlAllocationStateMask = 0x3U;
inline constexpr uint32_t kCxlAllocationRead = 1U << 8U;
inline constexpr uint32_t kCxlAllocationWrite = 1U << 9U;
inline constexpr uint32_t kCxlAllocationKnownBits = kCxlAllocationStateMask | kCxlAllocationRead | kCxlAllocationWrite;

struct CxlRangeEntry {
  le16 kind;
  le16 flags;
  le32 reserved;
  le64 offset;
  le64 length;
};
static_assert(sizeof(CxlRangeEntry) == 24);

struct CxlSuperblockHeader {
  std::array<std::byte, 8> magic;
  le16 abiMajor;
  le16 abiMinor;
  le32 superblockBytes;
  le32 endianMarker;
  le32 cacheLineBytes;
  le32 rangeCount;
  le32 endpointCount;
  le32 laneCount;
  le32 reserved0;
  le64 totalRegionBytes;
  le64 sessionGeneration;
  le64 lifecycleRecordOffset;
  std::array<std::byte, 32> manifestSha256;
  le32 superblockCrc32c;
  std::array<std::byte, 28> reserved1;
};
static_assert(sizeof(CxlSuperblockHeader) == 128);

struct alignas(4096) CxlSuperblock {
  CxlSuperblockHeader header;
  std::array<CxlRangeEntry, kCxlRangeCount> ranges;
  std::array<std::byte, 3776> reserved;
};
static_assert(sizeof(CxlSuperblock) == kCxlSuperblockBytes);
static_assert(alignof(CxlSuperblock) == kCxlSuperblockBytes);

struct alignas(64) CxlFrameEntry {
  le64 absoluteSequence;
  le64 sessionGeneration;
  le64 laneGeneration;
  le64 streamOffset;
  le64 payloadOffset;
  le32 payloadLength;
  le32 flags;
  le32 crc32c;
  le32 reserved0;
  le64 reserved1;
};
static_assert(sizeof(CxlFrameEntry) == kCxlCacheLineBytes);
static_assert(alignof(CxlFrameEntry) == kCxlCacheLineBytes);

struct alignas(64) CxlEndpointRecord {
  le64 recordSequence;
  le64 sessionGeneration;
  le32 endpointId;
  le32 lifecycle;
  le64 endpointGeneration;
  le64 capabilityBits;
  le64 heartbeat;
  le32 recordCrc32c;
  le32 reserved0;
  le64 reserved1;
};
static_assert(sizeof(CxlEndpointRecord) == kCxlCacheLineBytes);
static_assert(alignof(CxlEndpointRecord) == kCxlCacheLineBytes);

struct alignas(64) CxlLaneOwnerRecord {
  le64 recordSequence;
  le64 sessionGeneration;
  le64 laneGeneration;
  std::array<std::byte, 16> connectionNonce;
  le64 capabilityBits;
  le32 lifecycle;
  le32 crc32c;
  le64 endpointGeneration;
};
static_assert(sizeof(CxlLaneOwnerRecord) == kCxlCacheLineBytes);
static_assert(alignof(CxlLaneOwnerRecord) == kCxlCacheLineBytes);

struct alignas(64) CxlDeliveredRecord {
  le64 recordSequence;
  le64 sessionGeneration;
  le64 laneGeneration;
  le64 deliveredOffset;
  le64 deliveredOffsetComplement;
  le32 crc32c;
  le32 reserved0;
  le64 reserved1;
  le64 reserved2;
};
static_assert(sizeof(CxlDeliveredRecord) == kCxlCacheLineBytes);
static_assert(alignof(CxlDeliveredRecord) == kCxlCacheLineBytes);

struct alignas(64) CxlFabricLifecycleRecord {
  le64 recordSequence;
  le64 sessionGeneration;
  le64 authorityGeneration;
  le64 heartbeat;
  le32 authorityEndpoint;
  le32 superblockCrc32c;
  le32 lifecycle;
  le32 recordCrc32c;
  std::array<std::byte, 16> reserved;
};
static_assert(sizeof(CxlFabricLifecycleRecord) == kCxlCacheLineBytes);
static_assert(alignof(CxlFabricLifecycleRecord) == kCxlCacheLineBytes);

struct alignas(64) CxlAllocationRecord {
  le64 recordSequence;
  le64 sessionGeneration;
  le64 ownerGeneration;
  le64 allocationGeneration;
  le64 allocationBaseOffset;
  le64 allocationLength;
  le32 ownerEndpoint;
  le32 arenaId;
  le32 stateAndPermissions;
  le32 recordCrc32c;
};
static_assert(sizeof(CxlAllocationRecord) == kCxlCacheLineBytes);
static_assert(alignof(CxlAllocationRecord) == kCxlCacheLineBytes);

static_assert(std::is_trivially_copyable_v<CxlSuperblock>);
static_assert(std::is_trivially_copyable_v<CxlFabricLifecycleRecord>);

inline uint16_t loadLe16(const void *source) {
  uint16_t value;
  std::memcpy(&value, source, sizeof(value));
  return value;
}

inline uint32_t loadLe32(const void *source) {
  uint32_t value;
  std::memcpy(&value, source, sizeof(value));
  return value;
}

inline uint64_t loadLe64(const void *source) {
  uint64_t value;
  std::memcpy(&value, source, sizeof(value));
  return value;
}

inline void storeLe16(void *destination, uint16_t value) { std::memcpy(destination, &value, sizeof(value)); }
inline void storeLe32(void *destination, uint32_t value) { std::memcpy(destination, &value, sizeof(value)); }
inline void storeLe64(void *destination, uint64_t value) { std::memcpy(destination, &value, sizeof(value)); }

// Reflected Castagnoli CRC32C. A zero seed matches the standard vector
// "123456789" -> 0xe3069283 and folly::crc32c's chaining convention.
inline uint32_t cxlCrc32c(std::span<const std::byte> bytes, uint32_t seed = 0) {
  uint32_t crc = ~seed;
  for (auto byte : bytes) {
    crc ^= static_cast<uint8_t>(byte);
    for (unsigned bit = 0; bit < 8; ++bit) {
      const uint32_t mask = 0U - (crc & 1U);
      crc = (crc >> 1U) ^ (0x82F63B78U & mask);
    }
  }
  return ~crc;
}

template <typename T>
std::span<const std::byte> asBytes(const T &value) {
  static_assert(std::is_trivially_copyable_v<T>);
  return {reinterpret_cast<const std::byte *>(&value), sizeof(T)};
}

template <typename T>
uint32_t cxlCrc32cWithZeroedU32(T value, size_t checksumOffset, std::span<const std::byte> payload = {}) {
  static_assert(std::is_trivially_copyable_v<T>);
  if (checksumOffset + sizeof(uint32_t) > sizeof(T)) {
    return 0;
  }
  storeLe32(reinterpret_cast<std::byte *>(&value) + checksumOffset, 0);
  return cxlCrc32c(payload, cxlCrc32c(asBytes(value)));
}

template <typename T>
bool publishCxlOwnerRecord(std::span<std::byte> destination, const T &record) {
  static_assert(sizeof(T) == kCxlCacheLineBytes);
  static_assert(alignof(T) == kCxlCacheLineBytes);
  static_assert(std::is_trivially_copyable_v<T>);
  if (destination.size() != sizeof(T) || reinterpret_cast<uintptr_t>(destination.data()) % alignof(T) != 0) {
    return false;
  }
  const uint64_t finalSequence = loadLe64(&record);
  if (finalSequence == 0 || (finalSequence & 1U) != 0) {
    return false;
  }

  auto *sharedWords = reinterpret_cast<uint64_t *>(destination.data());
  const auto *recordWords = reinterpret_cast<const uint64_t *>(&record);
  std::atomic_ref<uint64_t>(sharedWords[0]).store(finalSequence - 1U, std::memory_order_relaxed);
  std::atomic_thread_fence(std::memory_order_seq_cst);
  for (size_t word = 1; word < sizeof(T) / sizeof(uint64_t); ++word) {
    std::atomic_ref<uint64_t>(sharedWords[word]).store(recordWords[word], std::memory_order_relaxed);
  }
  std::atomic_ref<uint64_t>(sharedWords[0]).store(finalSequence, std::memory_order_release);
  return true;
}

template <typename T>
std::optional<T> loadCxlOwnerRecord(std::span<std::byte> source, unsigned attempts = 16) {
  static_assert(sizeof(T) == kCxlCacheLineBytes);
  static_assert(alignof(T) == kCxlCacheLineBytes);
  static_assert(std::is_trivially_copyable_v<T>);
  if (source.size() != sizeof(T) || reinterpret_cast<uintptr_t>(source.data()) % alignof(T) != 0) {
    return std::nullopt;
  }

  auto *sharedWords = reinterpret_cast<uint64_t *>(source.data());
  T snapshot{};
  auto *snapshotWords = reinterpret_cast<uint64_t *>(&snapshot);
  for (unsigned attempt = 0; attempt < attempts; ++attempt) {
    const uint64_t before = std::atomic_ref<uint64_t>(sharedWords[0]).load(std::memory_order_acquire);
    if (before == 0 || (before & 1U) != 0) {
      continue;
    }
    for (size_t word = 1; word < sizeof(T) / sizeof(uint64_t); ++word) {
      snapshotWords[word] = std::atomic_ref<uint64_t>(sharedWords[word]).load(std::memory_order_relaxed);
    }
    std::atomic_thread_fence(std::memory_order_acquire);
    const uint64_t after = std::atomic_ref<uint64_t>(sharedWords[0]).load(std::memory_order_acquire);
    if (before == after && (after & 1U) == 0) {
      snapshotWords[0] = after;
      return snapshot;
    }
  }
  return std::nullopt;
}

template <typename T>
std::optional<T> loadCxlOwnerRecord(std::span<const std::byte> source, unsigned attempts = 16) {
  // The mapping remains concurrently mutable even when reached through a
  // const observer. The non-const overload performs atomic loads only.
  return loadCxlOwnerRecord<T>(std::span<std::byte>(const_cast<std::byte *>(source.data()), source.size()), attempts);
}

}  // namespace hf3fs::net::cxl
