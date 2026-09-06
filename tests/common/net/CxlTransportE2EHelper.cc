#include <algorithm>
#include <array>
#include <chrono>
#include <csignal>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <fcntl.h>
#include <filesystem>
#include <folly/experimental/coro/BlockingWait.h>
#include <folly/experimental/coro/Invoke.h>
#include <folly/init/Init.h>
#include <fstream>
#include <iostream>
#include <optional>
#include <set>
#include <spawn.h>
#include <string>
#include <string_view>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>
#include <vector>

#include "common/net/BulkControl.h"
#include "common/net/BulkTransfer.h"
#include "common/net/Client.h"
#include "common/net/Server.h"
#include "common/net/TransportRuntime.h"
#include "common/net/cxl/CxlBuffer.h"
#include "common/net/cxl/CxlFabric.h"
#include "common/utils/Size.h"
#include "tests/common/net/Echo.h"

extern char **environ;

namespace hf3fs::net::test {
namespace {

using namespace std::chrono_literals;

constexpr uint64_t kRegionBytes = 16_MB;
constexpr uint32_t kQueueDepth = 8;
constexpr uint32_t kCellBytes = 64_KB;
constexpr std::string_view kReceipt = "run-token=transport-e2e\nmanifest=9d9e9fa0a1a2a3a4\n";

class BulkControlCallbackService : public serde::ServiceWrapper<BulkControlCallbackService, BulkControl> {
 public:
  CoTryTask<BulkTransmissionRsp> apply(serde::CallContext &ctx, const BulkTransmissionReq &req) {
    if (ctx.transport()->kind() != TransportKind::CXL) {
      co_return makeError(StatusCode::kInvalidArg, "bulk callback requires CXL");
    }
    // Preserve TestBulkControl.Normal's reverse RPC on the incoming transport.
    serde::ClientContext callback(ctx.transport());
    auto result = co_await BulkControl<>::apply(callback, req);
    if (result) {
      std::cout << "HF3FS_CXL_BULK_CALLBACK_OK transport=CXL" << std::endl;
    }
    co_return result;
  }
};

class EchoServiceImpl : public serde::ServiceWrapper<EchoServiceImpl, Echo> {
 public:
  CoTryTask<EchoRsp> echo(serde::CallContext &, const EchoReq &req) {
    EchoRsp rsp;
    rsp.val = req.val;
    co_return rsp;
  }

  CoTryTask<HelloRsp> hello(serde::CallContext &, const HelloReq &req) {
    HelloRsp rsp;
    rsp.val = "Hello, " + req.val;
    rsp.idx = ++idx_;
    co_return rsp;
  }

  CoTryTask<HelloRsp> fail(serde::CallContext &, const HelloReq &) {
    co_return makeError(RPCCode::kInvalidMessageType, "failed");
  }

 private:
  uint32_t idx_{};
};

struct BulkRoundTripReq {
  SERDE_STRUCT_FIELD(remote, RemoteBufferHandle{});
  SERDE_STRUCT_FIELD(mask, uint8_t{});
};

struct BulkRoundTripRsp {
  SERDE_STRUCT_FIELD(checksum, uint64_t{});
};

SERDE_SERVICE(CxlBulkE2E, 13) { SERDE_SERVICE_METHOD(roundTrip, 1, BulkRoundTripReq, BulkRoundTripRsp); };

class BulkRoundTripService : public serde::ServiceWrapper<BulkRoundTripService, CxlBulkE2E> {
 public:
  CoTryTask<BulkRoundTripRsp> roundTrip(serde::CallContext &ctx, const BulkRoundTripReq &req) {
    if (ctx.transport()->kind() != TransportKind::CXL || ctx.transport()->bulkTransfer() == nullptr ||
        req.remote.length == 0 || req.remote.length > 64_KB) {
      co_return makeError(StatusCode::kInvalidArg, "invalid CXL bulk E2E request");
    }
    auto local = SharedBuffer::allocateHeap(req.remote.length);
    CO_RETURN_ON_ERROR(local);
    std::array<SharedBuffer, 1> buffers{*local};
    auto pulled = co_await ctx.transport()->bulkTransfer()->pull(req.remote, buffers);
    CO_RETURN_ON_ERROR(pulled);
    uint64_t checksum = 0;
    for (size_t index = 0; index < local->size(); ++index) {
      local->data()[index] ^= req.mask;
      checksum += local->data()[index];
    }
    auto pushed = co_await ctx.transport()->bulkTransfer()->push(req.remote, buffers);
    CO_RETURN_ON_ERROR(pushed);
    co_return BulkRoundTripRsp{checksum};
  }
};

cxl::CxlLayoutManifest manifest() {
  cxl::CxlLayoutManifest value{
      .totalRegionBytes = kRegionBytes,
      .sessionGeneration = 61,
      .endpointCount = 3,
      .laneCount = 6,
      .authorityEndpoint = cxl::EndpointId{1},
      .authorityGeneration = 29,
  };
  for (size_t index = 0; index < value.manifestSha256.size(); ++index) {
    value.manifestSha256[index] = static_cast<std::byte>(0x40U + index);
  }
  constexpr std::array<uint64_t, cxl::kCxlRangeCount> lengths{
      4_KB,
      12_KB,
      96_KB,
      6_KB,
      6_MB,
      4_KB,
      4_MB,
      4_KB,
  };
  uint64_t offset = cxl::kCxlSuperblockBytes;
  for (size_t index = 0; index < value.ranges.size(); ++index) {
    value.ranges[index] = cxl::CxlRange{static_cast<cxl::CxlRangeKind>(index + 1U), offset, lengths[index]};
    offset += lengths[index];
  }
  value.lifecycleRecordOffset = value.ranges.back().offset;
  return value;
}

cxl::CxlFabric::Config fabricConfig(cxl::CxlFabric::StartMode mode,
                                    cxl::EndpointId endpoint,
                                    const std::string &region,
                                    const std::string &ownerLock = {}) {
  return cxl::CxlFabric::Config{
      .mode = mode,
      .regionType = cxl::CxlFabric::RegionType::File,
      .regionPath = region,
      .regionLength = kRegionBytes,
      .manifest = manifest(),
      .endpoint = endpoint,
      .endpointGeneration = 1,
      .capabilityBits = 1,
      .attachTimeout = 2s,
      .heartbeatInterval = 5_ms,
      .authorityStaleTimeout = 2_s,
      .shutdownTimeout = 2_s,
      .authorityOwnerLock = ownerLock,
      .authorityReceipt = mode == cxl::CxlFabric::StartMode::InitializeAuthority ? std::string(kReceipt) : "",
  };
}

bool writeText(const std::filesystem::path &path, std::string_view contents) {
  std::ofstream output(path);
  output << contents;
  return output.good();
}

bool waitForFile(const std::filesystem::path &path, std::chrono::seconds timeout) {
  const auto deadline = std::chrono::steady_clock::now() + timeout;
  while (std::chrono::steady_clock::now() < deadline) {
    std::error_code error;
    if (std::filesystem::exists(path, error)) {
      return true;
    }
    std::this_thread::sleep_for(10ms);
  }
  return false;
}

int runAuthority(const std::vector<std::string> &args) {
  if (args.size() != 4) {
    return 2;
  }
  auto fabric = cxl::CxlFabric::start(
      fabricConfig(cxl::CxlFabric::StartMode::InitializeAuthority, cxl::EndpointId{1}, args[0], args[1]));
  if (!fabric) {
    std::cerr << fabric.error().describe() << '\n';
    return 3;
  }
  if (!writeText(args[2], "ready\n")) {
    return 4;
  }
  if (!waitForFile(args[3], 20s)) {
    return 5;
  }
  auto stopped = (*fabric)->stopAndJoin();
  if (!stopped) {
    std::cerr << stopped.error().describe() << '\n';
    return 6;
  }
  return 0;
}

int runServer(const std::vector<std::string> &args, bool bulkControl) {
  if (args.size() != 3) {
    return 2;
  }
  auto runtime =
      TransportRuntime::startCxl(fabricConfig(cxl::CxlFabric::StartMode::Attach, cxl::EndpointId{3}, args[0]));
  if (!runtime) {
    std::cerr << runtime.error().describe() << '\n';
    return 3;
  }

  int result = 0;
  {
    Server::Config config;
    auto &group = config.groups(0);
    group.set_network_type(Address::CXL);
    group.set_service_plane(std::optional<ServicePlane>{ServicePlane::Data});
    group.listener().set_listen_port(0);
    group.listener().set_filter_list(std::set<std::string>{"lo"});
    group.listener().set_custom_tcp_nic_prefix("lo");
    group.io_worker().cxlsocket().set_queue_depth(kQueueDepth);
    group.io_worker().cxlsocket().set_cell_bytes(kCellBytes);

    Server server(config);
    auto added = server.addSerdeService(std::make_unique<EchoServiceImpl>());
    if (added) {
      added = server.addSerdeService(std::make_unique<BulkRoundTripService>());
    }
    if (added && bulkControl) {
      added = server.addSerdeService(std::make_unique<BulkControlCallbackService>());
    }
    auto setup = added ? server.setup() : Result<Void>{makeError(std::move(added.error()))};
    auto started = setup ? server.start() : Result<Void>{makeError(std::move(setup.error()))};
    if (!started) {
      std::cerr << started.error().describe() << '\n';
      result = 4;
    } else {
      const auto address = server.groups().front()->addressList().front();
      if (!writeText(args[1], address.str() + "\n")) {
        result = 5;
      } else if (!waitForFile(args[2], 20s)) {
        result = 6;
      }
    }
    server.stopAndJoin();
  }
  auto stopped = TransportRuntime::stopCxl();
  if (!stopped && result == 0) {
    std::cerr << stopped.error().describe() << '\n';
    result = 7;
  }
  return result;
}

int runClient(const std::vector<std::string> &args, bool bulkControl) {
  if (args.size() != 2) {
    return 2;
  }
  const auto serverAddress = Address::fromString(args[1]);
  if (!serverAddress.isCXL() || serverAddress.ip == 0 || serverAddress.port == 0) {
    return 3;
  }
  const Address localAddress{serverAddress.ip, static_cast<uint16_t>(serverAddress.port + 1U), Address::CXL};
  auto clientFabricConfig = fabricConfig(cxl::CxlFabric::StartMode::Attach, cxl::EndpointId{2}, args[0]);
  clientFabricConfig.endpointGeneration = 0;  // claim the next retired incarnation
  auto runtime =
      TransportRuntime::startCxl(clientFabricConfig,
                                 localAddress,
                                 {CxlRoute{ServiceEndpoint{serverAddress, ServicePlane::Data}, cxl::EndpointId{3}}});
  if (!runtime) {
    std::cerr << runtime.error().describe() << '\n';
    return 4;
  }

  int result = 0;
  {
    Client::Config config;
    config.set_default_timeout(5_s);
    config.io_worker().cxlsocket().set_queue_depth(kQueueDepth);
    config.io_worker().cxlsocket().set_cell_bytes(kCellBytes);
    config.set_enable_bulk_control(true);
    Client client(config, "CxlE2E");
    auto started = client.start("CxlE2E");
    if (!started) {
      std::cerr << started.error().describe() << '\n';
      result = 5;
    } else {
      auto context = client.serdeCtx(serverAddress);
      if (bulkControl) {
        auto callback = folly::coro::blockingWait(BulkControl<>::apply(context, BulkTransmissionReq{}));
        if (!callback) {
          std::cerr << callback.error().describe() << '\n';
          result = 12;
        } else {
          std::cout << "HF3FS_CXL_BULK_CONTROL_OK transport=CXL" << std::endl;
        }
      } else {
        auto rpc = folly::coro::blockingWait(folly::coro::co_invoke([&]() -> CoTryTask<EchoRsp> {
          Echo<> echo;
          EchoReq request;
          request.val = "transport-pool -> tcp-bootstrap -> cxl-lane";
          co_return co_await echo.echo(context, request);
        }));
        if (!rpc || rpc->val != "transport-pool -> tcp-bootstrap -> cxl-lane") {
          if (!rpc) {
            std::cerr << rpc.error().describe() << '\n';
          }
          result = 6;
        }
        if (result == 0) {
          auto sequentialRpc = folly::coro::blockingWait(folly::coro::co_invoke([&]() -> CoTryTask<EchoRsp> {
            Echo<> echo;
            EchoReq request;
            request.val = "sequential-cxl-rpc";
            co_return co_await echo.echo(context, request);
          }));
          if (!sequentialRpc || sequentialRpc->val != "sequential-cxl-rpc") {
            if (!sequentialRpc) {
              std::cerr << sequentialRpc.error().describe() << '\n';
            }
            result = 8;
          }
        }
        auto arena = TransportRuntime::cxlBufferArena();
        auto remote =
            arena ? arena->tryAllocate(4096) : Result<SharedBuffer>{makeError(RPCCode::kDataPlaneNotInitialized)};
        if (result == 0 && !remote) {
          std::cerr << remote.error().describe() << '\n';
          result = 9;
        }
        if (result == 0) {
          uint64_t expectedChecksum = 0;
          for (size_t index = 0; index < remote->size(); ++index) {
            remote->data()[index] = static_cast<uint8_t>(index & 0xffU);
            expectedChecksum += static_cast<uint8_t>(remote->data()[index] ^ 0x5aU);
          }
          auto exported = remote->exportRemote(RemoteAccess::ReadWrite);
          if (!exported) {
            std::cerr << exported.error().describe() << '\n';
            result = 10;
          } else {
            BulkRoundTripReq request{exported->handle(), 0x5aU};
            UserRequestOptions options;
            options.timeout = 5_s;
            options.requestLifetime = std::make_shared<RemoteExportLease>(exported->takeLease());
            auto bulkRpc = folly::coro::blockingWait(CxlBulkE2E<>::roundTrip(context, request, &options));
            bool matches = true;
            for (size_t index = 0; index < remote->size(); ++index) {
              matches = matches && remote->data()[index] == static_cast<uint8_t>((index & 0xffU) ^ 0x5aU);
            }
            if (!bulkRpc || bulkRpc->checksum != expectedChecksum || !matches) {
              if (!bulkRpc) {
                std::cerr << bulkRpc.error().describe() << '\n';
              }
              result = 11;
            }
          }
        }
      }
    }
    client.stopAndJoin();
  }
  auto stopped = TransportRuntime::stopCxl();
  if (!stopped && result == 0) {
    std::cerr << stopped.error().describe() << '\n';
    result = 7;
  }
  return result;
}

class TemporaryPath {
 public:
  TemporaryPath(std::string_view prefix, bool keepFile) {
    std::array<char, 96> buffer{};
    const auto pattern = "/tmp/" + std::string(prefix) + "-XXXXXX";
    std::copy(pattern.begin(), pattern.end(), buffer.begin());
    const int fd = ::mkstemp(buffer.data());
    if (fd < 0) {
      return;
    }
    path_ = buffer.data();
    ::close(fd);
    if (!keepFile) {
      ::unlink(path_.c_str());
    }
  }
  ~TemporaryPath() {
    if (!path_.empty() && !preserved_) {
      ::unlink(path_.c_str());
    }
  }
  const std::string &path() const { return path_; }
  void preserve() { preserved_ = true; }

 private:
  std::string path_;
  bool preserved_{};
};

pid_t spawnSelf(const std::string &executable, const std::vector<std::string> &arguments) {
  std::vector<char *> argv;
  argv.reserve(arguments.size() + 2);
  argv.push_back(const_cast<char *>(executable.c_str()));
  for (const auto &argument : arguments) {
    argv.push_back(const_cast<char *>(argument.c_str()));
  }
  argv.push_back(nullptr);
  pid_t child = -1;
  const int error = ::posix_spawn(&child, executable.c_str(), nullptr, nullptr, argv.data(), environ);
  if (error != 0) {
    std::cerr << "posix_spawn: " << std::strerror(error) << '\n';
    return -1;
  }
  return child;
}

int waitChild(pid_t child, std::chrono::seconds timeout) {
  const auto deadline = std::chrono::steady_clock::now() + timeout;
  int status = 0;
  while (std::chrono::steady_clock::now() < deadline) {
    const auto result = ::waitpid(child, &status, WNOHANG);
    if (result == child) {
      return WIFEXITED(status) ? WEXITSTATUS(status) : 128;
    }
    if (result < 0) {
      return 129;
    }
    std::this_thread::sleep_for(10ms);
  }
  ::kill(child, SIGKILL);
  ::waitpid(child, &status, 0);
  return 130;
}

void stopChild(pid_t child) {
  if (child <= 0) {
    return;
  }
  ::kill(child, SIGKILL);
  int status = 0;
  (void)::waitpid(child, &status, 0);
}

bool verifyRetirement(const std::string &regionPath, uint64_t clientGeneration) {
  auto region = cxl::CxlRegion::mapFile(regionPath, kRegionBytes);
  if (!region) {
    return false;
  }
  const auto layout = manifest();
  for (uint32_t endpoint = 1; endpoint <= 3; ++endpoint) {
    auto bytes = region->checkedRange(layout.ranges[0].offset + (endpoint - 1U) * sizeof(cxl::CxlEndpointRecord),
                                      sizeof(cxl::CxlEndpointRecord));
    if (!bytes) {
      return false;
    }
    cxl::CxlEndpointRecord record;
    std::memcpy(&record, bytes->data(), sizeof(record));
    if (cxl::loadLe64(&record.recordSequence) == 0 || (cxl::loadLe64(&record.recordSequence) & 1U) != 0 ||
        cxl::loadLe64(&record.sessionGeneration) != layout.sessionGeneration ||
        cxl::loadLe32(&record.endpointId) != endpoint ||
        cxl::loadLe64(&record.endpointGeneration) != (endpoint == 2 ? clientGeneration : 1) ||
        cxl::loadLe32(&record.lifecycle) != static_cast<uint32_t>(cxl::CxlEndpointLifecycle::Retired) ||
        cxl::loadLe32(&record.recordCrc32c) !=
            cxl::cxlCrc32cWithZeroedU32(record, offsetof(cxl::CxlEndpointRecord, recordCrc32c))) {
      std::cerr << "CXL endpoint failed retirement validation: " << endpoint << '\n';
      return false;
    }
  }
  std::cout << "HF3FS_CXL_FIXTURE_RETIRED endpoints=3 client_generation=" << clientGeneration << std::endl;
  return true;
}

int runOrchestrator(const std::string &executable, bool bulkControl) {
  TemporaryPath region{"hf3fs-cxl-e2e-region", true};
  TemporaryPath ownerLock{"hf3fs-cxl-e2e-owner", true};
  TemporaryPath authorityReady{"hf3fs-cxl-e2e-authority-ready", false};
  TemporaryPath authorityStop{"hf3fs-cxl-e2e-authority-stop", false};
  TemporaryPath serverReady{"hf3fs-cxl-e2e-server-ready", false};
  TemporaryPath serverStop{"hf3fs-cxl-e2e-server-stop", false};
  auto preserve = [&] {
    for (auto *path : {&region, &ownerLock, &authorityReady, &authorityStop, &serverReady, &serverStop}) {
      path->preserve();
      std::cerr << "CXL fixture retained path: " << path->path() << '\n';
    }
  };
  if (region.path().empty() || ownerLock.path().empty() || !writeText(ownerLock.path(), kReceipt)) {
    return 2;
  }
  const int regionFd = ::open(region.path().c_str(), O_RDWR);
  if (regionFd < 0 || ::ftruncate(regionFd, kRegionBytes) != 0) {
    if (regionFd >= 0) {
      ::close(regionFd);
    }
    return 3;
  }
  ::close(regionFd);

  pid_t authority =
      spawnSelf(executable,
                {"authority", region.path(), ownerLock.path(), authorityReady.path(), authorityStop.path()});
  pid_t server = -1;
  auto cleanup = [&] {
    stopChild(server);
    stopChild(authority);
    preserve();
  };
  if (authority <= 0 || !waitForFile(authorityReady.path(), 5s)) {
    cleanup();
    return 4;
  }

  server =
      spawnSelf(executable,
                {bulkControl ? "server-bulk-control" : "server", region.path(), serverReady.path(), serverStop.path()});
  if (server <= 0 || !waitForFile(serverReady.path(), 5s)) {
    cleanup();
    return 5;
  }
  std::ifstream addressInput(serverReady.path());
  std::string serverAddress;
  addressInput >> serverAddress;
  if (serverAddress.empty()) {
    cleanup();
    return 6;
  }

  // The server owns only two lane slots. Repeated process incarnations must
  // reclaim closed lanes, not merely succeed until the initial slots run out.
  int clientStatus = 0;
  const unsigned incarnations = bulkControl ? 1 : 8;
  for (unsigned incarnation = 0; incarnation < incarnations && clientStatus == 0; ++incarnation) {
    const pid_t client =
        spawnSelf(executable, {bulkControl ? "client-bulk-control" : "client", region.path(), serverAddress});
    clientStatus = client > 0 ? waitChild(client, 10s) : 131;
  }
  if (!writeText(serverStop.path(), "stop\n")) {
    cleanup();
    return 7;
  }
  const int serverStatus = waitChild(server, 10s);
  server = -1;
  if (!writeText(authorityStop.path(), "stop\n")) {
    cleanup();
    return 8;
  }
  const int authorityStatus = waitChild(authority, 10s);
  authority = -1;
  if (clientStatus != 0 || serverStatus != 0 || authorityStatus != 0) {
    std::cerr << "CXL E2E child status: client=" << clientStatus << " server=" << serverStatus
              << " authority=" << authorityStatus << '\n';
    preserve();
    return 9;
  }
  if (!verifyRetirement(region.path(), incarnations)) {
    preserve();
    return 10;
  }
  return 0;
}

}  // namespace
}  // namespace hf3fs::net::test

int main(int argc, char **argv) {
  folly::Init init(&argc, &argv);
  if (argc < 2) {
    return 1;
  }
  const std::string role = argv[1];
  std::vector<std::string> args;
  for (int index = 2; index < argc; ++index) {
    args.emplace_back(argv[index]);
  }
  if (role == "orchestrate" || role == "orchestrate-bulk-control") {
    std::array<char, 4096> executable{};
    const auto size = ::readlink("/proc/self/exe", executable.data(), executable.size() - 1U);
    if (size <= 0) {
      return 2;
    }
    executable[static_cast<size_t>(size)] = '\0';
    return hf3fs::net::test::runOrchestrator(executable.data(), role == "orchestrate-bulk-control");
  }
  if (role == "authority") {
    return hf3fs::net::test::runAuthority(args);
  }
  if (role == "server" || role == "server-bulk-control") {
    return hf3fs::net::test::runServer(args, role == "server-bulk-control");
  }
  if (role == "client" || role == "client-bulk-control") {
    return hf3fs::net::test::runClient(args, role == "client-bulk-control");
  }
  return 1;
}
