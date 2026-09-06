#include <gtest/gtest.h>
#include <string>
#include <string_view>

#include "common/net/cxl/CxlAbi.h"

namespace hf3fs::net::cxl::test {

TEST(TestCxlAbi, FixedSizes) {
  static_assert(sizeof(CxlSuperblockHeader) == 128);
  static_assert(sizeof(CxlRangeEntry) == 24);
  static_assert(sizeof(CxlSuperblock) == 4096);
  static_assert(alignof(CxlSuperblock) == 4096);
  static_assert(sizeof(CxlFrameEntry) == 64);
  static_assert(alignof(CxlFrameEntry) == 64);
  static_assert(sizeof(CxlFabricLifecycleRecord) == 64);
  static_assert(alignof(CxlFabricLifecycleRecord) == 64);
  static_assert(sizeof(CxlEndpointRecord) == 64);
  static_assert(alignof(CxlEndpointRecord) == 64);
  static_assert(sizeof(CxlLaneOwnerRecord) == 64);
  static_assert(alignof(CxlLaneOwnerRecord) == 64);
  static_assert(sizeof(CxlDeliveredRecord) == 64);
  static_assert(alignof(CxlDeliveredRecord) == 64);
  static_assert(sizeof(CxlAllocationRecord) == 64);
  static_assert(alignof(CxlAllocationRecord) == 64);
  EXPECT_EQ(kCxlAbiVersion, 1);
}

TEST(TestCxlAbi, LittleEndianHelpersHaveFixedEncoding) {
  std::array<std::byte, 8> bytes{};
  storeLe64(bytes.data(), 0x0807060504030201ULL);
  EXPECT_EQ(bytes,
            (std::array<std::byte, 8>{std::byte{0x01},
                                      std::byte{0x02},
                                      std::byte{0x03},
                                      std::byte{0x04},
                                      std::byte{0x05},
                                      std::byte{0x06},
                                      std::byte{0x07},
                                      std::byte{0x08}}));
  EXPECT_EQ(loadLe64(bytes.data()), 0x0807060504030201ULL);
}

TEST(TestCxlAbi, CastagnoliStandardVectorAndCorruption) {
  constexpr std::string_view input = "123456789";
  auto bytes = std::span(reinterpret_cast<const std::byte *>(input.data()), input.size());
  EXPECT_EQ(cxlCrc32c(bytes), 0xE3069283U);

  CxlFrameEntry frame{};
  storeLe64(&frame.absoluteSequence, 17);
  storeLe64(&frame.sessionGeneration, 9);
  storeLe64(&frame.laneGeneration, 3);
  storeLe32(&frame.payloadLength, input.size());
  storeLe32(&frame.flags, static_cast<uint32_t>(CxlFrameFlags::Data));
  const auto checksum = cxlCrc32cWithZeroedU32(frame, offsetof(CxlFrameEntry, crc32c), bytes);
  storeLe32(&frame.crc32c, checksum);
  EXPECT_EQ(cxlCrc32cWithZeroedU32(frame, offsetof(CxlFrameEntry, crc32c), bytes), checksum);

  frame.flags ^= 1U;
  EXPECT_NE(cxlCrc32cWithZeroedU32(frame, offsetof(CxlFrameEntry, crc32c), bytes), checksum);
  frame.flags ^= 1U;
  auto corrupted = std::string(input);
  corrupted[4] ^= 1;
  auto corruptedBytes = std::span(reinterpret_cast<const std::byte *>(corrupted.data()), corrupted.size());
  EXPECT_NE(cxlCrc32cWithZeroedU32(frame, offsetof(CxlFrameEntry, crc32c), corruptedBytes), checksum);
}

}  // namespace hf3fs::net::cxl::test
