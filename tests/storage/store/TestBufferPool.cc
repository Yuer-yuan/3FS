#include <folly/executors/CPUThreadPoolExecutor.h>
#include <folly/experimental/coro/BlockingWait.h>
#include <folly/ScopeGuard.h>
#include <fstream>
#include <unistd.h>

#include "common/net/TransportRuntime.h"
#include "common/serde/Serde.h"
#include "common/utils/Reflection.h"
#include "fbs/storage/Common.h"
#include "fbs/storage/Service.h"
#include "storage/aio/AioReadWorker.h"
#include "storage/service/BufferPool.h"
#include "tests/GtestHelpers.h"

namespace hf3fs::storage::test {
namespace {

TEST(TestBufferPool, Normal) {
  CPUExecutorGroup executor(8, "");

  BufferPool::Config bufferPoolConfig;
  bufferPoolConfig.set_buffer_count(256);
  bufferPoolConfig.set_big_buffer_count(4);
  AioReadWorker::Config aioReadWorkerConfig;
  aioReadWorkerConfig.set_num_threads(8);
  for (auto i = 0; i < 8; ++i) {
    BufferPool pool(bufferPoolConfig);
    ASSERT_OK(pool.init(executor));

    AioReadWorker worker(aioReadWorkerConfig);
    ASSERT_OK(worker.start({}, pool.iovecs()));
    ASSERT_OK(worker.stopAndJoin());

    pool.clear(executor);
  }
}

TEST(TestBufferPool, BigBuffer) {
  CPUExecutorGroup executor(8, "");

  BufferPool::Config bufferPoolConfig;
  bufferPoolConfig.set_buffer_count(256);
  bufferPoolConfig.set_buffer_size(4_MB);
  bufferPoolConfig.set_big_buffer_count(4);
  bufferPoolConfig.set_big_buffer_size(64_MB);

  BufferPool pool(bufferPoolConfig);
  ASSERT_OK(pool.init(executor));

  auto guard = pool.get();
  auto result = folly::coro::blockingWait(guard.allocate(64_MB));
  ASSERT_OK(result);
  ASSERT_EQ(result->size(), 64_MB);

  pool.clear(executor);
}

TEST(TestBufferPool, DiskBuffersStayOutsideActiveCxlMapping) {
  using namespace net::cxl;
  folly::test::TemporaryDirectory directory;
  const auto region = (directory.path() / "region").string();
  const auto owner = (directory.path() / "owner").string();
  std::ofstream(region).close();
  std::ofstream(owner) << "disk-buffer-test";
  ASSERT_EQ(::truncate(region.c_str(), 1_MB), 0);
  CxlLayoutManifest manifest{
      .totalRegionBytes = 1_MB,
      .sessionGeneration = 1,
      .endpointCount = 1,
      .laneCount = 1,
      .authorityEndpoint = EndpointId{1},
      .authorityGeneration = 1,
  };
  manifest.manifestSha256.fill(std::byte{1});
  const std::array<uint64_t, kCxlRangeCount> lengths{4_KB, 4_KB, 16_KB, 8_KB, 64_KB, 4_KB, 128_KB, 4_KB};
  uint64_t offset = kCxlSuperblockBytes;
  for (size_t index = 0; index < lengths.size(); ++index) {
    manifest.ranges[index] = {static_cast<CxlRangeKind>(index + 1), offset, lengths[index]};
    offset += lengths[index];
  }
  manifest.lifecycleRecordOffset = manifest.ranges.back().offset;
  CxlFabric::Config fabricConfig;
  fabricConfig.mode = CxlFabric::StartMode::InitializeAuthority;
  fabricConfig.regionType = CxlFabric::RegionType::File;
  fabricConfig.regionPath = region;
  fabricConfig.regionLength = 1_MB;
  fabricConfig.manifest = manifest;
  fabricConfig.endpoint = EndpointId{1};
  fabricConfig.endpointGeneration = 1;
  fabricConfig.authorityOwnerLock = owner;
  fabricConfig.authorityReceipt = "disk-buffer-test";
  ASSERT_OK(net::TransportRuntime::startCxl(fabricConfig));
  auto cleanup = folly::makeGuard([] { EXPECT_TRUE(net::TransportRuntime::stopCxl()); });
  auto fabric = net::TransportRuntime::cxlFabric();
  const auto begin = reinterpret_cast<uintptr_t>(fabric->region().bytes().data());
  const auto end = begin + fabric->region().size();
  CPUExecutorGroup executor(2, "disk-buffer-test");
  BufferPool::Config config;
  config.set_buffer_count(1);
  config.set_big_buffer_count(1);
  config.set_buffer_size(64_KB);
  config.set_big_buffer_size(64_KB);
  BufferPool pool(config);
  ASSERT_OK(pool.init(executor));
  for (const auto &iov : pool.iovecs()) {
    const auto address = reinterpret_cast<uintptr_t>(iov.iov_base);
    EXPECT_TRUE(address < begin || address >= end);
    EXPECT_EQ(address % kAIOAlignSize, 0);
  }
  auto buffers = pool.get();
  auto buffer = buffers.tryAllocate(4_KB);
  ASSERT_OK(buffer);
  ASSERT_ERROR(buffer->exportRemote(net::RemoteAccess::Read), RPCCode::kTransportCapabilityMissing);
}

}  // namespace
}  // namespace hf3fs::storage::test
#include <array>
