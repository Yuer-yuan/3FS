#include <algorithm>
#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <gtest/gtest.h>
#include <memory>
#include <poll.h>
#include <stdexcept>
#include <string>
#include <string_view>
#include <sys/epoll.h>
#include <unistd.h>
#include <vector>

#include "common/net/cxl/CxlConnectService.h"
#include "common/utils/Size.h"
#include "tests/GtestHelpers.h"

namespace hf3fs::net::cxl {
namespace {

using namespace std::chrono_literals;

constexpr uint64_t kTestRegionBytes = 1_MB;
constexpr std::string_view kReceipt = "run-token=connect-test\nmanifest=abcdef0123456789\n";

class ConnectTemporaryFile {
 public:
  ConnectTemporaryFile(std::string_view prefix, std::string_view contents = {}) {
    std::array<char, 64> name{};
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
        throw std::runtime_error("write receipt failed");
      }
      offset += static_cast<size_t>(written);
    }
  }

  ~ConnectTemporaryFile() {
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

class TestCxlConnect : public ::testing::Test {
 protected:
  void SetUp() override {
    ASSERT_EQ(::ftruncate(regionFile_.fd(), kTestRegionBytes), 0);
    auto authority = CxlFabric::start(fabricConfig(CxlFabric::StartMode::InitializeAuthority, EndpointId{1}, 0));
    ASSERT_OK(authority);
    authority_ = std::move(*authority);
    auto client = CxlFabric::start(fabricConfig(CxlFabric::StartMode::Attach, EndpointId{2}, 0x3));
    ASSERT_OK(client);
    clientFabric_ = std::move(*client);
    auto server = CxlFabric::start(fabricConfig(CxlFabric::StartMode::Attach, EndpointId{3}, 0x1));
    ASSERT_OK(server);
    serverFabric_ = std::move(*server);

    service_ = std::make_unique<CxlConnectService>(
        serverFabric_,
        [this](std::unique_ptr<CxlSocket> socket, ServicePlane plane) -> Result<Void> {
          serverSocket_ = std::move(socket);
          acceptedPlane_ = plane;
          return Void{};
        });
  }

  void TearDown() override {
    if (clientSocket_) {
      clientSocket_->close();
    }
    if (serverSocket_) {
      serverSocket_->close();
    }
    service_.reset();
    if (clientFabric_) {
      (void)clientFabric_->stopAndJoin();
    }
    if (serverFabric_) {
      (void)serverFabric_->stopAndJoin();
    }
    if (authority_) {
      (void)authority_->stopAndJoin();
    }
  }

  CxlLayoutManifest manifest() const {
    CxlLayoutManifest value{
        .totalRegionBytes = kTestRegionBytes,
        .sessionGeneration = 59,
        .endpointCount = 3,
        .laneCount = 4,
        .authorityEndpoint = EndpointId{1},
        .authorityGeneration = 23,
    };
    for (size_t index = 0; index < value.manifestSha256.size(); ++index) {
      value.manifestSha256[index] = static_cast<std::byte>(0x90U + index);
    }
    constexpr std::array<uint64_t, kCxlRangeCount> lengths{
        4_KB,
        4_KB,
        64_KB,
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

  CxlFabric::Config fabricConfig(CxlFabric::StartMode mode, EndpointId endpoint, uint64_t capabilities) const {
    return CxlFabric::Config{
        .mode = mode,
        .regionType = CxlFabric::RegionType::File,
        .regionPath = regionFile_.path(),
        .regionLength = kTestRegionBytes,
        .manifest = manifest(),
        .endpoint = endpoint,
        .endpointGeneration = 1,
        .capabilityBits = capabilities,
        .attachTimeout = 20ms,
        .heartbeatInterval = 1_ms,
        .authorityStaleTimeout = 50_ms,
        .shutdownTimeout = 50_ms,
        .authorityOwnerLock = mode == CxlFabric::StartMode::InitializeAuthority ? lockFile_.path() : "",
        .authorityReceipt = mode == CxlFabric::StartMode::InitializeAuthority ? std::string(kReceipt) : "",
    };
  }

  CxlConnectReq validRequest() const {
    CxlConnectReq req;
    req.session_generation = manifest().sessionGeneration;
    req.requester_endpoint = 2;
    req.requester_endpoint_generation = 1;
    req.requester_address = Address{0x0200007fU, 9102, Address::CXL};
    req.target_endpoint = 3;
    req.service_plane = static_cast<uint8_t>(ServicePlane::Data);
    req.queue_depth = 4;
    req.cell_bytes = 256;
    req.capability_bits = 0x1;
    req.connection_nonce.low = 0x0807060504030201ULL;
    req.connection_nonce.high = 0x100f0e0d0c0b0a09ULL;
    return req;
  }

  CxlConnectRsp connectPair() {
    auto req = validRequest();
    auto rsp = service_->connectForTest(req);
    EXPECT_TRUE(rsp.hasValue()) << (rsp ? "" : rsp.error().describe());
    if (!rsp) {
      return {};
    }
    auto client =
        CxlConnectService::finishRequester(clientFabric_, req, *rsp, Address{0x0300007fU, 9103, Address::CXL});
    EXPECT_TRUE(client.hasValue()) << (client ? "" : client.error().describe());
    if (client) {
      clientSocket_ = std::move(*client);
      auto activated = service_->activateForTest(CxlConnectService::activationRequest(*rsp));
      EXPECT_TRUE(activated.hasValue()) << (activated ? "" : activated.error().describe());
    }
    return *rsp;
  }

  ConnectTemporaryFile regionFile_{"hf3fs-connect"};
  ConnectTemporaryFile lockFile_{"hf3fs-connect-owner", kReceipt};
  std::shared_ptr<CxlFabric> authority_;
  std::shared_ptr<CxlFabric> clientFabric_;
  std::shared_ptr<CxlFabric> serverFabric_;
  std::unique_ptr<CxlConnectService> service_;
  std::shared_ptr<CxlSocket> clientSocket_;
  std::shared_ptr<CxlSocket> serverSocket_;
  std::optional<ServicePlane> acceptedPlane_;
};

TEST_F(TestCxlConnect, RejectsWrongSessionGeneration) {
  auto req = validRequest();
  ++req.session_generation;
  ASSERT_ERROR(service_->connectForTest(req), RPCCode::kDataPlaneHandshakeFailed);
  EXPECT_FALSE(serverSocket_);
}

TEST_F(TestCxlConnect, RejectsWrongEndpointPlaneNonceAndCapability) {
  auto wrongEndpoint = validRequest();
  wrongEndpoint.target_endpoint = 2;
  ASSERT_ERROR(service_->connectForTest(wrongEndpoint), RPCCode::kDataPlaneHandshakeFailed);

  auto wrongPlane = validRequest();
  wrongPlane.service_plane = 9;
  ASSERT_ERROR(service_->connectForTest(wrongPlane), RPCCode::kDataPlaneHandshakeFailed);

  auto zeroNonce = validRequest();
  zeroNonce.connection_nonce = {};
  ASSERT_ERROR(service_->connectForTest(zeroNonce), RPCCode::kDataPlaneHandshakeFailed);

  auto unsupportedCapability = validRequest();
  unsupportedCapability.capability_bits = 0x4;
  ASSERT_ERROR(service_->connectForTest(unsupportedCapability), RPCCode::kDataPlaneHandshakeFailed);
}

TEST_F(TestCxlConnect, RejectsDuplicateActiveEndpointGenerationNonceTuple) {
  const auto req = validRequest();
  ASSERT_OK(service_->connectForTest(req));
  ASSERT_ERROR(service_->connectForTest(req), StatusCode::kQueueConflict);
}

TEST_F(TestCxlConnect, DifferentAcceptorsOwnDisjointDeterministicLanes) {
  auto serverRequest = validRequest();
  auto serverResponse = service_->connectForTest(serverRequest);
  ASSERT_OK(serverResponse);

  CxlConnectService clientEndpointService(clientFabric_, {});
  auto clientRequest = validRequest();
  clientRequest.requester_endpoint = 3;
  clientRequest.requester_address = Address{0x0300007fU, 9103, Address::CXL};
  clientRequest.target_endpoint = 2;
  clientRequest.connection_nonce.low++;
  auto clientResponse = clientEndpointService.connectForTest(clientRequest);
  ASSERT_OK(clientResponse);

  EXPECT_EQ(serverResponse->lane_id, 3);
  EXPECT_EQ(clientResponse->lane_id, 2);
  EXPECT_NE(serverResponse->requester_owner_record_offset, clientResponse->requester_owner_record_offset);
}

TEST_F(TestCxlConnect, ResponseBindsExactPlaneGeometryEndpointsAndNonce) {
  const auto req = validRequest();
  auto rsp = service_->connectForTest(req);
  ASSERT_OK(rsp);
  EXPECT_EQ(rsp->session_generation, req.session_generation);
  EXPECT_EQ(rsp->requester_endpoint, req.requester_endpoint);
  EXPECT_EQ(rsp->requester_endpoint_generation, req.requester_endpoint_generation);
  EXPECT_EQ(rsp->target_endpoint, req.target_endpoint);
  EXPECT_EQ(rsp->target_endpoint_generation, 1);
  EXPECT_EQ(rsp->service_plane, req.service_plane);
  EXPECT_EQ(rsp->queue_depth, req.queue_depth);
  EXPECT_EQ(rsp->cell_bytes, req.cell_bytes);
  EXPECT_EQ(rsp->connection_nonce, req.connection_nonce);
  EXPECT_EQ(rsp->capability_bits, 0x1);
  EXPECT_FALSE(acceptedPlane_);
}

TEST_F(TestCxlConnect, AcceptorIsHiddenUntilRequesterOwnerIsReady) {
  const auto req = validRequest();
  auto rsp = service_->connectForTest(req);
  ASSERT_OK(rsp);
  EXPECT_FALSE(serverSocket_);

  const auto activation = CxlConnectService::activationRequest(*rsp);
  ASSERT_ERROR(service_->activateForTest(activation), RPCCode::kDataPlaneHandshakeFailed);
  EXPECT_FALSE(serverSocket_);

  auto client = CxlConnectService::finishRequester(clientFabric_, req, *rsp, Address{0x0300007fU, 9103, Address::CXL});
  ASSERT_OK(client);
  clientSocket_ = std::move(*client);
  auto activated = service_->activateForTest(activation);
  ASSERT_OK(activated);
  EXPECT_TRUE(activated->activated);
  EXPECT_TRUE(serverSocket_);
  EXPECT_EQ(acceptedPlane_, ServicePlane::Data);
  EXPECT_NE(clientSocket_->bulkTransfer(), nullptr);
  EXPECT_NE(serverSocket_->bulkTransfer(), nullptr);
}

TEST_F(TestCxlConnect, TamperedResponseCannotAttachRequester) {
  auto req = validRequest();
  auto rsp = service_->connectForTest(req);
  ASSERT_OK(rsp);
  ++rsp->lane_generation;
  ASSERT_ERROR(CxlConnectService::finishRequester(clientFabric_, req, *rsp, Address{0x0300007fU, 9103, Address::CXL}),
               RPCCode::kDataPlaneHandshakeFailed);
}

TEST_F(TestCxlConnect, BootstrapCarriesNoServingPayload) {
  auto rsp = connectPair();
  ASSERT_NE(rsp.lane_id, 0);
  ASSERT_TRUE(clientSocket_);
  ASSERT_TRUE(serverSocket_);

  const std::string request = "payload over cxl";
  iovec requestVector{.iov_base = const_cast<char *>(request.data()), .iov_len = request.size()};
  ASSERT_RESULT_EQ(request.size(), clientSocket_->send(&requestVector, 1));
  ASSERT_OK(clientSocket_->flush());
  std::array<uint8_t, 64> serverBuffer{};
  ASSERT_RESULT_EQ(request.size(), serverSocket_->recv(folly::MutableByteRange(serverBuffer.data(), request.size())));
  EXPECT_TRUE(std::equal(request.begin(), request.end(), serverBuffer.begin()));

  const std::string response = "response over cxl";
  iovec responseVector{.iov_base = const_cast<char *>(response.data()), .iov_len = response.size()};
  ASSERT_RESULT_EQ(response.size(), serverSocket_->send(&responseVector, 1));
  ASSERT_OK(serverSocket_->flush());
  std::array<uint8_t, 64> clientBuffer{};
  ASSERT_RESULT_EQ(response.size(), clientSocket_->recv(folly::MutableByteRange(clientBuffer.data(), response.size())));
  EXPECT_TRUE(std::equal(response.begin(), response.end(), clientBuffer.begin()));

  const auto bootstrap = service_->metrics()->snapshot();
  EXPECT_EQ(bootstrap.metadataRequests, 2);
  EXPECT_GT(bootstrap.metadataBytes, 0);
  EXPECT_EQ(bootstrap.bootstrapServingBytes, 0);
  EXPECT_GT(clientSocket_->metrics()->snapshot().acceptedBytes, 0);
  EXPECT_GT(serverSocket_->metrics()->snapshot().acceptedBytes, 0);
}

TEST_F(TestCxlConnect, GracefulCloseReusesLaneWithNewGeneration) {
  const auto first = connectPair();
  ASSERT_NE(first.lane_id, 0);

  clientSocket_->close();
  clientSocket_.reset();
  serverSocket_->close();
  serverSocket_.reset();

  const auto req = validRequest();
  auto second = service_->connectForTest(req);
  ASSERT_OK(second);
  EXPECT_EQ(second->lane_id, first.lane_id);
  EXPECT_EQ(second->lane_generation, first.lane_generation + 1U);

  auto client =
      CxlConnectService::finishRequester(clientFabric_, req, *second, Address{0x0300007fU, 9103, Address::CXL});
  ASSERT_OK(client);
  clientSocket_ = std::move(*client);
  ASSERT_OK(service_->activateForTest(CxlConnectService::activationRequest(*second)));
  ASSERT_TRUE(serverSocket_);
}

TEST_F(TestCxlConnect, BootstrapPreservesUnpublishedBytesAcrossLaneReuse) {
  const auto &ranges = serverFabric_->layout().ranges();
  const auto &payloadRange = ranges[static_cast<size_t>(CxlRangeKind::RpcPayloadCells) - 1U];
  const auto &frameRange = ranges[static_cast<size_t>(CxlRangeKind::FrameRings) - 1U];
  auto payload = serverFabric_->region().checkedRange(payloadRange.offset, payloadRange.length, kCxlCacheLineBytes);
  auto frames = serverFabric_->region().checkedRange(frameRange.offset, frameRange.length, kCxlCacheLineBytes);
  ASSERT_OK(payload);
  ASSERT_OK(frames);
  std::fill(payload->begin(), payload->end(), std::byte{0xa5});
  std::fill(frames->begin(), frames->end(), std::byte{0x5a});

  uint64_t previousGeneration = 0;
  for (size_t incarnation = 0; incarnation < 2; ++incarnation) {
    const std::vector<std::byte> oldPayload(payload->begin(), payload->end());
    const std::vector<std::byte> oldFrames(frames->begin(), frames->end());
    const auto descriptor = connectPair();
    ASSERT_NE(descriptor.lane_id, 0);
    ASSERT_GT(descriptor.lane_generation, previousGeneration);
    previousGeneration = descriptor.lane_generation;
    // Bootstrap must not touch unpublished cells, including valid frames
    // left by an earlier generation. Only publication makes bytes readable.
    EXPECT_TRUE(std::equal(payload->begin(), payload->end(), oldPayload.begin()));
    EXPECT_TRUE(std::equal(frames->begin(), frames->end(), oldFrames.begin()));
    for (auto [sender, receiver] : {std::pair{clientSocket_.get(), serverSocket_.get()},
                                    std::pair{serverSocket_.get(), clientSocket_.get()}}) {
      std::array<uint8_t, 64> buffer;
      buffer.fill(0x7e);
      ASSERT_RESULT_EQ(0, receiver->recv(folly::MutableByteRange(buffer.data(), buffer.size())));
      const std::string message = incarnation == 0 ? "previous generation payload" : "new";
      iovec vector{const_cast<char *>(message.data()), message.size()};
      ASSERT_RESULT_EQ(message.size(), sender->send(&vector, 1));
      ASSERT_OK(sender->flush());
      ASSERT_RESULT_EQ(message.size(), receiver->recv(folly::MutableByteRange(buffer.data(), buffer.size())));
      EXPECT_TRUE(std::equal(message.begin(), message.end(), buffer.begin()));
      EXPECT_TRUE(std::all_of(buffer.begin() + message.size(), buffer.end(), [](uint8_t byte) { return byte == 0x7e; }));
      ASSERT_RESULT_EQ(0, receiver->recv(folly::MutableByteRange(buffer.data(), buffer.size())));
    }
    clientSocket_->close();
    clientSocket_.reset();
    serverSocket_->close();
    serverSocket_.reset();
  }
}

TEST_F(TestCxlConnect, OneSidedCloseKeepsLaneQuarantined) {
  const auto first = connectPair();
  ASSERT_NE(first.lane_id, 0);

  serverSocket_->close();
  serverSocket_.reset();

  auto next = validRequest();
  ++next.connection_nonce.low;
  ASSERT_ERROR(service_->connectForTest(next), StatusCode::kQueueFull);

  clientSocket_->close();
  clientSocket_.reset();
  ASSERT_OK(service_->connectForTest(next));
}

TEST_F(TestCxlConnect, CanceledPendingActivationReusesOnlyMatchingRetiredGeneration) {
  auto req = validRequest();
  auto first = service_->connectForTest(req);
  ASSERT_OK(first);
  auto requester =
      CxlConnectService::finishRequester(clientFabric_, req, *first, Address{0x0300007fU, 9103, Address::CXL});
  ASSERT_OK(requester);
  (*requester)->close();  // Cancellation before the activation RPC.
  EXPECT_FALSE(serverSocket_);

  auto second = service_->connectForTest(req);
  ASSERT_OK(second);
  EXPECT_EQ(second->lane_id, first->lane_id);
  EXPECT_EQ(second->lane_generation, first->lane_generation + 1);
  ASSERT_ERROR(service_->activateForTest(CxlConnectService::activationRequest(*first)),
               RPCCode::kDataPlaneHandshakeFailed);
  // The old terminal owner record cannot cancel this new pending generation.
  auto competing = req;
  ++competing.connection_nonce.low;
  ASSERT_ERROR(service_->connectForTest(competing), StatusCode::kQueueFull);
  auto attached =
      CxlConnectService::finishRequester(clientFabric_, req, *second, Address{0x0300007fU, 9103, Address::CXL});
  ASSERT_OK(attached);
  clientSocket_ = std::move(*attached);
  ASSERT_ERROR(CxlConnectService::faultRequester(clientFabric_, *first), RPCCode::kStaleGeneration);
  ASSERT_OK(service_->activateForTest(CxlConnectService::activationRequest(*second)));
}

TEST_F(TestCxlConnect, FaultedRequesterCancelsPendingActivation) {
  auto req = validRequest();
  auto first = service_->connectForTest(req);
  ASSERT_OK(first);
  auto attached =
      CxlConnectService::finishRequester(clientFabric_, req, *first, Address{0x0300007fU, 9103, Address::CXL});
  ASSERT_OK(attached);
  clientSocket_ = std::move(*attached);
  ASSERT_OK(CxlConnectService::faultRequester(clientFabric_, *first));
  clientSocket_->close();
  clientSocket_.reset();
  auto second = service_->connectForTest(req);
  ASSERT_OK(second);
  EXPECT_EQ(second->lane_id, first->lane_id);
  EXPECT_EQ(second->lane_generation, first->lane_generation + 1);
}

TEST_F(TestCxlConnect, PeerCloseWakesIdleSocketBeforeLaneReuse) {
  const auto first = connectPair();
  ASSERT_NE(first.lane_id, 0);
  ASSERT_OK(serverSocket_->poll(0));
  clientSocket_->close();

  pollfd event{serverSocket_->fd(), POLLIN, 0};
  ASSERT_EQ(::poll(&event, 1, 1000), 1);
  ASSERT_ERROR(serverSocket_->poll(EPOLLIN), RPCCode::kSocketClosed);

  // Transport handles the error on its I/O worker and destroys the socket.
  // The progress thread must not reclaim a lane while that worker can use it.
  auto next = validRequest();
  ++next.connection_nonce.low;
  ASSERT_ERROR(service_->connectForTest(next), StatusCode::kQueueFull);
  serverSocket_->close();
  auto second = service_->connectForTest(next);
  ASSERT_OK(second);
  EXPECT_EQ(second->lane_id, first.lane_id);
  EXPECT_EQ(second->lane_generation, first.lane_generation + 1);
}

TEST_F(TestCxlConnect, DrainsPublishedReplyBeforePeerClose) {
  const auto descriptor = connectPair();
  ASSERT_NE(descriptor.lane_id, 0);
  const std::string reply = "last reply before close";
  iovec vector{const_cast<char *>(reply.data()), reply.size()};
  ASSERT_RESULT_EQ(reply.size(), serverSocket_->send(&vector, 1));
  ASSERT_OK(serverSocket_->flush());
  serverSocket_->close();

  ASSERT_OK(clientSocket_->poll(0));
  std::array<uint8_t, 64> buffer{};
  ASSERT_RESULT_EQ(reply.size(), clientSocket_->recv(folly::MutableByteRange(buffer.data(), buffer.size())));
  EXPECT_TRUE(std::equal(reply.begin(), reply.end(), buffer.begin()));
  ASSERT_ERROR(clientSocket_->poll(0), RPCCode::kSocketClosed);
}

}  // namespace hf3fs::net::cxl
