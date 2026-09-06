#include <algorithm>
#include <array>
#include <cstdlib>
#include <gtest/gtest.h>
#include <limits>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unistd.h>
#include <vector>

#include "common/net/cxl/CxlLayout.h"
#include "common/utils/Size.h"
#include "tests/GtestHelpers.h"

namespace hf3fs::net::cxl::test {
namespace {

constexpr uint64_t kRegionBytes = 1_MB;

class TemporaryFile {
 public:
  TemporaryFile() {
    std::array<char, 32> name{};
    constexpr std::string_view pattern = "/tmp/hf3fs-cxl-XXXXXX";
    std::copy(pattern.begin(), pattern.end(), name.begin());
    fd_ = ::mkstemp(name.data());
    if (fd_ < 0) {
      throw std::runtime_error("mkstemp failed");
    }
    path_ = name.data();
  }

  ~TemporaryFile() {
    if (fd_ >= 0) {
      ::close(fd_);
    }
    if (!path_.empty()) {
      ::unlink(path_.c_str());
    }
  }

  TemporaryFile(const TemporaryFile &) = delete;
  TemporaryFile &operator=(const TemporaryFile &) = delete;

  int fd() const noexcept { return fd_; }
  const std::string &path() const noexcept { return path_; }

 private:
  int fd_{-1};
  std::string path_;
};

class MappedFileTest : public ::testing::Test {
 protected:
  void SetUp() override { ASSERT_EQ(::ftruncate(file_.fd(), kRegionBytes), 0); }

  Result<CxlRegion> map() { return CxlRegion::mapFile(file_.path(), kRegionBytes); }

  CxlLayoutManifest manifest(uint64_t generation = 9, EndpointId authority = EndpointId{1}) const {
    CxlLayoutManifest value{
        .totalRegionBytes = kRegionBytes,
        .sessionGeneration = generation,
        .lifecycleRecordOffset = 0,
        .endpointCount = 2,
        .laneCount = 2,
        .authorityEndpoint = authority,
        .authorityGeneration = 7,
    };
    for (size_t index = 0; index < value.manifestSha256.size(); ++index) {
      value.manifestSha256[index] = static_cast<std::byte>(index + 1U);
    }

    constexpr std::array<uint64_t, kCxlRangeCount> lengths{
        4_KB,
        4_KB,
        8_KB,
        8_KB,
        64_KB,
        4_KB,
        128_KB,
        4_KB,
    };
    uint64_t offset = kCxlSuperblockBytes;
    for (size_t index = 0; index < value.ranges.size(); ++index) {
      value.ranges[index] = CxlRange{static_cast<CxlRangeKind>(index + 1U), offset, lengths[index]};
      offset += lengths[index];
    }
    value.lifecycleRecordOffset = value.ranges.back().offset;
    return value;
  }

  LayoutExpectation expectation(const CxlLayoutManifest &manifest) const {
    return LayoutExpectation{
        .sessionGeneration = manifest.sessionGeneration,
        .manifestSha256 = manifest.manifestSha256,
        .totalRegionBytes = manifest.totalRegionBytes,
        .endpointCount = manifest.endpointCount,
        .laneCount = manifest.laneCount,
        .authorityEndpoint = manifest.authorityEndpoint,
    };
  }

  TemporaryFile file_;
};

TEST_F(MappedFileTest, RejectsOverflowingAndMisalignedRanges) {
  auto mapped = map();
  ASSERT_OK(mapped);
  auto overflow = mapped->checkedRange(std::numeric_limits<uint64_t>::max() - 4U, 16);
  ASSERT_ERROR(overflow, StatusCode::kInvalidArg);
  ASSERT_ERROR(mapped->checkedRange(kRegionBytes - 16, 32), StatusCode::kInvalidArg);
  ASSERT_ERROR(mapped->checkedRange(1, 64, 64), StatusCode::kInvalidArg);
  ASSERT_ERROR(mapped->checkedRange(0, 64, 3), StatusCode::kInvalidArg);
}

TEST_F(MappedFileTest, MoveAndSeparateMappingsPreserveSharedBytes) {
  auto firstResult = map();
  auto secondResult = map();
  ASSERT_OK(firstResult);
  ASSERT_OK(secondResult);
  CxlRegion first = std::move(*firstResult);
  CxlRegion second = std::move(*secondResult);

  first.bytes()[1234] = std::byte{0x5A};
  EXPECT_EQ(second.bytes()[1234], std::byte{0x5A});
  CxlRegion moved = std::move(first);
  EXPECT_FALSE(first);
  EXPECT_EQ(first.fd(), -1);
  EXPECT_EQ(moved.bytes()[1234], std::byte{0x5A});
}

TEST_F(MappedFileTest, DaxMappingRejectsRegularFile) {
  ASSERT_ERROR(CxlRegion::mapDax(file_.path()), StatusCode::kInvalidArg);
}

TEST_F(MappedFileTest, LayoutInitializesAndAttachesThroughSeparateMappings) {
  auto authorityResult = map();
  auto peerResult = map();
  ASSERT_OK(authorityResult);
  ASSERT_OK(peerResult);
  auto authorityRegion = std::move(*authorityResult);
  auto peerRegion = std::move(*peerResult);
  auto layoutManifest = manifest();

  auto initialized = CxlLayout::initializeAsAuthority(authorityRegion, layoutManifest, EndpointId{1});
  ASSERT_OK(initialized);
  auto attached = CxlLayout::attach(peerRegion, expectation(layoutManifest), std::chrono::milliseconds(10));
  ASSERT_OK(attached);
  EXPECT_EQ(attached->sessionGeneration(), 9);
  EXPECT_EQ(attached->ranges(), layoutManifest.ranges);
  auto payload = attached->range(CxlRangeKind::RpcPayloadCells);
  ASSERT_OK(payload);
  EXPECT_EQ(payload->size(), 64_KB);
}

TEST_F(MappedFileTest, RejectsGenerationAndCrcMismatch) {
  auto regionResult = map();
  ASSERT_OK(regionResult);
  auto region = std::move(*regionResult);
  auto layoutManifest = manifest();
  ASSERT_OK(CxlLayout::initializeAsAuthority(region, layoutManifest, EndpointId{1}));

  auto wrongGeneration = expectation(layoutManifest);
  wrongGeneration.sessionGeneration = 10;
  ASSERT_ERROR(CxlLayout::loadAndValidate(region, wrongGeneration), RPCCode::kStaleGeneration);

  auto superblock = region.checkedRange(0, sizeof(CxlSuperblock), alignof(CxlSuperblock));
  ASSERT_OK(superblock);
  (*superblock)[200] ^= std::byte{1};
  ASSERT_ERROR(CxlLayout::loadAndValidate(region, expectation(layoutManifest)), StatusCode::kDataCorruption);
}

TEST_F(MappedFileTest, AttachNeverFormatsAndSecondAuthorityIsRejected) {
  auto regionResult = map();
  ASSERT_OK(regionResult);
  auto region = std::move(*regionResult);
  std::fill(region.bytes().begin(), region.bytes().end(), std::byte{0xA5});
  const auto before = std::vector<std::byte>(region.bytes().begin(), region.bytes().end());
  auto layoutManifest = manifest();

  ASSERT_ERROR(CxlLayout::attach(region, expectation(layoutManifest), std::chrono::milliseconds(2)),
               RPCCode::kDataPlaneHandshakeFailed);
  EXPECT_TRUE(std::equal(before.begin(), before.end(), region.bytes().begin()));

  ASSERT_OK(CxlLayout::initializeAsAuthority(region, layoutManifest, EndpointId{1}));
  auto competingManifest = manifest(9, EndpointId{2});
  ASSERT_ERROR(CxlLayout::initializeAsAuthority(region, competingManifest, EndpointId{2}), StatusCode::kInvalidArg);
}

TEST_F(MappedFileTest, RejectsOverlappingManifestBeforeWriting) {
  auto regionResult = map();
  ASSERT_OK(regionResult);
  auto region = std::move(*regionResult);
  auto invalid = manifest();
  invalid.ranges[2].offset = invalid.ranges[1].offset;
  ASSERT_ERROR(CxlLayout::initializeAsAuthority(region, invalid, EndpointId{1}), StatusCode::kInvalidArg);
  EXPECT_TRUE(std::all_of(region.bytes().begin(), region.bytes().begin() + kCxlSuperblockBytes, [](std::byte byte) {
    return byte == std::byte{0};
  }));
}

}  // namespace
}  // namespace hf3fs::net::cxl::test
