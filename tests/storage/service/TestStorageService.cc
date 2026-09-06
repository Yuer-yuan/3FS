#include <algorithm>
#include <chrono>
#include <thread>

#include "client/storage/StorageClient.h"
#include "common/kv/mem/MemKVEngine.h"
#include "common/net/Client.h"
#include "common/net/TransportRuntime.h"
#include "common/serde/Serde.h"
#include "fbs/mgmtd/RoutingInfo.h"
#include "fbs/storage/Service.h"
#include "storage/service/StorageOperator.h"
#include "storage/service/StorageServer.h"
#include "storage/store/ChunkMetadata.h"
#include "tests/GtestHelpers.h"
#include "tests/client/ClientWithConfig.h"
#include "tests/client/ServerWithConfig.h"
#include "tests/lib/Helper.h"
#include "tests/lib/UnitTestFabric.h"
#include "tests/mgmtd/MgmtdTestHelper.h"

namespace hf3fs::storage {
namespace {

using namespace hf3fs::test;
class TestStorageForward : public UnitTestFabric, public ::testing::Test {
 protected:
  TestStorageForward()
      : UnitTestFabric(SystemSetupConfig{
            128_KB /*chunkSize*/,
            1 /*numChains*/,
            3 /*numReplicas*/,
            3 /*numStorageNodes*/,
            {folly::fs::temp_directory_path()} /*dataPaths*/,
            hf3fs::Path() /*clientConfig*/,
            hf3fs::Path() /*serverConfig*/,
            {} /*storageEndpoints*/,
            0 /*serviceLevel*/,
            0 /*listenPort*/,
            client::StorageClient::ImplementationType::RPC /*clientImplType*/,
            kv::KVStore::Type::RocksDB /*metaStoreType*/,
            true /*useFakeMgmtdClient*/,
        }) {}

  void SetUp() override {
#if HF3FS_ENABLE_RDMA
    net::IBDevice::Config ibConfig;
    auto ibResult = net::IBManager::start(ibConfig);
    ASSERT_OK(ibResult);
#endif
    ASSERT_TRUE(setUpStorageSystem());
  }

  void TearDown() override { tearDownStorageSystem(); }
};

TEST_F(TestStorageForward, Write) {
  // register a block of memory
  std::vector<uint8_t> memoryBlock(setupConfig_.chunk_size(), 0xFF);
  auto regRes = storageClient_->registerIOBuffer(&memoryBlock[0], memoryBlock.size());
  ASSERT_OK(regRes);

  // create write IO

  auto ioBuffer = std::move(*regRes);
  auto chainId = firstChainId_;
  ChunkId chunkId(1 /*high*/, 1 /*low*/);
  auto writeIO = storageClient_->createWriteIO(chainId,
                                               chunkId,
                                               0 /*offset*/,
                                               setupConfig_.chunk_size() /*length*/,
                                               setupConfig_.chunk_size() /*chunkSize*/,
                                               &memoryBlock[0],
                                               &ioBuffer);

  client::WriteOptions options;

  storageServers_.back()->stopAndJoin();
  storageServers_.pop_back();

  ASSERT_TRUE(updateRoutingInfo([&](auto &routingInfo) {
    setTargetOffline(routingInfo, 0);
    setTargetOffline(routingInfo, 1);
  }));

  folly::coro::blockingWait(storageClient_->write(writeIO, flat::UserInfo(), options));
  ASSERT_OK(writeIO.result.lengthInfo);
  ASSERT_EQ(writeIO.length, writeIO.result.lengthInfo.value());
}

TEST_F(TestStorageForward, WriteFailed) {
  // register a block of memory
  std::vector<uint8_t> memoryBlock(setupConfig_.chunk_size(), 0xFF);
  auto regRes = storageClient_->registerIOBuffer(&memoryBlock[0], memoryBlock.size());
  ASSERT_OK(regRes);

  // create write IO

  auto ioBuffer = std::move(*regRes);
  auto chainId = firstChainId_;
  ChunkId chunkId(1 /*high*/, 1 /*low*/);
  auto writeIO = storageClient_->createWriteIO(chainId,
                                               chunkId,
                                               0 /*offset*/,
                                               setupConfig_.chunk_size() /*length*/,
                                               setupConfig_.chunk_size() /*chunkSize*/,
                                               &memoryBlock[0],
                                               &ioBuffer);

  client::WriteOptions options;

  storageServers_.back()->stopAndJoin();
  storageServers_.pop_back();

  clientConfig_.retry().set_max_retry_time(10_s);
  folly::coro::blockingWait(storageClient_->write(writeIO, flat::UserInfo(), options));
  ASSERT_FALSE(writeIO.result.lengthInfo);
}

TEST_F(TestStorageForward, WriteAndRead) {
  // register a block of memory
  std::vector<uint8_t> memoryBlock(setupConfig_.chunk_size(), 0xFF);
  memoryBlock[0] = 0x01;
  memoryBlock[1] = 0x02;
  memoryBlock[2] = 0x03;
  auto regRes = storageClient_->registerIOBuffer(&memoryBlock[0], memoryBlock.size());
  ASSERT_OK(regRes);

  // create write IO

  auto ioBuffer = std::move(*regRes);
  auto chainId = firstChainId_;
  ChunkId chunkId(1 /*high*/, 1 /*low*/);
  auto writeIO = storageClient_->createWriteIO(chainId,
                                               chunkId,
                                               0 /*offset*/,
                                               3,
                                               setupConfig_.chunk_size() /*chunkSize*/,
                                               &memoryBlock[0],
                                               &ioBuffer);

  client::WriteOptions options;
  folly::coro::blockingWait(storageClient_->write(writeIO, flat::UserInfo(), options));
  ASSERT_OK(writeIO.result.lengthInfo);
  ASSERT_EQ(writeIO.length, writeIO.result.lengthInfo.value());

  {
    auto readIO = storageClient_->createReadIO(chainId, chunkId, 0, 4096, &memoryBlock[0], &ioBuffer);
    folly::coro::blockingWait(storageClient_->read(readIO, flat::UserInfo(), client::ReadOptions{}));
    ASSERT_OK(readIO.result.lengthInfo);
    ASSERT_EQ(*readIO.result.lengthInfo, 3);
    ASSERT_EQ(memoryBlock[0], 0x01);
    ASSERT_EQ(memoryBlock[1], 0x02);
    ASSERT_EQ(memoryBlock[2], 0x03);
  }

  {
    auto readIO = storageClient_->createReadIO(chainId, chunkId, 1, 4096, &memoryBlock[0], &ioBuffer);
    folly::coro::blockingWait(storageClient_->read(readIO, flat::UserInfo(), client::ReadOptions{}));
    ASSERT_OK(readIO.result.lengthInfo);
    ASSERT_EQ(*readIO.result.lengthInfo, 2);
    ASSERT_EQ(memoryBlock[0], 0x02);
    ASSERT_EQ(memoryBlock[1], 0x03);
  }
}

class TestStorageTail : public TestStorageForward {
 protected:
  TestStorageTail() {
    setupConfig_.set_num_replicas(1);
    setupConfig_.set_num_storage_nodes(1);
    setupConfig_.set_chunk_size(512_KB);
  }
};

TEST_F(TestStorageTail, BulkWriteDoesNotAllocateForwardingCopy) {
  auto fabric = net::TransportRuntime::cxlFabric();
  if (!fabric) GTEST_SKIP() << "requires the real CXL storage fixture";
  using namespace net::cxl;
  // The fixture assigns its first storage process endpoint 3. Observe that
  // owner's actual allocation generations across a real request, including
  // allocations that were subsequently released.
  auto position = fabric->participantPosition(EndpointId{3});
  ASSERT_OK(position);
  auto directory = fabric->layout().range(CxlRangeKind::AllocationDirectory, alignof(CxlAllocationRecord));
  ASSERT_OK(directory);
  const size_t slots = directory->size() / sizeof(CxlAllocationRecord) / position->count;
  const size_t offset = position->index * slots * sizeof(CxlAllocationRecord);
  auto generations = [&]() -> Result<std::vector<uint64_t>> {
    std::vector<uint64_t> result;
    for (size_t slot = 0; slot < slots; ++slot) {
      auto bytes = directory->subspan(offset + slot * sizeof(CxlAllocationRecord), sizeof(CxlAllocationRecord));
      if (std::atomic_ref<uint64_t>(*reinterpret_cast<uint64_t *>(bytes.data())).load(std::memory_order_acquire) == 0) {
        result.push_back(0);  // never-published allocation slot
        continue;
      }
      auto record = loadCxlOwnerRecord<CxlAllocationRecord>(bytes);
      if (!record) return makeError(StatusCode::kDataCorruption);
      result.push_back(loadLe64(&record->allocationGeneration));
    }
    return result;
  };
  auto before = generations();
  ASSERT_OK(before);

  std::vector<uint8_t> bytes(512_KB, 0x6d);
  bytes.front() = 1;
  bytes.back() = 2;
  const auto expected = bytes;
  auto registration = storageClient_->registerIOBuffer(bytes.data(), bytes.size());
  ASSERT_OK(registration);
  auto write = storageClient_->createWriteIO(firstChainId_, ChunkId(1, 1), 0, bytes.size(),
                                            bytes.size(), bytes.data(), &*registration);
  folly::coro::blockingWait(storageClient_->write(write, flat::UserInfo(), client::WriteOptions{}));
  ASSERT_OK(write.result.lengthInfo);
  ASSERT_EQ(*write.result.lengthInfo, bytes.size());
  auto after = generations();
  ASSERT_OK(after);
  EXPECT_EQ(*before, *after) << "a chain tail must not allocate a CXL forwarding copy";

  std::fill(bytes.begin(), bytes.end(), 0);
  auto read = storageClient_->createReadIO(firstChainId_, ChunkId(1, 1), 0, bytes.size(),
                                          bytes.data(), &*registration);
  folly::coro::blockingWait(storageClient_->read(read, flat::UserInfo(), client::ReadOptions{}));
  ASSERT_OK(read.result.lengthInfo);
  ASSERT_EQ(*read.result.lengthInfo, bytes.size());
  EXPECT_EQ(bytes, expected);
}

}  // namespace
}  // namespace hf3fs::storage
