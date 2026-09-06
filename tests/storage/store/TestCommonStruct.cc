#include "common/net/cxl/CxlAbi.h"
#include "common/serde/Serde.h"
#include "common/utils/Reflection.h"
#include "fbs/storage/Common.h"
#include "fbs/storage/Service.h"
#include "tests/GtestHelpers.h"

namespace hf3fs::storage::test {
namespace {

TEST(TestRemoteBufferHandleSerde, CxlRoundTrip) {
  net::RemoteBufferHandle expected{};
  expected.abiVersion = net::cxl::kCxlAbiVersion;
  expected.transportKind = static_cast<uint8_t>(net::TransportKind::CXL);
  expected.permissions = net::remoteAccessBits(net::RemoteAccess::Read);
  expected.ownerEndpoint = 7;
  expected.arenaId = 7;
  expected.allocationSlot = 11;
  expected.sessionGeneration = 13;
  expected.ownerGeneration = 17;
  expected.allocationGeneration = 19;
  expected.offset = 1_MB;
  expected.length = 4096;
  net::sealRemoteBufferHandle(expected);

  auto encoded = serde::serialize(expected);
  net::RemoteBufferHandle actual;
  ASSERT_OK(serde::deserialize(actual, encoded));
  EXPECT_EQ(actual, expected);
  ASSERT_OK(net::validateRemoteBufferHandle(actual));

  auto readable = net::RemoteBufferHandle::serdeFromReadable(expected.serdeToReadable());
  ASSERT_OK(readable);
  EXPECT_EQ(*readable, expected);
}

TEST(TestCommonStruct, Normal) {
  {
    ChunkId ser(0xface, 0xbeef);
    auto out = serde::serialize(ser);
    ASSERT_EQ(out.size(), 1 + 8 + 8);

    ChunkId des;
    ASSERT_OK(serde::deserialize(des, out));
    ASSERT_EQ(des, ser);

    auto str = ser.describe();
    auto result = ChunkId::fromString(str);
    ASSERT_OK(result);
    ASSERT_EQ(*result, ser);

    auto next = ChunkId{0xface, 0xbef0};
    ASSERT_EQ(ser.nextChunkId(), next);
  }

  {
    auto id = ChunkId{0x0000, 0x007f};
    auto next = ChunkId{0x0000, 0x0080};
    ASSERT_EQ(id.nextChunkId(), next);
  }
  {
    auto id = ChunkId{0x00ff, 0xffffffffffffffffull};
    auto next = ChunkId{0x0100, 0x0000000000000000ull};
    ASSERT_EQ(id.nextChunkId(), next);
  }

  {
    std::string str = "00000000-00000346-85270000-0000000B";
    auto chunk = ChunkId::fromString(str);
    ASSERT_EQ(chunk->describe(), str);
  }

  {
    ChecksumInfo ser;
    ser.type = ChecksumType::CRC32;
    ser.value = 0xff;
    auto out = serde::serialize(ser);
    ASSERT_EQ(out.size(), 1 + 1 + 4);

    ChecksumInfo des;
    ASSERT_OK(serde::deserialize(des, out));
    ASSERT_EQ(des, ser);
  }

  {
    BatchReadReq req;
    req.payloads.emplace_back();
    req.payloads.back().key.chunkId = ChunkId(1, 1);
    req.payloads.back().remoteBuf.ownerEndpoint = 7;
    auto out = serde::serialize(req);
    XLOGF(INFO, "single read req size: {}, json: {}", out.length(), req);

    auto front = req.payloads.front();
    req.payloads.resize(16, front);
    out = serde::serialize(req);
    XLOGF(INFO, "10 read req size: {}", out.length());
  }

  refl::Helper::iterate<StorageSerde<>>([](auto type) {
    using T = decltype(type);
    XLOGF(INFO,
          "method: {}, ID: {}\n  Req: {}\n  Rsp: {}",
          T::name,
          T::id,
          serde::toJsonString(typename T::ReqType{}),
          serde::toJsonString(typename T::RspType{}));
  });
}

}  // namespace
}  // namespace hf3fs::storage::test
