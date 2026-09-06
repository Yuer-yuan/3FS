#pragma once

#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <span>
#include <utility>

#include "common/net/cxl/CxlAbi.h"
#include "common/net/cxl/CxlRegion.h"

namespace hf3fs::net::cxl {

struct CxlRange {
  CxlRangeKind kind{};
  uint64_t offset{};
  uint64_t length{};

  bool operator==(const CxlRange &) const = default;
};

struct CxlLayoutManifest {
  uint64_t totalRegionBytes{};
  uint64_t sessionGeneration{};
  uint64_t lifecycleRecordOffset{};
  std::array<std::byte, 32> manifestSha256{};
  uint32_t endpointCount{};
  uint32_t laneCount{};
  EndpointId authorityEndpoint{};
  uint64_t authorityGeneration{};
  std::array<CxlRange, kCxlRangeCount> ranges{};
};

struct LayoutExpectation {
  uint64_t sessionGeneration{};
  std::array<std::byte, 32> manifestSha256{};
  std::optional<uint64_t> totalRegionBytes;
  std::optional<uint32_t> endpointCount;
  std::optional<uint32_t> laneCount;
  std::optional<EndpointId> authorityEndpoint;
};

class CxlLayout {
 public:
  static Result<CxlLayout> initializeAsAuthority(CxlRegion &region,
                                                 const CxlLayoutManifest &manifest,
                                                 EndpointId authority);

  static Result<CxlLayout> attach(CxlRegion &region,
                                  const LayoutExpectation &expectation,
                                  std::chrono::milliseconds timeout);

  static Result<CxlLayout> loadAndValidate(CxlRegion &region, const LayoutExpectation &expectation);

  Result<std::span<std::byte>> range(CxlRangeKind kind, size_t alignment = kCxlCacheLineBytes);
  Result<std::span<const std::byte>> range(CxlRangeKind kind, size_t alignment = kCxlCacheLineBytes) const;

  const CxlSuperblock &superblock() const noexcept { return superblock_; }
  const CxlFabricLifecycleRecord &lifecycle() const noexcept { return lifecycle_; }
  const std::array<CxlRange, kCxlRangeCount> &ranges() const noexcept { return ranges_; }
  uint64_t sessionGeneration() const noexcept { return loadLe64(&superblock_.header.sessionGeneration); }

 private:
  CxlLayout(CxlRegion &region,
            CxlSuperblock superblock,
            CxlFabricLifecycleRecord lifecycle,
            std::array<CxlRange, kCxlRangeCount> ranges)
      : region_(&region),
        superblock_(std::move(superblock)),
        lifecycle_(std::move(lifecycle)),
        ranges_(std::move(ranges)) {}

  CxlRegion *region_;
  CxlSuperblock superblock_{};
  CxlFabricLifecycleRecord lifecycle_{};
  std::array<CxlRange, kCxlRangeCount> ranges_{};
};

}  // namespace hf3fs::net::cxl
