#include <algorithm>
#include <array>
#include <atomic>
#include <cstdlib>
#include <cstring>
#include <gtest/gtest.h>
#include <limits>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unistd.h>
#include <vector>

#include "common/net/cxl/CxlLane.h"
#include "common/utils/Size.h"
#include "tests/GtestHelpers.h"

namespace hf3fs::net::cxl {
namespace {

constexpr uint64_t kTestRegionBytes = 1_MB;
constexpr uint32_t kTestDepth = 4;
constexpr uint32_t kTestCellBytes = 256;

class LaneTemporaryFile {
 public:
  LaneTemporaryFile() {
    std::array<char, 32> name{};
    constexpr std::string_view pattern = "/tmp/hf3fs-lane-XXXXXX";
    std::copy(pattern.begin(), pattern.end(), name.begin());
    fd_ = ::mkstemp(name.data());
    if (fd_ < 0) {
      throw std::runtime_error("mkstemp failed");
    }
    path_ = name.data();
  }

  ~LaneTemporaryFile() {
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

std::vector<std::byte> deterministicBytes(size_t size, uint8_t seed) {
  std::vector<std::byte> bytes(size);
  for (size_t index = 0; index < bytes.size(); ++index) {
    bytes[index] = static_cast<std::byte>((index * 31U + seed) & 0xFFU);
  }
  return bytes;
}

}  // namespace

class MappedCxlLaneTest : public ::testing::Test {
 protected:
  void SetUp() override {
    ASSERT_EQ(::ftruncate(file_.fd(), kTestRegionBytes), 0);
    auto requesterRegion = CxlRegion::mapFile(file_.path(), kTestRegionBytes);
    auto acceptorRegion = CxlRegion::mapFile(file_.path(), kTestRegionBytes);
    ASSERT_OK(requesterRegion);
    ASSERT_OK(acceptorRegion);
    requesterRegion_.emplace(std::move(*requesterRegion));
    acceptorRegion_.emplace(std::move(*acceptorRegion));

    manifest_ = makeManifest();
    auto requesterLayout = CxlLayout::initializeAsAuthority(*requesterRegion_, manifest_, EndpointId{1});
    ASSERT_OK(requesterLayout);
    requesterLayout_.emplace(std::move(*requesterLayout));
    auto acceptorLayout = CxlLayout::attach(*acceptorRegion_, expectation(), std::chrono::milliseconds(10));
    ASSERT_OK(acceptorLayout);
    acceptorLayout_.emplace(std::move(*acceptorLayout));

    config_ = makeLaneConfig();
    auto requester = CxlLane::create(*requesterRegion_, *requesterLayout_, config_, CxlLaneRole::Requester);
    auto acceptor = CxlLane::create(*acceptorRegion_, *acceptorLayout_, config_, CxlLaneRole::Acceptor);
    ASSERT_OK(requester);
    ASSERT_OK(acceptor);
    requester_.emplace(std::move(*requester));
    acceptor_.emplace(std::move(*acceptor));
  }

  CxlLayoutManifest makeManifest() const {
    CxlLayoutManifest manifest{
        .totalRegionBytes = kTestRegionBytes,
        .sessionGeneration = 9,
        .endpointCount = 2,
        .laneCount = 1,
        .authorityEndpoint = EndpointId{1},
        .authorityGeneration = 7,
    };
    for (size_t index = 0; index < manifest.manifestSha256.size(); ++index) {
      manifest.manifestSha256[index] = static_cast<std::byte>(0x80U + index);
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

  LayoutExpectation expectation() const {
    return LayoutExpectation{
        .sessionGeneration = manifest_.sessionGeneration,
        .manifestSha256 = manifest_.manifestSha256,
        .totalRegionBytes = manifest_.totalRegionBytes,
        .endpointCount = manifest_.endpointCount,
        .laneCount = manifest_.laneCount,
        .authorityEndpoint = manifest_.authorityEndpoint,
    };
  }

  CxlLaneConfig makeLaneConfig() const {
    const auto &cursorRange = manifest_.ranges[static_cast<size_t>(CxlRangeKind::CursorPages) - 1U];
    const auto &frameRange = manifest_.ranges[static_cast<size_t>(CxlRangeKind::FrameRings) - 1U];
    const auto &payloadRange = manifest_.ranges[static_cast<size_t>(CxlRangeKind::RpcPayloadCells) - 1U];
    const uint64_t ringBytes = kTestDepth * sizeof(CxlFrameEntry);
    const uint64_t payloadBytes = kTestDepth * kTestCellBytes;
    return CxlLaneConfig{
        .sessionGeneration = manifest_.sessionGeneration,
        .laneGeneration = 3,
        .depth = kTestDepth,
        .cellBytes = kTestCellBytes,
        .directions =
            {
                CxlLaneDirectionLayout{
                    .producerCursorOffset = cursorRange.offset,
                    .consumerCursorOffset = cursorRange.offset + 4_KB,
                    .deliveredRecordOffset = cursorRange.offset + 4_KB + kCxlCacheLineBytes,
                    .frameRingOffset = frameRange.offset,
                    .payloadCellsOffset = payloadRange.offset,
                },
                CxlLaneDirectionLayout{
                    .producerCursorOffset = cursorRange.offset + 8_KB,
                    .consumerCursorOffset = cursorRange.offset + 12_KB,
                    .deliveredRecordOffset = cursorRange.offset + 12_KB + kCxlCacheLineBytes,
                    .frameRingOffset = frameRange.offset + ringBytes,
                    .payloadCellsOffset = payloadRange.offset + payloadBytes,
                },
            },
    };
  }

  Result<size_t> push(const std::vector<std::byte> &bytes) {
    return requester_->tryPush(Direction::Submission, FrameView{bytes});
  }

  CxlFrameEntry &submissionEntry(uint64_t sequence) {
    const auto offset = config_.directions[0].frameRingOffset + (sequence % config_.depth) * sizeof(CxlFrameEntry);
    auto range = requesterRegion_->checkedRange(offset, sizeof(CxlFrameEntry), alignof(CxlFrameEntry));
    if (!range) {
      throw std::runtime_error("invalid test frame range");
    }
    return *reinterpret_cast<CxlFrameEntry *>(range->data());
  }

  std::span<std::byte> submissionPayload(uint64_t sequence, size_t length) {
    const auto offset = config_.directions[0].payloadCellsOffset + (sequence % config_.depth) * config_.cellBytes;
    auto range = requesterRegion_->checkedRange(offset, length, kCxlCacheLineBytes);
    if (!range) {
      throw std::runtime_error("invalid test payload range");
    }
    return *range;
  }

  void recomputeSubmissionCrc(uint64_t sequence) {
    auto &entry = submissionEntry(sequence);
    auto payload = submissionPayload(sequence, loadLe32(&entry.payloadLength));
    storeLe32(&entry.crc32c, cxlCrc32cWithZeroedU32(entry, offsetof(CxlFrameEntry, crc32c), payload));
  }

  void setCursor(uint64_t offset, uint64_t value) {
    auto range = requesterRegion_->checkedRange(offset, sizeof(uint64_t), alignof(uint64_t));
    ASSERT_OK(range);
    std::atomic_ref<uint64_t>(*reinterpret_cast<uint64_t *>(range->data())).store(value, std::memory_order_release);
  }

  std::vector<std::byte> snapshotSubmissionSlot(uint64_t sequence) {
    std::vector<std::byte> snapshot(sizeof(CxlFrameEntry) + config_.cellBytes);
    std::memcpy(snapshot.data(), &submissionEntry(sequence), sizeof(CxlFrameEntry));
    auto payload = submissionPayload(sequence, config_.cellBytes);
    std::memcpy(snapshot.data() + sizeof(CxlFrameEntry), payload.data(), payload.size());
    return snapshot;
  }

  LaneTemporaryFile file_;
  CxlLayoutManifest manifest_;
  CxlLaneConfig config_;
  std::optional<CxlRegion> requesterRegion_;
  std::optional<CxlRegion> acceptorRegion_;
  std::optional<CxlLayout> requesterLayout_;
  std::optional<CxlLayout> acceptorLayout_;
  std::optional<CxlLane> requester_;
  std::optional<CxlLane> acceptor_;
};

TEST_F(MappedCxlLaneTest, FullQueueDoesNotOverwrite) {
  for (uint8_t index = 0; index < kTestDepth; ++index) {
    ASSERT_RESULT_EQ(1, push(deterministicBytes(1, index)));
  }
  auto before = snapshotSubmissionSlot(0);
  ASSERT_ERROR(push(deterministicBytes(1, 99)), StatusCode::kQueueFull);
  EXPECT_EQ(snapshotSubmissionSlot(0), before);
  EXPECT_FALSE(requester_->isRetired());
}

TEST_F(MappedCxlLaneTest, ConsumerControlsReuse) {
  for (uint8_t index = 0; index < kTestDepth; ++index) {
    ASSERT_RESULT_EQ(1, push(deterministicBytes(1, index)));
  }
  std::array<std::byte, 1> output{};
  ASSERT_RESULT_EQ(1, acceptor_->tryPop(Direction::Submission, output));
  ASSERT_RESULT_EQ(1, push(deterministicBytes(1, 55)));
  EXPECT_EQ(acceptor_->consumerCursor(Direction::Submission), 1);
  EXPECT_EQ(requester_->producerCursor(Direction::Submission), kTestDepth + 1);
}

TEST_F(MappedCxlLaneTest, PartialPopPublishesDeliveredBeforeCellReuse) {
  auto payload = deterministicBytes(100, 7);
  ASSERT_RESULT_EQ(payload.size(), push(payload));
  std::array<std::byte, 17> first{};
  ASSERT_RESULT_EQ(first.size(), acceptor_->tryPop(Direction::Submission, first));
  ASSERT_RESULT_EQ(first.size(), requester_->peerDeliveredOffset(Direction::Submission));
  EXPECT_EQ(acceptor_->consumerCursor(Direction::Submission), 0);

  std::array<std::byte, 128> remainder{};
  ASSERT_RESULT_EQ(payload.size() - first.size(), acceptor_->tryPop(Direction::Submission, remainder));
  ASSERT_RESULT_EQ(payload.size(), requester_->peerDeliveredOffset(Direction::Submission));
  EXPECT_EQ(acceptor_->consumerCursor(Direction::Submission), 1);
  EXPECT_TRUE(std::equal(first.begin(), first.end(), payload.begin()));
  EXPECT_TRUE(std::equal(remainder.begin(), remainder.begin() + 83, payload.begin() + 17));
}

TEST_F(MappedCxlLaneTest, ReassemblesAcrossRingWrap) {
  std::vector<std::byte> expected;
  std::vector<std::byte> received;
  for (uint8_t frame = 0; frame < 3 * kTestDepth + 1; ++frame) {
    auto payload = deterministicBytes(3 * kTestCellBytes / 4 + frame, frame);
    expected.insert(expected.end(), payload.begin(), payload.end());
    ASSERT_RESULT_EQ(payload.size(), push(payload));
    size_t remaining = payload.size();
    while (remaining != 0) {
      std::array<std::byte, 37> buffer{};
      auto popped = acceptor_->tryPop(Direction::Submission, buffer);
      ASSERT_OK(popped);
      received.insert(received.end(), buffer.begin(), buffer.begin() + *popped);
      remaining -= *popped;
    }
  }
  EXPECT_EQ(received, expected);
}

TEST_F(MappedCxlLaneTest, RejectsStaleLaneGeneration) {
  ASSERT_RESULT_EQ(8, push(deterministicBytes(8, 1)));
  storeLe64(&submissionEntry(0).laneGeneration, config_.laneGeneration + 1);
  std::array<std::byte, 8> output{};
  ASSERT_ERROR(acceptor_->tryPop(Direction::Submission, output), StatusCode::kDataCorruption);
  EXPECT_TRUE(acceptor_->isRetired());
}

TEST_F(MappedCxlLaneTest, RejectsStaleSessionGeneration) {
  ASSERT_RESULT_EQ(8, push(deterministicBytes(8, 1)));
  storeLe64(&submissionEntry(0).sessionGeneration, config_.sessionGeneration + 1);
  std::array<std::byte, 8> output{};
  ASSERT_ERROR(acceptor_->tryPop(Direction::Submission, output), StatusCode::kDataCorruption);
}

TEST_F(MappedCxlLaneTest, RejectsDuplicateOrReorderedAbsoluteSequence) {
  ASSERT_RESULT_EQ(8, push(deterministicBytes(8, 1)));
  storeLe64(&submissionEntry(0).absoluteSequence, 1);
  recomputeSubmissionCrc(0);
  std::array<std::byte, 8> output{};
  ASSERT_ERROR(acceptor_->tryPop(Direction::Submission, output), StatusCode::kDataCorruption);
}

TEST_F(MappedCxlLaneTest, RejectsBadPayloadCrcBeforeReturningFirstByte) {
  ASSERT_RESULT_EQ(8, push(deterministicBytes(8, 2)));
  submissionPayload(0, 8)[3] ^= std::byte{1};
  std::array<std::byte, 1> output{std::byte{0xA5}};
  ASSERT_ERROR(acceptor_->tryPop(Direction::Submission, output), StatusCode::kDataCorruption);
  EXPECT_EQ(output[0], std::byte{0xA5});
}

TEST_F(MappedCxlLaneTest, RejectsNonzeroReservedField) {
  ASSERT_RESULT_EQ(8, push(deterministicBytes(8, 3)));
  storeLe32(&submissionEntry(0).reserved0, 1);
  recomputeSubmissionCrc(0);
  std::array<std::byte, 8> output{};
  ASSERT_ERROR(acceptor_->tryPop(Direction::Submission, output), StatusCode::kDataCorruption);
}

TEST_F(MappedCxlLaneTest, RejectsGappedStreamOffset) {
  ASSERT_RESULT_EQ(8, push(deterministicBytes(8, 4)));
  std::array<std::byte, 8> output{};
  ASSERT_RESULT_EQ(8, acceptor_->tryPop(Direction::Submission, output));
  ASSERT_RESULT_EQ(8, push(deterministicBytes(8, 5)));
  storeLe64(&submissionEntry(1).streamOffset, 9);
  recomputeSubmissionCrc(1);
  ASSERT_ERROR(acceptor_->tryPop(Direction::Submission, output), StatusCode::kDataCorruption);
}

TEST_F(MappedCxlLaneTest, RejectsInvalidLocalFlagCombinationWithoutPublication) {
  auto payload = deterministicBytes(1, 6);
  ASSERT_ERROR(requester_->tryPush(Direction::Submission,
                                   FrameView{payload,
                                             static_cast<CxlFrameFlags>(static_cast<uint32_t>(CxlFrameFlags::Data) |
                                                                        static_cast<uint32_t>(CxlFrameFlags::Error))}),
               StatusCode::kInvalidArg);
  EXPECT_EQ(requester_->producerCursor(Direction::Submission), 0);
  EXPECT_FALSE(requester_->isRetired());
}

TEST_F(MappedCxlLaneTest, RejectsWrongImmutableCellOffset) {
  ASSERT_RESULT_EQ(8, push(deterministicBytes(8, 7)));
  submissionEntry(0).payloadOffset += config_.cellBytes;
  recomputeSubmissionCrc(0);
  std::array<std::byte, 8> output{};
  ASSERT_ERROR(acceptor_->tryPop(Direction::Submission, output), StatusCode::kDataCorruption);
}

TEST_F(MappedCxlLaneTest, RejectsCorruptAbsoluteCursorRelationship) {
  setCursor(config_.directions[0].producerCursorOffset, 0);
  setCursor(config_.directions[0].consumerCursorOffset, 1);
  ASSERT_ERROR(acceptor_->readable(Direction::Submission), StatusCode::kDataCorruption);
  EXPECT_TRUE(acceptor_->isRetired());
}

TEST_F(MappedCxlLaneTest, RetiresBeforeAbsoluteCursorWrap) {
  const uint64_t cutoff = std::numeric_limits<uint64_t>::max() - config_.depth;
  setCursor(config_.directions[0].producerCursorOffset, cutoff);
  setCursor(config_.directions[0].consumerCursorOffset, cutoff);
  ASSERT_ERROR(push(deterministicBytes(1, 8)), RPCCode::kStaleGeneration);
  EXPECT_TRUE(requester_->isRetired());
  EXPECT_EQ(requester_->producerCursor(Direction::Submission), cutoff);
}

TEST_F(MappedCxlLaneTest, RejectsStreamOffsetOverflow) {
  requester_->directions_[0].nextPushStreamOffset = std::numeric_limits<uint64_t>::max();
  ASSERT_ERROR(push(deterministicBytes(1, 9)), RPCCode::kStaleGeneration);
  EXPECT_TRUE(requester_->isRetired());
}

TEST_F(MappedCxlLaneTest, RejectsUntrustworthyDeliveredRecord) {
  auto offset = config_.directions[0].deliveredRecordOffset;
  auto record = acceptorRegion_->checkedRange(offset, sizeof(CxlDeliveredRecord), alignof(CxlDeliveredRecord));
  ASSERT_OK(record);
  auto *delivered = reinterpret_cast<CxlDeliveredRecord *>(record->data());
  delivered->deliveredOffsetComplement ^= 1U;
  ASSERT_ERROR(requester_->peerDeliveredOffset(Direction::Submission), StatusCode::kDataCorruption);
  EXPECT_TRUE(requester_->isRetired());
}

TEST(TestCxlLane, CursorAtomicIsAlwaysLockFree) { static_assert(std::atomic_ref<uint64_t>::is_always_lock_free); }

}  // namespace hf3fs::net::cxl
