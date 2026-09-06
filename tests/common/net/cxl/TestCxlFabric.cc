#include <algorithm>
#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <fcntl.h>
#ifndef HF3FS_CXL_MINIMAL_TEST
#include <folly/ScopeGuard.h>
#endif
#include <gtest/gtest.h>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <unistd.h>
#include <vector>

#include "common/net/cxl/CxlFabric.h"
#ifndef HF3FS_CXL_MINIMAL_TEST
#include "common/net/TransportRuntime.h"
#include "common/serde/Serde.h"
#endif
#include "common/utils/Size.h"
#include "tests/GtestHelpers.h"

namespace hf3fs::net::cxl {
namespace {

using namespace std::chrono_literals;

constexpr uint64_t kTestRegionBytes = 1_MB;
constexpr std::string_view kReceipt = "run-token=endpoint-test\nmanifest=0123456789abcdef\n";

class FabricTemporaryFile {
 public:
  FabricTemporaryFile(std::string_view prefix, std::string_view contents = {}) {
    std::array<char, 64> name{};
    const std::string pattern = "/tmp/" + std::string(prefix) + "-XXXXXX";
    std::copy(pattern.begin(), pattern.end(), name.begin());
    fd_ = ::mkstemp(name.data());
    if (fd_ < 0) {
      throw std::runtime_error("mkstemp failed");
    }
    path_ = name.data();
    size_t offset = 0;
    while (offset != contents.size()) {
      const auto written = ::write(fd_, contents.data() + offset, contents.size() - offset);
      if (written <= 0) {
        throw std::runtime_error("write receipt failed");
      }
      offset += static_cast<size_t>(written);
    }
  }

  ~FabricTemporaryFile() {
    if (fd_ >= 0) {
      ::close(fd_);
    }
    if (!path_.empty()) {
      ::unlink(path_.c_str());
    }
  }

  int fd() const noexcept { return fd_; }
  const std::string &path() const noexcept { return path_; }

 private:
  int fd_{-1};
  std::string path_;
};

}  // namespace

class TestCxlFabric : public ::testing::Test {
 protected:
  void SetUp() override { ASSERT_EQ(::ftruncate(regionFile_.fd(), kTestRegionBytes), 0); }

  CxlLayoutManifest makeManifest(uint32_t endpointCount) const {
    CxlLayoutManifest manifest{
        .totalRegionBytes = kTestRegionBytes,
        .sessionGeneration = 43,
        .endpointCount = endpointCount,
        .laneCount = 1,
        .authorityEndpoint = EndpointId{1},
        .authorityGeneration = 17,
    };
    for (size_t index = 0; index < manifest.manifestSha256.size(); ++index) {
      manifest.manifestSha256[index] = static_cast<std::byte>(0x60U + index);
    }
    constexpr std::array<uint64_t, kCxlRangeCount> lengths{
        4_KB,
        4_KB,
        16_KB,
        8_KB,
        64_KB,
        4_KB,
        128_KB,
        4_KB,
    };
    uint64_t offset = kCxlSuperblockBytes;
    for (size_t index = 0; index < manifest.ranges.size(); ++index) {
      manifest.ranges[index] = CxlRange{static_cast<CxlRangeKind>(index + 1U), offset, lengths[index]};
      offset += lengths[index];
    }
    manifest.lifecycleRecordOffset = manifest.ranges.back().offset;
    return manifest;
  }

  CxlFabric::Config authorityConfig(uint32_t endpointCount = 1) const {
    return CxlFabric::Config{
        .mode = CxlFabric::StartMode::InitializeAuthority,
        .regionType = CxlFabric::RegionType::File,
        .regionPath = regionFile_.path(),
        .regionLength = kTestRegionBytes,
        .manifest = makeManifest(endpointCount),
        .endpoint = EndpointId{1},
        .endpointGeneration = 1,
        .capabilityBits = 0x01,
        .attachTimeout = 20ms,
        .heartbeatInterval = 1_ms,
        .authorityStaleTimeout = 50_ms,
        .shutdownTimeout = 50_ms,
        .authorityOwnerLock = lockFile_.path(),
        .authorityReceipt = std::string(kReceipt),
    };
  }

  CxlFabric::Config attachConfig(uint32_t endpointCount = 2, EndpointId endpoint = EndpointId{2}) const {
    return CxlFabric::Config{
        .mode = CxlFabric::StartMode::Attach,
        .regionType = CxlFabric::RegionType::File,
        .regionPath = regionFile_.path(),
        .regionLength = kTestRegionBytes,
        .manifest = makeManifest(endpointCount),
        .endpoint = endpoint,
        .endpointGeneration = 1,
        .capabilityBits = 0x02,
        .attachTimeout = 20ms,
        .heartbeatInterval = 1_ms,
        .authorityStaleTimeout = 50_ms,
        .shutdownTimeout = 50_ms,
        .authorityOwnerLock = {},
        .authorityReceipt = {},
    };
  }

#ifndef HF3FS_CXL_MINIMAL_TEST
  CxlRuntimeManifest makeRuntimeManifest(uint32_t endpointCount = 1) const {
    const auto layout = makeManifest(endpointCount);
    CxlRuntimeManifest manifest;
    manifest.schema = "hf3fs.cxl-runtime-manifest.v1";
    manifest.totalRegionBytes = layout.totalRegionBytes;
    manifest.sessionGeneration = layout.sessionGeneration;
    manifest.lifecycleRecordOffset = layout.lifecycleRecordOffset;
    manifest.manifestSha256 = "606162636465666768696a6b6c6d6e6f707172737475767778797a7b7c7d7e7f";
    manifest.endpointCount = layout.endpointCount;
    manifest.laneCount = layout.laneCount;
    manifest.authorityEndpoint = layout.authorityEndpoint.value;
    manifest.authorityGeneration = layout.authorityGeneration;
    for (const auto &range : layout.ranges) {
      manifest.ranges.push_back(CxlManifestRange{
          .kind = range.kind,
          .offset = range.offset,
          .length = range.length,
      });
    }
    manifest.participants.push_back(CxlManifestParticipant{
        .endpoint = 1,
        .localAddress = "CXL://127.0.0.1:9001",
    });
    manifest.routes.push_back(CxlManifestRoute{
        .address = "CXL://127.0.0.1:9001",
        .plane = ServicePlane::Data,
        .targetEndpoint = 1,
    });
    return manifest;
  }
#endif

  FabricTemporaryFile regionFile_{"hf3fs-fabric"};
  FabricTemporaryFile lockFile_{"hf3fs-owner", kReceipt};
};

TEST_F(TestCxlFabric, OneMappingIsSharedByMultipleProcessWorkers) {
  auto authority = CxlFabric::start(authorityConfig(2));
  ASSERT_OK(authority);
  auto attacher = CxlFabric::start(attachConfig());
  ASSERT_OK(attacher);
  ASSERT_OK((*authority)->check());
  ASSERT_OK((*attacher)->check());

  auto ownerBytes = (*authority)->layout().range(CxlRangeKind::BulkArenas);
  auto peerBytes = (*attacher)->layout().range(CxlRangeKind::BulkArenas);
  ASSERT_OK(ownerBytes);
  ASSERT_OK(peerBytes);
  (*ownerBytes)[123] = std::byte{0x5A};
  EXPECT_EQ((*peerBytes)[123], std::byte{0x5A});
  EXPECT_NE((*authority)->progressEngine().get(), (*attacher)->progressEngine().get());

  ASSERT_OK((*attacher)->stopAndJoin());
  EXPECT_FALSE((*attacher)->mapped());
  ASSERT_OK((*authority)->stopAndJoin());
  EXPECT_FALSE((*authority)->mapped());
}

TEST_F(TestCxlFabric, DuplicateLiveParticipantAndSecondAuthorityAreRejected) {
  auto authority = CxlFabric::start(authorityConfig());
  ASSERT_OK(authority);
  ASSERT_ERROR(CxlFabric::start(attachConfig(1, EndpointId{1})), StatusCode::kQueueConflict);
  ASSERT_ERROR(CxlFabric::start(authorityConfig()), StatusCode::kQueueConflict);
  ASSERT_OK((*authority)->stopAndJoin());
}

TEST_F(TestCxlFabric, AttachBeforeAuthorityNeverFormatsRegion) {
  std::vector<std::byte> prefix;
  {
    auto before = CxlRegion::mapFile(regionFile_.path(), kTestRegionBytes);
    ASSERT_OK(before);
    prefix.assign(before->bytes().begin(), before->bytes().begin() + kCxlSuperblockBytes);
  }

  auto early = attachConfig(1, EndpointId{1});
  early.attachTimeout = 2ms;
  ASSERT_ERROR(CxlFabric::start(std::move(early)), RPCCode::kDataPlaneHandshakeFailed);
  {
    auto after = CxlRegion::mapFile(regionFile_.path(), kTestRegionBytes);
    ASSERT_OK(after);
    EXPECT_TRUE(std::equal(prefix.begin(), prefix.end(), after->bytes().begin()));
  }

  auto authority = CxlFabric::start(authorityConfig());
  ASSERT_OK(authority);
  ASSERT_OK((*authority)->stopAndJoin());
}

TEST_F(TestCxlFabric, WrongAuthorityReceiptRollsBackBeforeMapping) {
  auto wrong = authorityConfig();
  wrong.authorityReceipt = "wrong receipt";
  ASSERT_ERROR(CxlFabric::start(std::move(wrong)), StatusCode::kInvalidConfig);

  auto mapped = CxlRegion::mapFile(regionFile_.path(), kTestRegionBytes);
  ASSERT_OK(mapped);
  EXPECT_TRUE(std::all_of(mapped->bytes().begin(), mapped->bytes().begin() + kCxlSuperblockBytes, [](std::byte value) {
    return value == std::byte{};
  }));
}

#ifndef HF3FS_CXL_MINIMAL_TEST
TEST_F(TestCxlFabric, RuntimeStartsAuthorityFromSerializedManifest) {
  FabricTemporaryFile manifestFile{"hf3fs-manifest", serde::toJsonString(makeRuntimeManifest())};
  TransportRuntime::Config config;
  config.set_enabled(true);
  config.set_mode(CxlFabric::StartMode::InitializeAuthority);
  config.set_region_type(CxlFabric::RegionType::File);
  config.set_region_path(regionFile_.path());
  config.set_region_length(kTestRegionBytes);
  config.set_manifest_path(manifestFile.path());
  config.set_endpoint(1);
  config.set_endpoint_generation(1);
  config.set_capability_bits(1);
  config.set_attach_timeout(20_ms);
  config.set_heartbeat_interval(1_ms);
  config.set_authority_stale_timeout(50_ms);
  config.set_shutdown_timeout(50_ms);
  config.set_authority_owner_lock(lockFile_.path());
  config.set_authority_receipt(std::string(kReceipt));

  ASSERT_OK(TransportRuntime::startConfigured(config));
  auto cleanup = folly::makeGuard([] { (void)TransportRuntime::stopCxl(); });
  EXPECT_NE(TransportRuntime::cxlFabric(), nullptr);
  EXPECT_NE(TransportRuntime::cxlBufferArena(), nullptr);
  ASSERT_OK(TransportRuntime::stopCxl());
  cleanup.dismiss();
}

TEST_F(TestCxlFabric, RuntimeRejectsIncompleteManifestBeforeMapping) {
  auto manifest = makeRuntimeManifest();
  manifest.ranges.pop_back();
  FabricTemporaryFile manifestFile{"hf3fs-bad-manifest", serde::toJsonString(manifest)};
  TransportRuntime::Config config;
  config.set_enabled(true);
  config.set_mode(CxlFabric::StartMode::InitializeAuthority);
  config.set_region_type(CxlFabric::RegionType::File);
  config.set_region_path(regionFile_.path());
  config.set_region_length(kTestRegionBytes);
  config.set_manifest_path(manifestFile.path());
  config.set_endpoint(1);
  config.set_endpoint_generation(1);
  config.set_authority_owner_lock(lockFile_.path());
  config.set_authority_receipt(std::string(kReceipt));

  ASSERT_ERROR(TransportRuntime::startConfigured(config), StatusCode::kInvalidConfig);
  EXPECT_EQ(TransportRuntime::cxlFabric(), nullptr);
}
#endif

TEST_F(TestCxlFabric, NormalShutdownPublishesRetirementBeforeUnmap) {
  auto authority = CxlFabric::start(authorityConfig());
  ASSERT_OK(authority);
  ASSERT_OK((*authority)->stopAndJoin());
  EXPECT_FALSE((*authority)->mapped());

  auto mapped = CxlRegion::mapFile(regionFile_.path(), kTestRegionBytes);
  ASSERT_OK(mapped);
  const auto manifest = makeManifest(1);
  auto lifecycleBytes = mapped->checkedRange(manifest.lifecycleRecordOffset,
                                             sizeof(CxlFabricLifecycleRecord),
                                             alignof(CxlFabricLifecycleRecord));
  ASSERT_OK(lifecycleBytes);
  auto lifecycle = loadCxlOwnerRecord<CxlFabricLifecycleRecord>(*lifecycleBytes);
  ASSERT_TRUE(lifecycle.has_value());
  EXPECT_EQ(loadLe32(&lifecycle->lifecycle), static_cast<uint32_t>(CxlFabricLifecycle::Retired));

  auto endpointBytes =
      mapped->checkedRange(manifest.ranges[0].offset, sizeof(CxlEndpointRecord), alignof(CxlEndpointRecord));
  ASSERT_OK(endpointBytes);
  auto endpoint = loadCxlOwnerRecord<CxlEndpointRecord>(*endpointBytes);
  ASSERT_TRUE(endpoint.has_value());
  EXPECT_EQ(loadLe32(&endpoint->lifecycle), static_cast<uint32_t>(CxlEndpointLifecycle::Retired));
}

TEST_F(TestCxlFabric, CorruptAuthorityLifecycleFaultsAttacherWithoutTakeover) {
  auto authorityConfigValue = authorityConfig(2);
  authorityConfigValue.heartbeatInterval = 1_s;
  authorityConfigValue.authorityStaleTimeout = 2_s;
  auto authority = CxlFabric::start(std::move(authorityConfigValue));
  ASSERT_OK(authority);
  bool authorityHeartbeatStarted = false;
  for (size_t attempt = 0; attempt < 100 && !authorityHeartbeatStarted; ++attempt) {
    auto lifecycle = (*authority)->lifecycleSnapshot();
    ASSERT_OK(lifecycle);
    authorityHeartbeatStarted = loadLe64(&lifecycle->heartbeat) > 1;
    if (!authorityHeartbeatStarted) {
      std::this_thread::sleep_for(1ms);
    }
  }
  ASSERT_TRUE(authorityHeartbeatStarted);
  auto attachConfigValue = attachConfig();
  attachConfigValue.authorityStaleTimeout = 2_s;
  auto attacher = CxlFabric::start(std::move(attachConfigValue));
  ASSERT_OK(attacher);

  auto lifecycleBytes = (*authority)
                            ->region()
                            .checkedRange(makeManifest(2).lifecycleRecordOffset,
                                          sizeof(CxlFabricLifecycleRecord),
                                          alignof(CxlFabricLifecycleRecord));
  ASSERT_OK(lifecycleBytes);
  auto &record = *reinterpret_cast<CxlFabricLifecycleRecord *>(lifecycleBytes->data());
  storeLe32(&record.recordCrc32c, loadLe32(&record.recordCrc32c) ^ 1U);

  bool faulted = false;
  for (size_t attempt = 0; attempt < 100 && !faulted; ++attempt) {
    std::this_thread::sleep_for(1ms);
    faulted = (*attacher)->check().hasError();
  }
  EXPECT_TRUE(faulted);
  EXPECT_FALSE((*attacher)->acceptingSubmissions());
  EXPECT_FALSE((*attacher)->config().mode == CxlFabric::StartMode::InitializeAuthority);
  ASSERT_ERROR((*attacher)->stopAndJoin(), StatusCode::kDataCorruption);
  ASSERT_ERROR((*authority)->stopAndJoin(), RPCCode::kDataPlaneHandshakeFailed);
}

TEST_F(TestCxlFabric, PausedLifecyclePublicationRecoversBeforeStaleDeadline) {
  auto mapped = CxlRegion::mapFile(regionFile_.path(), kTestRegionBytes);
  ASSERT_OK(mapped);
  auto layout = CxlLayout::initializeAsAuthority(*mapped, makeManifest(2), EndpointId{1});
  ASSERT_OK(layout);
  auto bytes = mapped->checkedRange(makeManifest(2).lifecycleRecordOffset,
                                    sizeof(CxlFabricLifecycleRecord),
                                    alignof(CxlFabricLifecycleRecord));
  ASSERT_OK(bytes);
  auto config = attachConfig();
  config.authorityStaleTimeout = 500_ms;
  auto attacher = CxlFabric::start(config);
  ASSERT_OK(attacher);

  // This test thread is the only authority publisher. Pause after its odd
  // sequence, long enough for several observer heartbeats, then finish it.
  auto sequence = std::atomic_ref<uint64_t>(*reinterpret_cast<uint64_t *>(bytes->data()));
  sequence.store(5, std::memory_order_release);
  std::this_thread::sleep_for(20ms);
  EXPECT_FALSE((*attacher)->check().hasError());
  auto next = layout->lifecycle();
  storeLe64(&next.recordSequence, 6);
  storeLe64(&next.heartbeat, 2);
  storeLe32(&next.recordCrc32c,
            cxlCrc32cWithZeroedU32(next, offsetof(CxlFabricLifecycleRecord, recordCrc32c)));
  ASSERT_TRUE(publishCxlOwnerRecord(*bytes, next));
  std::this_thread::sleep_for(10ms);
  EXPECT_FALSE((*attacher)->check().hasError());
  EXPECT_TRUE((*attacher)->acceptingSubmissions());
  ASSERT_OK((*attacher)->stopAndJoin());
}

TEST_F(TestCxlFabric, StuckLifecyclePublicationStillExpires) {
  auto mapped = CxlRegion::mapFile(regionFile_.path(), kTestRegionBytes);
  ASSERT_OK(mapped);
  auto layout = CxlLayout::initializeAsAuthority(*mapped, makeManifest(2), EndpointId{1});
  ASSERT_OK(layout);
  auto bytes = mapped->checkedRange(makeManifest(2).lifecycleRecordOffset,
                                    sizeof(CxlFabricLifecycleRecord),
                                    alignof(CxlFabricLifecycleRecord));
  ASSERT_OK(bytes);
  auto attacher = CxlFabric::start(attachConfig());
  ASSERT_OK(attacher);
  std::atomic_ref<uint64_t>(*reinterpret_cast<uint64_t *>(bytes->data())).store(5, std::memory_order_release);
  for (size_t attempt = 0; attempt < 200 && (*attacher)->check(); ++attempt) {
    std::this_thread::sleep_for(1ms);
  }
  ASSERT_ERROR((*attacher)->check(), RPCCode::kStaleGeneration);
  EXPECT_FALSE((*attacher)->acceptingSubmissions());
  ASSERT_ERROR((*attacher)->stopAndJoin(), RPCCode::kStaleGeneration);
}

}  // namespace hf3fs::net::cxl
