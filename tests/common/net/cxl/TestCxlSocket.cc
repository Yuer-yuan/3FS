#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <gtest/gtest.h>
#include <optional>
#include <poll.h>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <unistd.h>
#include <vector>

#include "common/net/cxl/CxlSocket.h"
#include "common/utils/Size.h"
#include "tests/GtestHelpers.h"

namespace hf3fs::net::cxl {
namespace {

using namespace std::chrono_literals;

constexpr uint64_t kTestRegionBytes = 1_MB;
constexpr uint32_t kTestDepth = 4;
constexpr uint32_t kTestCellBytes = 256;

class SocketTemporaryFile {
 public:
  SocketTemporaryFile() {
    std::array<char, 34> name{};
    constexpr std::string_view pattern = "/tmp/hf3fs-socket-XXXXXX";
    std::copy(pattern.begin(), pattern.end(), name.begin());
    fd_ = ::mkstemp(name.data());
    if (fd_ < 0) {
      throw std::runtime_error("mkstemp failed");
    }
    path_ = name.data();
  }

  ~SocketTemporaryFile() {
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

bool waitFd(int fd, std::chrono::milliseconds timeout = 1s) {
  pollfd descriptor{.fd = fd, .events = POLLIN, .revents = 0};
  int result;
  do {
    result = ::poll(&descriptor, 1, static_cast<int>(timeout.count()));
  } while (result < 0 && errno == EINTR);
  return result == 1 && (descriptor.revents & POLLIN) != 0;
}

}  // namespace

TEST(CxlIdleBackoff, ResetsOnObservedProgress) {
  CxlIdleBackoff backoff(1_us, 8_us);
  EXPECT_EQ(backoff.next(false), 1_us);
  EXPECT_EQ(backoff.next(false), 2_us);
  EXPECT_EQ(backoff.next(false), 4_us);
  EXPECT_EQ(backoff.next(false), 8_us);
  EXPECT_EQ(backoff.next(false), 8_us);
  EXPECT_EQ(backoff.next(true), 1_us);
  EXPECT_EQ(backoff.next(false), 1_us);
  EXPECT_EQ(backoff.next(false), 2_us);
}

class TestCxlSocket : public ::testing::Test {
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
    config_ = makeLaneConfig();

    engine_ = std::make_shared<CxlProgressEngine>();
    ASSERT_OK(engine_->start());
    const Address requesterAddress{0x0100007fU, 9001, Address::CXL};
    const Address acceptorAddress{0x0200007fU, 9002, Address::CXL};
    auto pair =
        CxlSocket::createConnectedPairForTest(*region_, *layout_, config_, engine_, acceptorAddress, requesterAddress);
    ASSERT_OK(pair);
    client_ = std::move(pair->first);
    server_ = std::move(pair->second);
  }

  void TearDown() override {
    if (client_) {
      client_->close();
    }
    if (server_) {
      server_->close();
    }
    if (engine_) {
      engine_->stopAndJoin();
    }
  }

  CxlLayoutManifest makeManifest() const {
    CxlLayoutManifest manifest{
        .totalRegionBytes = kTestRegionBytes,
        .sessionGeneration = 19,
        .endpointCount = 2,
        .laneCount = 1,
        .authorityEndpoint = EndpointId{1},
        .authorityGeneration = 11,
    };
    for (size_t index = 0; index < manifest.manifestSha256.size(); ++index) {
      manifest.manifestSha256[index] = static_cast<std::byte>(0x40U + index);
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

  CxlLaneConfig makeLaneConfig() const {
    const auto &cursorRange = manifest_.ranges[static_cast<size_t>(CxlRangeKind::CursorPages) - 1U];
    const auto &frameRange = manifest_.ranges[static_cast<size_t>(CxlRangeKind::FrameRings) - 1U];
    const auto &payloadRange = manifest_.ranges[static_cast<size_t>(CxlRangeKind::RpcPayloadCells) - 1U];
    const uint64_t ringBytes = kTestDepth * sizeof(CxlFrameEntry);
    const uint64_t payloadBytes = kTestDepth * kTestCellBytes;
    return CxlLaneConfig{
        .sessionGeneration = manifest_.sessionGeneration,
        .laneGeneration = 5,
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

  void fillServerOutboundRing() {
    auto bytes = deterministicBytes(kTestDepth * kTestCellBytes, 91);
    iovec vector{.iov_base = bytes.data(), .iov_len = bytes.size()};
    ASSERT_RESULT_EQ(bytes.size(), server_->send(&vector, 1));
    ASSERT_OK(server_->flush());
  }

  SocketTemporaryFile file_;
  CxlLayoutManifest manifest_;
  CxlLaneConfig config_;
  std::optional<CxlRegion> region_;
  std::optional<CxlLayout> layout_;
  std::shared_ptr<CxlProgressEngine> engine_;
  std::shared_ptr<CxlSocket> client_;
  std::shared_ptr<CxlSocket> server_;
};

TEST_F(TestCxlSocket, AcceptedPublishedAndDeliveredAreDistinct) {
  std::byte byte{0x5A};
  iovec vector{.iov_base = &byte, .iov_len = 1};
  ASSERT_RESULT_EQ(1, client_->send(&vector, 1));

  auto accepted = client_->publicationSnapshot();
  ASSERT_TRUE(accepted.has_value());
  EXPECT_EQ(accepted->acceptedOffset, 1);
  EXPECT_EQ(accepted->publishedOffset, 0);
  EXPECT_EQ(accepted->peerDeliveredOffset, 0);
  EXPECT_TRUE(accepted->trustworthy);
  EXPECT_TRUE(client_->hasReservedSlot());

  ASSERT_OK(client_->flush());
  auto published = client_->publicationSnapshot();
  ASSERT_TRUE(published.has_value());
  EXPECT_EQ(published->acceptedOffset, 1);
  EXPECT_EQ(published->publishedOffset, 1);
  EXPECT_EQ(published->peerDeliveredOffset, 0);

  std::byte output{};
  ASSERT_RESULT_EQ(1, server_->recv(folly::MutableByteRange(reinterpret_cast<uint8_t *>(&output), 1)));
  auto delivered = client_->publicationSnapshot();
  ASSERT_TRUE(delivered.has_value());
  EXPECT_EQ(delivered->peerDeliveredOffset, 1);
  EXPECT_EQ(output, byte);
}

TEST_F(TestCxlSocket, PartialSendAndRecvPreserveByteStream) {
  const auto payload = deterministicBytes(5 * kTestCellBytes + 3, 17);
  std::vector<std::byte> received;
  size_t sent = 0;

  while (sent != payload.size()) {
    iovec vector{.iov_base = const_cast<std::byte *>(payload.data() + sent), .iov_len = payload.size() - sent};
    auto result = client_->send(&vector, 1);
    ASSERT_OK(result);
    ASSERT_GT(*result, 0);
    sent += *result;
    ASSERT_OK(client_->flush());

    while (true) {
      std::array<std::byte, 37> output{};
      auto read = server_->recv(folly::MutableByteRange(reinterpret_cast<uint8_t *>(output.data()), output.size()));
      ASSERT_OK(read);
      if (*read == 0) {
        break;
      }
      received.insert(received.end(), output.begin(), output.begin() + *read);
    }
  }

  EXPECT_EQ(received, payload);
  auto snapshot = client_->publicationSnapshot();
  ASSERT_TRUE(snapshot.has_value());
  EXPECT_EQ(snapshot->acceptedOffset, payload.size());
  EXPECT_EQ(snapshot->publishedOffset, payload.size());
  EXPECT_EQ(snapshot->peerDeliveredOffset, payload.size());
}

TEST_F(TestCxlSocket, MultipleIovecsAndMessagesShareOneCell) {
  std::array<std::byte, 2> first{std::byte{'a'}, std::byte{'b'}};
  std::array<std::byte, 3> second{std::byte{'c'}, std::byte{'d'}, std::byte{'e'}};
  std::array<iovec, 2> firstCall{{
      {.iov_base = first.data(), .iov_len = 1},
      {.iov_base = first.data() + 1, .iov_len = 1},
  }};
  iovec secondCall{.iov_base = second.data(), .iov_len = second.size()};
  ASSERT_RESULT_EQ(first.size(), client_->send(firstCall.data(), firstCall.size()));
  ASSERT_RESULT_EQ(second.size(), client_->send(&secondCall, 1));

  auto staged = client_->publicationSnapshot();
  ASSERT_TRUE(staged.has_value());
  EXPECT_EQ(staged->acceptedOffset, 5);
  EXPECT_EQ(staged->publishedOffset, 0);
  ASSERT_OK(client_->flush());

  std::array<std::byte, 5> output{};
  ASSERT_RESULT_EQ(output.size(),
                   server_->recv(folly::MutableByteRange(reinterpret_cast<uint8_t *>(output.data()), output.size())));
  const std::array<std::byte, 5> expected{std::byte{'a'},
                                          std::byte{'b'},
                                          std::byte{'c'},
                                          std::byte{'d'},
                                          std::byte{'e'}};
  EXPECT_EQ(output, expected);
}

TEST_F(TestCxlSocket, CellBoundaryMaintainsPublicationInvariant) {
  auto cell = deterministicBytes(kTestCellBytes, 29);
  iovec full{.iov_base = cell.data(), .iov_len = cell.size()};
  ASSERT_RESULT_EQ(cell.size(), client_->send(&full, 1));
  auto boundary = client_->publicationSnapshot();
  ASSERT_TRUE(boundary.has_value());
  EXPECT_EQ(boundary->acceptedOffset, kTestCellBytes);
  EXPECT_EQ(boundary->publishedOffset, kTestCellBytes);
  EXPECT_FALSE(client_->hasReservedSlot());

  std::byte tail{0x33};
  iovec partial{.iov_base = &tail, .iov_len = 1};
  ASSERT_RESULT_EQ(1, client_->send(&partial, 1));
  auto staged = client_->publicationSnapshot();
  ASSERT_TRUE(staged.has_value());
  EXPECT_EQ(staged->acceptedOffset - staged->publishedOffset, client_->stagedBytes());
  EXPECT_TRUE(client_->hasReservedSlot());
  ASSERT_OK(client_->flush());
  auto flushed = client_->publicationSnapshot();
  ASSERT_TRUE(flushed.has_value());
  EXPECT_EQ(flushed->acceptedOffset, flushed->publishedOffset);
  EXPECT_FALSE(client_->hasReservedSlot());
}

TEST_F(TestCxlSocket, EveryPositiveSendIsPublishedOrFlushableFromReservedCredit) {
  for (size_t length : {size_t{1}, size_t{17}, size_t{238}, size_t{256}}) {
    auto bytes = deterministicBytes(length, static_cast<uint8_t>(length));
    iovec vector{.iov_base = bytes.data(), .iov_len = bytes.size()};
    ASSERT_RESULT_EQ(bytes.size(), client_->send(&vector, 1));
    auto snapshot = client_->publicationSnapshot();
    ASSERT_TRUE(snapshot.has_value());
    ASSERT_LE(snapshot->publishedOffset, snapshot->acceptedOffset);
    EXPECT_EQ(snapshot->acceptedOffset - snapshot->publishedOffset, client_->stagedBytes());
    EXPECT_EQ(client_->hasReservedSlot(), client_->stagedBytes() != 0);
    ASSERT_OK(client_->flush());
    snapshot = client_->publicationSnapshot();
    ASSERT_TRUE(snapshot.has_value());
    EXPECT_EQ(snapshot->publishedOffset, snapshot->acceptedOffset);

    std::vector<std::byte> output(length);
    ASSERT_RESULT_EQ(length,
                     server_->recv(folly::MutableByteRange(reinterpret_cast<uint8_t *>(output.data()), output.size())));
    EXPECT_EQ(output, bytes);
  }
}

TEST_F(TestCxlSocket, EventFdSignalsReadableAndWritableTransitions) {
  fillServerOutboundRing();
  auto armed = server_->poll(0);
  ASSERT_OK(armed);
  EXPECT_EQ(*armed, 0);

  std::byte request{0x6C};
  iovec vector{.iov_base = &request, .iov_len = 1};
  ASSERT_RESULT_EQ(1, client_->send(&vector, 1));
  ASSERT_OK(client_->flush());
  ASSERT_TRUE(waitFd(server_->fd()));
  auto readable = server_->poll(0);
  ASSERT_OK(readable);
  EXPECT_NE(*readable & Socket::kEventReadableFlag, 0);

  std::byte requestOutput{};
  ASSERT_RESULT_EQ(
      1,
      server_->recv(folly::MutableByteRange(reinterpret_cast<uint8_t *>(&requestOutput), sizeof(requestOutput))));
  EXPECT_EQ(requestOutput, request);

  armed = server_->poll(0);
  ASSERT_OK(armed);
  EXPECT_EQ(*armed, 0);
  std::array<std::byte, kTestCellBytes> completion{};
  ASSERT_RESULT_EQ(
      completion.size(),
      client_->recv(folly::MutableByteRange(reinterpret_cast<uint8_t *>(completion.data()), completion.size())));
  ASSERT_TRUE(waitFd(server_->fd()));
  auto writable = server_->poll(0);
  ASSERT_OK(writable);
  EXPECT_NE(*writable & Socket::kEventWritableFlag, 0);
}

TEST_F(TestCxlSocket, ArmThenPublishCannotLoseWakeup) {
  fillServerOutboundRing();
  constexpr size_t iterations = 10000;
  size_t timeouts = 0;
  for (size_t iteration = 0; iteration < iterations; ++iteration) {
    auto armed = server_->poll(0);
    ASSERT_OK(armed);
    ASSERT_EQ(*armed, 0);

    std::byte request = static_cast<std::byte>(iteration & 0xFFU);
    iovec vector{.iov_base = &request, .iov_len = 1};
    ASSERT_RESULT_EQ(1, client_->send(&vector, 1));
    ASSERT_OK(client_->flush());
    if (!waitFd(server_->fd())) {
      ++timeouts;
      continue;
    }
    auto readable = server_->poll(0);
    ASSERT_OK(readable);
    ASSERT_NE(*readable & Socket::kEventReadableFlag, 0);
    std::byte output{};
    ASSERT_RESULT_EQ(1, server_->recv(folly::MutableByteRange(reinterpret_cast<uint8_t *>(&output), sizeof(output))));
    ASSERT_EQ(output, request);
  }
  EXPECT_EQ(timeouts, 0);
}

TEST_F(TestCxlSocket, EmptyRecvRearmsReadableNotification) {
  ASSERT_TRUE(waitFd(server_->fd()));
  ASSERT_OK(server_->poll(0));

  for (uint8_t value : {uint8_t{0x41}, uint8_t{0x52}}) {
    std::byte request{value};
    iovec vector{.iov_base = &request, .iov_len = 1};
    ASSERT_RESULT_EQ(1, client_->send(&vector, 1));
    ASSERT_OK(client_->flush());
    ASSERT_TRUE(waitFd(server_->fd()));
    auto readable = server_->poll(0);
    ASSERT_OK(readable);
    ASSERT_NE(*readable & Socket::kEventReadableFlag, 0);

    std::byte output{};
    ASSERT_RESULT_EQ(1, server_->recv(folly::MutableByteRange(reinterpret_cast<uint8_t *>(&output), sizeof(output))));
    EXPECT_EQ(output, request);
    ASSERT_RESULT_EQ(0, server_->recv(folly::MutableByteRange(reinterpret_cast<uint8_t *>(&output), sizeof(output))));
  }
}

TEST_F(TestCxlSocket, CrossWorkerProducerIsRejected) {
  std::byte byte{0x14};
  iovec vector{.iov_base = &byte, .iov_len = 1};
  ASSERT_RESULT_EQ(1, client_->send(&vector, 1));

  std::atomic<status_code_t> resultCode{StatusCode::kOK};
  std::thread otherWorker([&] {
    auto result = client_->flush();
    resultCode.store(result ? StatusCode::kOK : result.error().code(), std::memory_order_release);
  });
  otherWorker.join();
  EXPECT_EQ(resultCode.load(std::memory_order_acquire), StatusCode::kInvalidArg);
  ASSERT_OK(client_->flush());
}

TEST_F(TestCxlSocket, StableWorkerTokenAllowsThreadMigrationAndRejectsAnotherWorker) {
  int workerA;
  int workerB;
  ASSERT_OK(client_->bindExecutionOwner(&workerA));

  std::byte byte{0x24};
  std::atomic<status_code_t> sendCode{StatusCode::kUnknown};
  std::thread firstThread([&] {
    Socket::ExecutionScope scope(&workerA);
    iovec vector{.iov_base = &byte, .iov_len = 1};
    auto result = client_->send(&vector, 1);
    sendCode.store(result ? StatusCode::kOK : result.error().code(), std::memory_order_release);
  });
  firstThread.join();
  EXPECT_EQ(sendCode.load(std::memory_order_acquire), StatusCode::kOK);

  std::atomic<status_code_t> flushCode{StatusCode::kUnknown};
  std::thread migratedThread([&] {
    Socket::ExecutionScope scope(&workerA);
    auto result = client_->flush();
    flushCode.store(result ? StatusCode::kOK : result.error().code(), std::memory_order_release);
  });
  migratedThread.join();
  EXPECT_EQ(flushCode.load(std::memory_order_acquire), StatusCode::kOK);

  std::thread wrongWorker([&] {
    Socket::ExecutionScope scope(&workerB);
    auto result = client_->flush();
    flushCode.store(result ? StatusCode::kOK : result.error().code(), std::memory_order_release);
  });
  wrongWorker.join();
  EXPECT_EQ(flushCode.load(std::memory_order_acquire), StatusCode::kInvalidArg);
}

TEST_F(TestCxlSocket, ZeroLengthAndInvalidIovecDoNotChangePublication) {
  ASSERT_RESULT_EQ(0, client_->send(nullptr, 0));
  std::array<std::byte, 1> output{};
  ASSERT_RESULT_EQ(0, server_->recv(folly::MutableByteRange(reinterpret_cast<uint8_t *>(output.data()), size_t{0})));
  iovec invalid{.iov_base = nullptr, .iov_len = 1};
  ASSERT_ERROR(client_->send(&invalid, 1), StatusCode::kInvalidArg);
  auto snapshot = client_->publicationSnapshot();
  ASSERT_TRUE(snapshot.has_value());
  EXPECT_EQ(snapshot->acceptedOffset, 0);
  EXPECT_EQ(snapshot->publishedOffset, 0);
}

TEST_F(TestCxlSocket, FiveHundredTwelveMiBIovecUsesNormalPartialSendSemantics) {
  auto prefix = deterministicBytes(kTestDepth * kTestCellBytes, 71);
  iovec vector{.iov_base = prefix.data(), .iov_len = 512_MB};
  ASSERT_RESULT_EQ(prefix.size(), client_->send(&vector, 1));
  auto snapshot = client_->publicationSnapshot();
  ASSERT_TRUE(snapshot.has_value());
  EXPECT_EQ(snapshot->acceptedOffset, prefix.size());
  EXPECT_EQ(snapshot->publishedOffset, prefix.size());
  EXPECT_FALSE(client_->hasReservedSlot());
}

TEST_F(TestCxlSocket, CorruptDeliveredRecordMakesSnapshotUntrustworthy) {
  auto range = region_->checkedRange(config_.directions[0].deliveredRecordOffset,
                                     sizeof(CxlDeliveredRecord),
                                     alignof(CxlDeliveredRecord));
  ASSERT_OK(range);
  auto &record = *reinterpret_cast<CxlDeliveredRecord *>(range->data());
  storeLe64(&record.deliveredOffsetComplement, 0);

  auto snapshot = client_->publicationSnapshot();
  ASSERT_TRUE(snapshot.has_value());
  EXPECT_FALSE(snapshot->trustworthy);
  EXPECT_FALSE(snapshot->pending);
}

TEST_F(TestCxlSocket, InProgressDeliveredPublicationIsNotCorruption) {
  const auto payload = deterministicBytes(kTestCellBytes, 83);
  iovec vector{.iov_base = const_cast<std::byte *>(payload.data()), .iov_len = payload.size()};
  ASSERT_RESULT_EQ(payload.size(), client_->send(&vector, 1));
  std::vector<uint8_t> output(payload.size());
  ASSERT_RESULT_EQ(output.size(), server_->recv(folly::MutableByteRange(output.data(), output.size())));

  auto range = region_->checkedRange(config_.directions[0].deliveredRecordOffset,
                                     sizeof(CxlDeliveredRecord), alignof(CxlDeliveredRecord));
  ASSERT_OK(range);
  auto *words = reinterpret_cast<uint64_t *>(range->data());
  const auto finalSequence = std::atomic_ref<uint64_t>(words[0]).load(std::memory_order_acquire);
  ASSERT_NE(finalSequence, 0);
  ASSERT_EQ(finalSequence & 1U, 0);
  const auto corruptBefore = client_->metrics()->snapshot().corruptPublications;

  // Deterministically pause a valid peer at the last step of publication:
  // its payload words are ready, but its final even sequence is not released.
  std::atomic_ref<uint64_t>(words[0]).store(finalSequence - 1U, std::memory_order_release);
  auto pending = client_->publicationSnapshot();
  ASSERT_TRUE(pending.has_value());
  EXPECT_TRUE(pending->pending);
  EXPECT_FALSE(pending->trustworthy);
  EXPECT_EQ(client_->metrics()->snapshot().corruptPublications, corruptBefore);
  std::atomic_ref<uint64_t>(words[0]).store(finalSequence, std::memory_order_release);

  auto completed = client_->publicationSnapshot();
  ASSERT_TRUE(completed.has_value());
  EXPECT_TRUE(completed->trustworthy);
  EXPECT_FALSE(completed->pending);
  EXPECT_EQ(completed->peerDeliveredOffset, payload.size());
  EXPECT_EQ(client_->metrics()->snapshot().corruptPublications, corruptBefore);
  ASSERT_OK(client_->check());
}

TEST_F(TestCxlSocket, StableDeliveredRegressionIsNotPending) {
  auto range = region_->checkedRange(config_.directions[0].deliveredRecordOffset,
                                     sizeof(CxlDeliveredRecord), alignof(CxlDeliveredRecord));
  ASSERT_OK(range);
  auto original = loadCxlOwnerRecord<CxlDeliveredRecord>(*range);
  ASSERT_TRUE(original.has_value());
  auto payload = deterministicBytes(1, 84);
  iovec vector{.iov_base = payload.data(), .iov_len = payload.size()};
  ASSERT_RESULT_EQ(1, client_->send(&vector, 1));
  ASSERT_OK(client_->flush());
  uint8_t output;
  ASSERT_RESULT_EQ(1, server_->recv(folly::MutableByteRange(&output, 1)));
  auto delivered = client_->publicationSnapshot();
  ASSERT_TRUE(delivered.has_value());
  ASSERT_TRUE(delivered->trustworthy);
  ASSERT_EQ(delivered->peerDeliveredOffset, 1);
  ASSERT_TRUE(publishCxlOwnerRecord(*range, *original));
  auto regressed = client_->publicationSnapshot();
  ASSERT_TRUE(regressed.has_value());
  EXPECT_FALSE(regressed->trustworthy);
  EXPECT_FALSE(regressed->pending);
}

TEST_F(TestCxlSocket, PeerIdentityComesFromDeclaredCxlAddress) {
  const Address declaredPeer{0x0200007fU, 9002, Address::CXL};
  EXPECT_EQ(client_->kind(), TransportKind::CXL);
  EXPECT_EQ(client_->peerIP(), declaredPeer.toFollyIP());
  EXPECT_NE(client_->describe().find("CXL(peer="), std::string::npos);
}

TEST_F(TestCxlSocket, ConcurrentCloseIsIdempotent) {
  const auto before = client_->metrics()->snapshot().retiredLanes;
  std::thread closer([socket = client_] { socket->close(); });
  client_->close();
  closer.join();
  EXPECT_TRUE(client_->closed());
  EXPECT_EQ(client_->fd(), -1);
  EXPECT_EQ(client_->metrics()->snapshot().retiredLanes, before + 1);
  ASSERT_ERROR(client_->check(), RPCCode::kSocketClosed);
}

}  // namespace hf3fs::net::cxl
