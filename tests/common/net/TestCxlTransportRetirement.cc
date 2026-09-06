#include <filesystem>
#include <fstream>
#include <sstream>
#include <sys/eventfd.h>
#include <unistd.h>

#include "common/net/IOWorker.h"
#include "common/net/cxl/CxlSocket.h"
#include "tests/GtestHelpers.h"

namespace hf3fs::net::test {
namespace {

class RetiringTransport : public Transport {
 public:
  RetiringTransport(std::unique_ptr<Socket> socket, IOWorker &worker)
      : Transport(std::move(socket), worker, Address{}, ServicePlane::Data) {}
  void retire() { (void)retirePublication(); }
};

bool epollContains(int targetFd) {
  for (const auto &entry : std::filesystem::directory_iterator("/proc/self/fdinfo")) {
    std::ifstream file(entry.path());
    std::string line;
    while (std::getline(file, line)) {
      if (line.starts_with("tfd:")) {
        std::istringstream fields(line.substr(4));
        int fd = -1;
        fields >> fd;
        if (fd == targetFd) return true;
      }
    }
  }
  return false;
}

TEST(TestCxlTransportRetirement, RemovesKernelInterestBeforeClosingSocket) {
  namespace cxl = net::cxl;
  folly::test::TemporaryFile file;
  ASSERT_EQ(::ftruncate(file.fd(), 4_MB), 0);
  auto region = cxl::CxlRegion::mapFile(file.path().string(), 4_MB);
  ASSERT_OK(region);
  cxl::CxlLayoutManifest manifest{
      .totalRegionBytes = 4_MB,
      .sessionGeneration = 1,
      .endpointCount = 2,
      .laneCount = 1,
      .authorityEndpoint = cxl::EndpointId{1},
      .authorityGeneration = 1,
  };
  const std::array<uint64_t, cxl::kCxlRangeCount> lengths{4_KB, 4_KB, 16_KB, 4_KB, 1_MB, 4_KB, 128_KB, 4_KB};
  uint64_t offset = cxl::kCxlSuperblockBytes;
  for (size_t i = 0; i < lengths.size(); ++i) {
    manifest.ranges[i] = {static_cast<cxl::CxlRangeKind>(i + 1), offset, lengths[i]};
    offset += lengths[i];
  }
  manifest.lifecycleRecordOffset = manifest.ranges.back().offset;
  auto layout = cxl::CxlLayout::initializeAsAuthority(*region, manifest, cxl::EndpointId{1});
  ASSERT_OK(layout);
  cxl::CxlLaneConfig laneConfig{.sessionGeneration = 1, .laneGeneration = 1, .depth = 8, .cellBytes = 65536};
  for (size_t i = 0; i < 2; ++i) {
    laneConfig.directions[i] = {
        .producerCursorOffset = manifest.ranges[2].offset + i * 8_KB,
        .consumerCursorOffset = manifest.ranges[2].offset + i * 8_KB + 4_KB,
        .deliveredRecordOffset = manifest.ranges[2].offset + i * 8_KB + 4_KB + 64,
        .frameRingOffset = manifest.ranges[3].offset + i * 8 * sizeof(cxl::CxlFrameEntry),
        .payloadCellsOffset = manifest.ranges[4].offset + i * 512_KB,
    };
  }
  auto lane = cxl::CxlLane::create(*region, *layout, laneConfig, cxl::CxlLaneRole::Requester);
  ASSERT_OK(lane);
  auto peerLane = cxl::CxlLane::create(*region, *layout, laneConfig, cxl::CxlLaneRole::Acceptor);
  ASSERT_OK(peerLane);
  auto engine = std::make_shared<cxl::CxlProgressEngine>();
  ASSERT_OK(engine->start());
  auto socket = cxl::CxlSocket::createUnique(std::move(*lane), Address::fromString("CXL://127.0.0.1:9001"), engine);
  ASSERT_OK(socket);
  ASSERT_TRUE((*socket)->publicationSnapshot()->trustworthy);
  const int socketFd = (*socket)->fd();
  // Keep the open file description alive so close() cannot implicitly remove
  // the epoll registration and conceal a missing explicit deregistration.
  FdWrapper observer(::dup(socketFd));
  ASSERT_TRUE(observer.valid());
  CPUExecutorGroup executor(1, "RetireTest");
  folly::IOThreadPoolExecutor connector(1);
  serde::Services services;
  Processor::Config processorConfig;
  Processor processor(services, executor, processorConfig);
  IOWorker::Config workerConfig;
  IOWorker worker(processor, executor, connector, workerConfig);
  auto transport = std::make_shared<RetiringTransport>(std::move(*socket), worker);
  auto eventLoop = EventLoop::create();
  ASSERT_OK(eventLoop->start());
  eventLoop->stopAndJoin();  // Keep the epoll descriptor; no callbacks race this resource check.
  ASSERT_OK(eventLoop->add(transport, EPOLLIN | EPOLLET));
  ASSERT_TRUE(epollContains(socketFd));
  transport->retire();
  EXPECT_FALSE(epollContains(socketFd));
  transport.reset();
  engine->stopAndJoin();
}

}  // namespace
}  // namespace hf3fs::net::test
