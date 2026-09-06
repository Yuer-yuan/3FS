#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <fcntl.h>
#include <folly/experimental/coro/BlockingWait.h>
#include <gtest/gtest.h>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <unistd.h>
#include <vector>

#include "common/net/cxl/CxlBuffer.h"
#include "common/net/TransportEvidence.h"
#include "common/net/cxl/CxlBulkTransfer.h"
#include "common/utils/Size.h"
#include "tests/GtestHelpers.h"

namespace hf3fs::net::cxl {

TEST(TestCxlBufferValue, RemoteHandleFitsCoroutineFrameAlignment) {
  EXPECT_EQ(sizeof(RemoteBufferHandle), 64);
  EXPECT_LE(alignof(RemoteBufferHandle), alignof(std::max_align_t));
  EXPECT_EQ(offsetof(RemoteBufferHandle, sessionGeneration), 16);
  EXPECT_EQ(offsetof(RemoteBufferHandle, offset), 40);
  EXPECT_EQ(offsetof(RemoteBufferHandle, checksum), 56);
}

namespace {

using namespace std::chrono_literals;

constexpr uint64_t kTestRegionBytes = 1_MB;
constexpr std::string_view kReceipt = "run-token=cxl-buffer-test\nmanifest=0123456789abcdef\n";

class TemporaryFile {
 public:
  explicit TemporaryFile(std::string_view prefix, std::string_view contents = {}) {
    std::array<char, 96> name{};
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
        throw std::runtime_error("write temporary file failed");
      }
      offset += static_cast<size_t>(written);
    }
  }

  ~TemporaryFile() {
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

class TestCxlBuffer : public ::testing::Test {
 protected:
  void SetUp() override {
    ASSERT_EQ(::ftruncate(regionFile_.fd(), kTestRegionBytes), 0);
    const std::vector activeEndpoints{EndpointId{1}, EndpointId{16}};
    auto authorityResult =
        CxlFabric::start(config(CxlFabric::StartMode::InitializeAuthority, EndpointId{1}), activeEndpoints);
    ASSERT_OK(authorityResult);
    authority_ = *authorityResult;
    auto peerResult = CxlFabric::start(config(CxlFabric::StartMode::Attach, EndpointId{16}), activeEndpoints);
    ASSERT_OK(peerResult);
    peer_ = *peerResult;
    auto arenaResult = CxlBufferArena::create(authority_);
    ASSERT_OK(arenaResult);
    arena_ = *arenaResult;
    transfer_ = std::make_unique<CxlBulkTransfer>(peer_);
  }

  void TearDown() override {
    transfer_.reset();
    arena_.reset();
    if (peer_ && peer_->running()) {
      EXPECT_TRUE(peer_->stopAndJoin());
    }
    if (authority_ && authority_->running()) {
      EXPECT_TRUE(authority_->stopAndJoin());
    }
  }

  CxlLayoutManifest manifest() const {
    CxlLayoutManifest value{
        .totalRegionBytes = kTestRegionBytes,
        .sessionGeneration = 71,
        .endpointCount = 16,
        .laneCount = 1,
        .authorityEndpoint = EndpointId{1},
        .authorityGeneration = 19,
    };
    for (size_t index = 0; index < value.manifestSha256.size(); ++index) {
      value.manifestSha256[index] = static_cast<std::byte>(0x30U + index);
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
    for (size_t index = 0; index < value.ranges.size(); ++index) {
      value.ranges[index] = CxlRange{static_cast<CxlRangeKind>(index + 1U), offset, lengths[index]};
      offset += lengths[index];
    }
    value.lifecycleRecordOffset = value.ranges.back().offset;
    return value;
  }

  CxlFabric::Config config(CxlFabric::StartMode mode, EndpointId endpoint) const {
    return CxlFabric::Config{
        .mode = mode,
        .regionType = CxlFabric::RegionType::File,
        .regionPath = regionFile_.path(),
        .regionLength = kTestRegionBytes,
        .manifest = manifest(),
        .endpoint = endpoint,
        .endpointGeneration = 1,
        .capabilityBits = 0x04,
        .attachTimeout = 50ms,
        .heartbeatInterval = heartbeatInterval(),
        .authorityStaleTimeout = heartbeatInterval() * 100,
        .shutdownTimeout = 100_ms,
        .authorityOwnerLock = mode == CxlFabric::StartMode::InitializeAuthority ? lockFile_.path() : "",
        .authorityReceipt = mode == CxlFabric::StartMode::InitializeAuthority ? std::string(kReceipt) : "",
    };
  }

  virtual Duration heartbeatInterval() const { return 1_ms; }

  TemporaryFile regionFile_{"hf3fs-cxl-buffer"};
  TemporaryFile lockFile_{"hf3fs-cxl-buffer-owner", kReceipt};
  std::shared_ptr<CxlFabric> authority_;
  std::shared_ptr<CxlFabric> peer_;
  std::shared_ptr<CxlBufferArena> arena_;
  std::unique_ptr<CxlBulkTransfer> transfer_;
};

class TestCxlBufferPublication : public TestCxlBuffer {
 protected:
  Duration heartbeatInterval() const override { return 1_h; }

  void SetUp() override {
    TestCxlBuffer::SetUp();
    ASSERT_FALSE(HasFatalFailure());
    // Let each initial background publication finish. No owner will publish
    // again during this bounded test; stopAndJoin wakes the long timer.
    for (auto &fabric : {authority_, peer_}) {
      bool published = false;
      const auto deadline = std::chrono::steady_clock::now() + 5s;
      while (!published && std::chrono::steady_clock::now() < deadline) {
        auto record = fabric->endpointState().snapshot();
        published = record && loadLe64(&record->heartbeat) > 0;
        if (!published) {
          std::this_thread::sleep_for(1ms);
        }
      }
      ASSERT_TRUE(published);
    }
  }
};

TEST_F(TestCxlBufferPublication, PendingOwnerPublicationCopiesNothingAndCanBeRetried) {
  auto remote = arena_->tryAllocate(8);
  auto local = SharedBuffer::allocateHeap(8);
  ASSERT_OK(remote);
  ASSERT_OK(local);
  std::fill_n(remote->data(), 8, uint8_t{0x5a});
  std::fill_n(local->data(), 8, uint8_t{0xa5});
  auto readable = remote->exportRemote(RemoteAccess::Read);
  auto writable = remote->exportRemote(RemoteAccess::Write);
  ASSERT_OK(readable);
  ASSERT_OK(writable);
  std::array<SharedBuffer, 1> buffers{*local};
  auto directory = authority_->layout().range(CxlRangeKind::EndpointDirectory, alignof(CxlEndpointRecord));
  ASSERT_OK(directory);
  auto &record = *reinterpret_cast<CxlEndpointRecord *>(directory->data());
  auto sequence = std::atomic_ref<uint64_t>(record.recordSequence);
  const auto even = sequence.load(std::memory_order_acquire);
  const auto crc = loadLe32(&record.recordCrc32c);
  const auto before = TransportEvidence::process().snapshot();

  sequence.store(even + 1U, std::memory_order_release);
  auto pull = folly::coro::blockingWait(transfer_->pull(readable->handle(), buffers));
  auto push = folly::coro::blockingWait(transfer_->push(writable->handle(), buffers));
  EXPECT_FALSE(pull);
  EXPECT_FALSE(push);
  if (!pull) {
    EXPECT_EQ(pull.error().code(), RPCCode::kTimeout);
  }
  if (!push) {
    EXPECT_EQ(push.error().code(), RPCCode::kTimeout);
  }
  EXPECT_TRUE(std::all_of(local->data(), local->data() + 8, [](auto b) { return b == 0xa5; }));
  EXPECT_TRUE(std::all_of(remote->data(), remote->data() + 8, [](auto b) { return b == 0x5a; }));
  EXPECT_EQ(TransportEvidence::process().snapshot(), before);

  sequence.store(even, std::memory_order_release);
  storeLe32(&record.recordCrc32c, crc ^ 1U);
  auto corrupt = folly::coro::blockingWait(transfer_->pull(readable->handle(), buffers));
  EXPECT_FALSE(corrupt);
  if (!corrupt) {
    EXPECT_EQ(corrupt.error().code(), StatusCode::kDataCorruption);
  }
  EXPECT_EQ(TransportEvidence::process().snapshot(), before);
  EXPECT_TRUE(std::all_of(local->data(), local->data() + 8, [](auto b) { return b == 0xa5; }));
  storeLe32(&record.recordCrc32c, crc);

  ASSERT_OK(folly::coro::blockingWait(transfer_->pull(readable->handle(), buffers)));
  EXPECT_TRUE(std::all_of(local->data(), local->data() + 8, [](auto b) { return b == 0x5a; }));
  std::fill_n(local->data(), 8, uint8_t{0x3c});
  ASSERT_OK(folly::coro::blockingWait(transfer_->push(writable->handle(), buffers)));
  EXPECT_TRUE(std::all_of(remote->data(), remote->data() + 8, [](auto b) { return b == 0x3c; }));
  const auto after = TransportEvidence::process().snapshot();
  EXPECT_EQ(after[TransportEvidence::CxlBulkReadBytes] - before[TransportEvidence::CxlBulkReadBytes], 8);
  EXPECT_EQ(after[TransportEvidence::CxlBulkWriteBytes] - before[TransportEvidence::CxlBulkWriteBytes], 8);
}

TEST_F(TestCxlBufferPublication, PendingAllocationPublicationStillRequiresStableCrc) {
  auto remote = arena_->tryAllocate(8);
  auto local = SharedBuffer::allocateHeap(8);
  ASSERT_OK(remote);
  ASSERT_OK(local);
  std::fill_n(remote->data(), 8, uint8_t{0x5a});
  std::fill_n(local->data(), 8, uint8_t{0xa5});
  auto readable = remote->exportRemote(RemoteAccess::Read);
  ASSERT_OK(readable);
  ASSERT_EQ(readable->handle().ownerEndpoint, 1);
  ASSERT_EQ(readable->handle().allocationSlot, 0);
  std::array<SharedBuffer, 1> buffers{*local};
  auto directory = authority_->layout().range(CxlRangeKind::AllocationDirectory, alignof(CxlAllocationRecord));
  ASSERT_OK(directory);
  auto &record = *reinterpret_cast<CxlAllocationRecord *>(directory->data());
  auto sequence = std::atomic_ref<uint64_t>(record.recordSequence);
  const auto even = sequence.load(std::memory_order_acquire);
  const auto crc = loadLe32(&record.recordCrc32c);
  const auto before = TransportEvidence::process().snapshot();

  sequence.store(even + 1U, std::memory_order_release);
  auto pending = folly::coro::blockingWait(transfer_->pull(readable->handle(), buffers));
  EXPECT_FALSE(pending);
  if (!pending) {
    EXPECT_EQ(pending.error().code(), RPCCode::kTimeout);
  }
  EXPECT_EQ(TransportEvidence::process().snapshot(), before);
  EXPECT_TRUE(std::all_of(local->data(), local->data() + 8, [](auto b) { return b == 0xa5; }));

  sequence.store(even, std::memory_order_release);
  storeLe32(&record.recordCrc32c, crc ^ 1U);
  auto corrupt = folly::coro::blockingWait(transfer_->pull(readable->handle(), buffers));
  EXPECT_FALSE(corrupt);
  if (!corrupt) {
    EXPECT_EQ(corrupt.error().code(), StatusCode::kDataCorruption);
  }
  EXPECT_EQ(TransportEvidence::process().snapshot(), before);
  EXPECT_TRUE(std::all_of(local->data(), local->data() + 8, [](auto b) { return b == 0xa5; }));
  storeLe32(&record.recordCrc32c, crc);
  ASSERT_OK(folly::coro::blockingWait(transfer_->pull(readable->handle(), buffers)));
  EXPECT_TRUE(std::all_of(local->data(), local->data() + 8, [](auto b) { return b == 0x5a; }));
  const auto after = TransportEvidence::process().snapshot();
  EXPECT_EQ(after[TransportEvidence::CxlBulkReadBytes] - before[TransportEvidence::CxlBulkReadBytes], 8);
}

TEST_F(TestCxlBuffer, SubrangeKeepsAllocationIdentityAndGeneration) {
  EXPECT_EQ(arena_->capacity(), 64_KB);
  auto bufferResult = arena_->tryAllocate(4096);
  ASSERT_OK(bufferResult);
  auto subrangeResult = bufferResult->subrange(512, 1024);
  ASSERT_OK(subrangeResult);
  auto exportResult = subrangeResult->exportRemote(RemoteAccess::Read);
  ASSERT_OK(exportResult);

  const auto &handle = exportResult->handle();
  EXPECT_EQ(handle.length, 1024);
  EXPECT_EQ(handle.offset % kCxlCacheLineBytes, 0);
  EXPECT_EQ(handle.ownerEndpoint, 1);
  EXPECT_EQ(handle.arenaId, 1);
  EXPECT_EQ(handle.sessionGeneration, 71);
  EXPECT_EQ(handle.ownerGeneration, 1);
  EXPECT_EQ(handle.allocationGeneration, 1);
  ASSERT_RESULT_EQ(true, arena_->isAllocated(handle));
}

TEST_F(TestCxlBuffer, PullAndPushCopyExactScatterGatherRanges) {
  const auto countersBefore = TransportEvidence::process().snapshot();
  auto remoteResult = arena_->tryAllocate(8);
  ASSERT_OK(remoteResult);
  auto &remote = *remoteResult;
  std::array<uint8_t, 8> initial{1, 2, 3, 4, 5, 6, 7, 8};
  std::copy(initial.begin(), initial.end(), remote.data());

  auto readable = remote.exportRemote(RemoteAccess::Read);
  ASSERT_OK(readable);
  auto firstResult = SharedBuffer::allocateHeap(3);
  auto secondResult = SharedBuffer::allocateHeap(5);
  ASSERT_OK(firstResult);
  ASSERT_OK(secondResult);
  std::array<SharedBuffer, 2> destination{*firstResult, *secondResult};
  ASSERT_OK(folly::coro::blockingWait(transfer_->pull(readable->handle(), destination)));
  EXPECT_TRUE(std::equal(initial.begin(), initial.begin() + 3, destination[0].data()));
  EXPECT_TRUE(std::equal(initial.begin() + 3, initial.end(), destination[1].data()));

  std::array<uint8_t, 8> replacement{9, 10, 11, 12, 13, 14, 15, 16};
  std::copy(replacement.begin(), replacement.begin() + 3, destination[0].data());
  std::copy(replacement.begin() + 3, replacement.end(), destination[1].data());
  auto writable = remote.exportRemote(RemoteAccess::Write);
  ASSERT_OK(writable);
  ASSERT_OK(folly::coro::blockingWait(transfer_->push(writable->handle(), destination)));
  EXPECT_TRUE(std::equal(replacement.begin(), replacement.end(), remote.data()));
  auto counters = TransportEvidence::process().snapshot();
  EXPECT_EQ(counters[TransportEvidence::CxlBulkReadBytes] - countersBefore[TransportEvidence::CxlBulkReadBytes], 8);
  EXPECT_EQ(counters[TransportEvidence::CxlBulkWriteBytes] - countersBefore[TransportEvidence::CxlBulkWriteBytes], 8);
  // A denied transfer must not inflate completed-copy evidence.
  ASSERT_ERROR(folly::coro::blockingWait(transfer_->push(readable->handle(), destination)),
               RPCCode::kRemoteBufferAccessDenied);
  EXPECT_EQ(TransportEvidence::process().snapshot(), counters);
}

TEST_F(TestCxlBuffer, BothHandleAndOwnerRecordEnforcePermissions) {
  auto remote = arena_->tryAllocate(8);
  ASSERT_OK(remote);
  auto readable = remote->exportRemote(RemoteAccess::Read);
  ASSERT_OK(readable);
  auto local = SharedBuffer::allocateHeap(8);
  ASSERT_OK(local);
  std::array<SharedBuffer, 1> buffers{*local};

  ASSERT_ERROR(folly::coro::blockingWait(transfer_->push(readable->handle(), buffers)),
               RPCCode::kRemoteBufferAccessDenied);

  auto forged = readable->handle();
  forged.permissions = remoteAccessBits(RemoteAccess::Write);
  sealRemoteBufferHandle(forged);
  ASSERT_ERROR(folly::coro::blockingWait(transfer_->push(forged, buffers)), RPCCode::kRemoteBufferAccessDenied);
}

TEST_F(TestCxlBuffer, ExportLeasePinsAllocationAfterLocalViewsDisappear) {
  auto buffer = arena_->tryAllocate(64);
  ASSERT_OK(buffer);
  auto exported = buffer->exportRemote(RemoteAccess::Read);
  ASSERT_OK(exported);
  const auto handle = exported->handle();

  *buffer = SharedBuffer{};
  ASSERT_RESULT_EQ(true, arena_->isAllocated(handle));
  auto lease = exported->takeLease();
  lease.reset();
  ASSERT_RESULT_EQ(false, arena_->isAllocated(handle));
}

TEST_F(TestCxlBuffer, ReusedSlotRejectsStaleHandleGeneration) {
  RemoteBufferHandle stale;
  {
    auto first = arena_->tryAllocate(1024);
    ASSERT_OK(first);
    auto exported = first->exportRemote(RemoteAccess::Read);
    ASSERT_OK(exported);
    stale = exported->handle();
  }

  auto replacement = arena_->tryAllocate(1024);
  ASSERT_OK(replacement);
  auto replacementExport = replacement->exportRemote(RemoteAccess::Read);
  ASSERT_OK(replacementExport);
  EXPECT_EQ(stale.allocationSlot, replacementExport->handle().allocationSlot);
  EXPECT_NE(stale.allocationGeneration, replacementExport->handle().allocationGeneration);

  auto local = SharedBuffer::allocateHeap(stale.length);
  ASSERT_OK(local);
  std::array<SharedBuffer, 1> buffers{*local};
  ASSERT_ERROR(folly::coro::blockingWait(transfer_->pull(stale, buffers)), RPCCode::kStaleGeneration);
}

TEST_F(TestCxlBuffer, RejectsChecksumAndAggregateLengthMismatch) {
  auto remote = arena_->tryAllocate(16);
  ASSERT_OK(remote);
  auto exported = remote->exportRemote(RemoteAccess::Read);
  ASSERT_OK(exported);
  auto shortLocal = SharedBuffer::allocateHeap(15);
  ASSERT_OK(shortLocal);
  std::array<SharedBuffer, 1> buffers{*shortLocal};
  ASSERT_ERROR(folly::coro::blockingWait(transfer_->pull(exported->handle(), buffers)), StatusCode::kInvalidArg);

  auto corrupt = exported->handle();
  ++corrupt.checksum;
  ASSERT_ERROR(folly::coro::blockingWait(transfer_->pull(corrupt, buffers)), StatusCode::kInvalidFormat);
}

TEST_F(TestCxlBuffer, GenericBatchPostsMultipleCxlTransfers) {
  auto firstRemote = arena_->tryAllocate(4);
  auto secondRemote = arena_->tryAllocate(4);
  ASSERT_OK(firstRemote);
  ASSERT_OK(secondRemote);
  std::fill_n(firstRemote->data(), firstRemote->size(), uint8_t{0x31});
  std::fill_n(secondRemote->data(), secondRemote->size(), uint8_t{0x42});
  auto firstExport = firstRemote->exportRemote(RemoteAccess::Read);
  auto secondExport = secondRemote->exportRemote(RemoteAccess::Read);
  ASSERT_OK(firstExport);
  ASSERT_OK(secondExport);
  auto firstLocal = SharedBuffer::allocateHeap(4);
  auto secondLocal = SharedBuffer::allocateHeap(4);
  ASSERT_OK(firstLocal);
  ASSERT_OK(secondLocal);

  BulkTransferBatch batch(transfer_.get(), BulkDirection::Pull);
  ASSERT_OK(batch.add(firstExport->handle(), *firstLocal));
  ASSERT_OK(batch.add(secondExport->handle(), *secondLocal));
  EXPECT_EQ(batch.size(), 2);
  ASSERT_OK(folly::coro::blockingWait(batch.post()));
  EXPECT_TRUE(std::all_of(firstLocal->data(), firstLocal->data() + firstLocal->size(), [](uint8_t value) {
    return value == 0x31;
  }));
  EXPECT_TRUE(std::all_of(secondLocal->data(), secondLocal->data() + secondLocal->size(), [](uint8_t value) {
    return value == 0x42;
  }));
}

TEST_F(TestCxlBuffer, ReadOnlyExportCopyOwnsStableBytesAfterDiskBufferReuse) {
  std::vector<uint8_t> diskBuffer(4096, 0x5a);
  auto exported = arena_->exportReadOnlyCopy(diskBuffer);
  ASSERT_OK(exported);
  const auto handle = exported->handle();
  auto lifetime = exported->takeLease();
  std::fill(diskBuffer.begin(), diskBuffer.end(), 0xa5);
  auto local = SharedBuffer::allocateAligned(diskBuffer.size(), 4096);
  ASSERT_OK(local);
  std::array<SharedBuffer, 1> buffers{*local};
  ASSERT_OK(folly::coro::blockingWait(transfer_->pull(handle, buffers)));
  EXPECT_TRUE(std::all_of(local->data(), local->data() + local->size(), [](uint8_t value) { return value == 0x5a; }));
  ASSERT_ERROR(folly::coro::blockingWait(transfer_->push(handle, buffers)), RPCCode::kRemoteBufferAccessDenied);
  lifetime.reset();
  ASSERT_RESULT_EQ(false, arena_->isAllocated(handle));
}

}  // namespace hf3fs::net::cxl
