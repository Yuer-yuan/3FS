#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <gtest/gtest.h>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unistd.h>

#include "common/net/cxl/CxlEndpointState.h"
#include "common/utils/Size.h"
#include "tests/GtestHelpers.h"

namespace hf3fs::net::cxl {
namespace {

using namespace std::chrono_literals;

constexpr uint64_t kTestRegionBytes = 1_MB;

class EndpointTemporaryFile {
 public:
  EndpointTemporaryFile() {
    std::array<char, 36> name{};
    constexpr std::string_view pattern = "/tmp/hf3fs-endpoint-XXXXXX";
    std::copy(pattern.begin(), pattern.end(), name.begin());
    fd_ = ::mkstemp(name.data());
    if (fd_ < 0) {
      throw std::runtime_error("mkstemp failed");
    }
    path_ = name.data();
  }

  ~EndpointTemporaryFile() {
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

class TestCxlEndpointState : public ::testing::Test {
 protected:
  void SetUp() override {
    ASSERT_EQ(::ftruncate(file_.fd(), kTestRegionBytes), 0);
    auto region = CxlRegion::mapFile(file_.path(), kTestRegionBytes);
    ASSERT_OK(region);
    region_.emplace(std::move(*region));
    manifest_ = makeManifest();
    auto layout = CxlLayout::initializeAsAuthority(*region_, manifest_, EndpointId{1});
    ASSERT_OK(layout);
    layout_.emplace(std::move(*layout));
  }

  CxlLayoutManifest makeManifest() const {
    CxlLayoutManifest manifest{
        .totalRegionBytes = kTestRegionBytes,
        .sessionGeneration = 37,
        .endpointCount = 3,
        .laneCount = 1,
        .authorityEndpoint = EndpointId{1},
        .authorityGeneration = 13,
    };
    for (size_t index = 0; index < manifest.manifestSha256.size(); ++index) {
      manifest.manifestSha256[index] = static_cast<std::byte>(0x20U + index);
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

  std::span<std::byte> endpointBytes(EndpointId endpoint) {
    auto directory = layout_->range(CxlRangeKind::EndpointDirectory, alignof(CxlEndpointRecord));
    if (!directory) {
      throw std::runtime_error("invalid endpoint directory");
    }
    return directory->subspan((endpoint.value - 1U) * sizeof(CxlEndpointRecord), sizeof(CxlEndpointRecord));
  }

  EndpointTemporaryFile file_;
  CxlLayoutManifest manifest_;
  std::optional<CxlRegion> region_;
  std::optional<CxlLayout> layout_;
};

TEST_F(TestCxlEndpointState, PublishesLifecycleHeartbeatAndExactRestartGeneration) {
  auto claimed = CxlEndpointState::claim(*layout_, EndpointId{1}, 1, 0x55);
  ASSERT_OK(claimed);
  CxlEndpointState state = std::move(*claimed);
  auto starting = state.snapshot();
  ASSERT_OK(starting);
  EXPECT_EQ(loadLe32(&starting->lifecycle), static_cast<uint32_t>(CxlEndpointLifecycle::Starting));
  EXPECT_EQ(loadLe64(&starting->heartbeat), 0);
  EXPECT_EQ(loadLe64(&starting->capabilityBits), 0x55);

  ASSERT_OK(state.publish(CxlEndpointLifecycle::Ready));
  ASSERT_OK(state.heartbeat());
  auto ready = state.snapshot();
  ASSERT_OK(ready);
  EXPECT_EQ(loadLe32(&ready->lifecycle), static_cast<uint32_t>(CxlEndpointLifecycle::Ready));
  EXPECT_EQ(loadLe64(&ready->heartbeat), 1);
  ASSERT_ERROR(state.publish(CxlEndpointLifecycle::Starting), StatusCode::kInvalidArg);

  ASSERT_OK(state.publish(CxlEndpointLifecycle::Draining));
  ASSERT_OK(state.publish(CxlEndpointLifecycle::Retired));
  auto restarted = CxlEndpointState::claim(*layout_, EndpointId{1}, 2, 0xAA);
  ASSERT_OK(restarted);
  EXPECT_EQ(restarted->endpointGeneration(), 2);
}

TEST_F(TestCxlEndpointState, RejectsDuplicateLiveClaimAndSkippedGeneration) {
  auto first = CxlEndpointState::claim(*layout_, EndpointId{2}, 1, 0);
  ASSERT_OK(first);
  ASSERT_ERROR(CxlEndpointState::claim(*layout_, EndpointId{2}, 1, 0), StatusCode::kQueueConflict);
  ASSERT_OK(first->publish(CxlEndpointLifecycle::Ready));
  ASSERT_OK(first->publish(CxlEndpointLifecycle::Draining));
  ASSERT_OK(first->publish(CxlEndpointLifecycle::Retired));
  ASSERT_ERROR(CxlEndpointState::claim(*layout_, EndpointId{2}, 3, 0), RPCCode::kStaleGeneration);
}

TEST_F(TestCxlEndpointState, ZeroGenerationClaimsNextRetiredIncarnation) {
  auto first = CxlEndpointState::claim(*layout_, EndpointId{2}, 0, 0);
  ASSERT_OK(first);
  EXPECT_EQ(first->endpointGeneration(), 1);
  ASSERT_ERROR(CxlEndpointState::claim(*layout_, EndpointId{2}, 0, 0), StatusCode::kQueueConflict);
  ASSERT_OK(first->publish(CxlEndpointLifecycle::Ready));
  ASSERT_OK(first->publish(CxlEndpointLifecycle::Draining));
  ASSERT_OK(first->publish(CxlEndpointLifecycle::Retired));

  auto second = CxlEndpointState::claim(*layout_, EndpointId{2}, 0, 0);
  ASSERT_OK(second);
  EXPECT_EQ(second->endpointGeneration(), 2);
}

TEST_F(TestCxlEndpointState, EachOwnerOnlyChangesItsOwnRecord) {
  auto firstResult = CxlEndpointState::claim(*layout_, EndpointId{1}, 1, 0);
  auto secondResult = CxlEndpointState::claim(*layout_, EndpointId{2}, 1, 0);
  ASSERT_OK(firstResult);
  ASSERT_OK(secondResult);
  CxlEndpointState first = std::move(*firstResult);
  CxlEndpointState second = std::move(*secondResult);
  auto secondBefore = second.snapshot();
  ASSERT_OK(secondBefore);
  ASSERT_OK(first.heartbeat());
  auto secondAfter = second.snapshot();
  ASSERT_OK(secondAfter);
  EXPECT_EQ(loadLe64(&secondAfter->recordSequence), loadLe64(&secondBefore->recordSequence));
  EXPECT_EQ(loadLe64(&secondAfter->heartbeat), loadLe64(&secondBefore->heartbeat));
}

TEST_F(TestCxlEndpointState, DistinguishesPendingPublicationFromStableCorruption) {
  auto claimed = CxlEndpointState::claim(*layout_, EndpointId{1}, 1, 0);
  ASSERT_OK(claimed);
  auto bytes = endpointBytes(EndpointId{1});
  auto *sequence = reinterpret_cast<uint64_t *>(bytes.data());
  const uint64_t even = std::atomic_ref<uint64_t>(*sequence).load(std::memory_order_acquire);
  std::atomic_ref<uint64_t>(*sequence).store(even + 1U, std::memory_order_release);
  ASSERT_ERROR(claimed->snapshot(), RPCCode::kTimeout);

  std::atomic_ref<uint64_t>(*sequence).store(even, std::memory_order_release);
  auto &record = *reinterpret_cast<CxlEndpointRecord *>(bytes.data());
  storeLe32(&record.recordCrc32c, loadLe32(&record.recordCrc32c) ^ 1U);
  ASSERT_ERROR(claimed->snapshot(), StatusCode::kDataCorruption);
}

TEST_F(TestCxlEndpointState, MatchesFullWidthEndpointGeneration) {
  auto observerResult = CxlEndpointState::claim(*layout_, EndpointId{1}, 1, 0);
  ASSERT_OK(observerResult);
  CxlEndpointState observer = std::move(*observerResult);
  constexpr uint64_t wideGeneration = (uint64_t{1} << 48U) + 9U;
  CxlEndpointRecord record{};
  storeLe64(&record.recordSequence, 2);
  storeLe64(&record.sessionGeneration, manifest_.sessionGeneration);
  storeLe32(&record.endpointId, 3);
  storeLe32(&record.lifecycle, static_cast<uint32_t>(CxlEndpointLifecycle::Ready));
  storeLe64(&record.endpointGeneration, wideGeneration);
  storeLe32(&record.recordCrc32c, cxlCrc32cWithZeroedU32(record, offsetof(CxlEndpointRecord, recordCrc32c)));
  ASSERT_TRUE(publishCxlOwnerRecord(endpointBytes(EndpointId{3}), record));

  ASSERT_OK(observer.snapshot(EndpointId{3}, wideGeneration));
  ASSERT_ERROR(observer.snapshot(EndpointId{3}, static_cast<uint32_t>(wideGeneration)), RPCCode::kStaleGeneration);
}

TEST_F(TestCxlEndpointState, PeerStalenessUsesLocalObservationTime) {
  auto observerResult = CxlEndpointState::claim(*layout_, EndpointId{1}, 1, 0);
  auto peerResult = CxlEndpointState::claim(*layout_, EndpointId{2}, 1, 0);
  ASSERT_OK(observerResult);
  ASSERT_OK(peerResult);
  CxlEndpointState observer = std::move(*observerResult);
  CxlEndpointState peer = std::move(*peerResult);
  ASSERT_OK(peer.publish(CxlEndpointLifecycle::Ready));

  const auto initial = std::chrono::steady_clock::now();
  ASSERT_RESULT_EQ(false, observer.peerStale(EndpointId{2}, 1_s, initial));
  ASSERT_RESULT_EQ(true, observer.peerStale(EndpointId{2}, 1_s, initial + 2s));
  ASSERT_OK(peer.heartbeat());
  ASSERT_RESULT_EQ(false, observer.peerStale(EndpointId{2}, 1_s, initial + 2s));
  ASSERT_RESULT_EQ(true, observer.peerStale(EndpointId{2}, 1_s, initial + 4s));
}

TEST_F(TestCxlEndpointState, PendingPublicationDoesNotRenewPeerLiveness) {
  auto observer = CxlEndpointState::claim(*layout_, EndpointId{1}, 1, 0);
  auto peer = CxlEndpointState::claim(*layout_, EndpointId{2}, 1, 0);
  ASSERT_OK(observer);
  ASSERT_OK(peer);
  ASSERT_OK(peer->publish(CxlEndpointLifecycle::Ready));
  const auto initial = std::chrono::steady_clock::now();
  ASSERT_RESULT_EQ(false, observer->peerStale(EndpointId{2}, 1_s, initial));
  auto sequence = std::atomic_ref<uint64_t>(*reinterpret_cast<uint64_t *>(endpointBytes(EndpointId{2}).data()));
  const auto even = sequence.load(std::memory_order_acquire);
  sequence.store(even + 1U, std::memory_order_release);
  ASSERT_ERROR(observer->peerStale(EndpointId{2}, 1_s, initial + 2s), RPCCode::kTimeout);
  sequence.store(even, std::memory_order_release);
  ASSERT_RESULT_EQ(true, observer->peerStale(EndpointId{2}, 1_s, initial + 2s));
  ASSERT_OK(peer->heartbeat());
  ASSERT_RESULT_EQ(false, observer->peerStale(EndpointId{2}, 1_s, initial + 2s));
}

TEST_F(TestCxlEndpointState, FaultIsPublishedAndCannotBeReclaimedInSession) {
  auto claimed = CxlEndpointState::claim(*layout_, EndpointId{2}, 1, 0);
  ASSERT_OK(claimed);
  ASSERT_OK(claimed->fault());
  ASSERT_OK(claimed->fault());
  auto snapshot = claimed->snapshot();
  ASSERT_OK(snapshot);
  EXPECT_EQ(loadLe32(&snapshot->lifecycle), static_cast<uint32_t>(CxlEndpointLifecycle::Faulted));
  ASSERT_ERROR(CxlEndpointState::claim(*layout_, EndpointId{2}, 2, 0), StatusCode::kQueueConflict);
}

}  // namespace hf3fs::net::cxl
