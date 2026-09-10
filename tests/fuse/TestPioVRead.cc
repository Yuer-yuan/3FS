#include <array>
#include <gtest/gtest.h>

#include "fuse/PioV.h"

namespace hf3fs::lib::agent::test {
namespace {

TEST(PioVRead, WholeMissingChunkIsAZeroFilledHole) {
  std::array<uint8_t, 8> data;
  data.fill(0xa5);
  Result<uint32_t> missing = makeError(StorageClientCode::kChunkNotFound);
  std::vector<ssize_t> results(1, 0);
  const std::array outcomes{ReadIoOutcome{0, data.data(), data.size(), &missing}};

  finishReadIoResults(results, outcomes, true);

  EXPECT_EQ(results[0], 8);
  EXPECT_EQ(data, (std::array<uint8_t, 8>{}));
}

TEST(PioVRead, ShortChunkInsideLogicalFileIsZeroFilled) {
  std::array<uint8_t, 8> data{1, 2, 3, 4, 0xa5, 0xa5, 0xa5, 0xa5};
  Result<uint32_t> shortRead = 4;
  std::vector<ssize_t> results(1, 0);
  const std::array outcomes{ReadIoOutcome{0, data.data(), data.size(), &shortRead}};

  finishReadIoResults(results, outcomes, true);

  EXPECT_EQ(results[0], 8);
  EXPECT_EQ(data, (std::array<uint8_t, 8>{1, 2, 3, 4, 0, 0, 0, 0}));
}

TEST(PioVRead, ReadLengthIsClippedAtLogicalEof) {
  EXPECT_EQ(logicalReadLength(100, 20, 16), 16);
  EXPECT_EQ(logicalReadLength(100, 92, 16), 8);
  EXPECT_EQ(logicalReadLength(100, 100, 16), 0);
  EXPECT_EQ(logicalReadLength(100, 101, 16), 0);
}

TEST(PioVRead, InteriorShortChunkIsAZeroFilledHole) {
  std::array<uint8_t, 8> data{1, 2, 0xa5, 0xa5, 5, 6, 7, 8};
  Result<uint32_t> shortRead = 2;
  Result<uint32_t> fullRead = 4;
  std::vector<ssize_t> results(1, 0);
  const std::array outcomes{ReadIoOutcome{0, data.data(), 4, &shortRead},
                            ReadIoOutcome{0, data.data() + 4, 4, &fullRead}};

  finishReadIoResults(results, outcomes, true);

  EXPECT_EQ(results[0], 8);
  EXPECT_EQ(data, (std::array<uint8_t, 8>{1, 2, 0, 0, 5, 6, 7, 8}));
}

TEST(PioVRead, ForbiddenHoleAndOtherErrorsRemainErrors) {
  std::array<uint8_t, 8> data{};
  Result<uint32_t> missing = makeError(StorageClientCode::kChunkNotFound);
  Result<uint32_t> checksum = makeError(StorageClientCode::kChecksumMismatch);
  std::vector<ssize_t> results(2, 0);
  const std::array outcomes{ReadIoOutcome{0, data.data(), 4, &missing},
                            ReadIoOutcome{1, data.data() + 4, 4, &checksum}};

  finishReadIoResults(results, outcomes, false);

  EXPECT_EQ(results[0], -static_cast<ssize_t>(ClientAgentCode::kHoleInIoOutcome));
  EXPECT_EQ(results[1], -static_cast<ssize_t>(StorageClientCode::kChecksumMismatch));
}

TEST(PioVRead, ForbiddenInteriorShortChunkIsAnError) {
  std::array<uint8_t, 4> data{};
  Result<uint32_t> shortRead = 2;
  std::vector<ssize_t> results(1, 0);
  const std::array outcomes{ReadIoOutcome{0, data.data(), 4, &shortRead}};

  finishReadIoResults(results, outcomes, false);

  EXPECT_EQ(results[0], -static_cast<ssize_t>(ClientAgentCode::kHoleInIoOutcome));
}

}  // namespace
}  // namespace hf3fs::lib::agent::test
