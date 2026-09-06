#include "tests/lib/CxlStorageTestFixture.h"

#include <cstdlib>
#include <fcntl.h>
#include <fstream>
#include <iostream>
#include <openssl/sha.h>
#include <sys/socket.h>
#include <unistd.h>

#include "common/net/cxl/CxlAbi.h"
#include "tests/lib/Helper.h"
#include "tests/lib/UnitTestFabric.h"

namespace hf3fs::test {
namespace {
using namespace std::chrono_literals;
namespace cxl = net::cxl;

void save(const std::filesystem::path &path, std::string_view bytes) {
  std::ofstream out(path);
  out << bytes;
  out.close();
  if (!out) throw std::runtime_error("cannot write fixture evidence: " + path.string());
}

void prepareGroups(net::Server::Config &config) {
  for (size_t i = 0; i < config.groups_length(); ++i) {
    auto &group = config.groups(i);
    group.set_network_type(i == 0 ? net::Address::CXL : net::Address::TCP);
    group.set_service_plane(i == 0 ? net::ServicePlane::Data : net::ServicePlane::Control);
    group.io_worker().cxlsocket().set_queue_depth(8);
    group.io_worker().cxlsocket().set_cell_bytes(65536);
    group.listener().set_filter_list({"lo"});
    group.listener().set_custom_tcp_nic_prefix("lo");
    group.listener().set_reuse_port(false);
  }
}

void stopProcess(std::unique_ptr<CxlTestProcess> &process) {
  if (!process) return;
  CxlStorageTestRequest request;
  request.command = "stop";
  try {
    cxlTestRequest(*process, request);
    const auto status = process->wait(10s);
    if (status != 0 || process->forcedCleanup()) {
      throw std::runtime_error(fmt::format("service exit {}: {}", status, process->log().string()));
    }
    process.reset();
  } catch (...) {
    process->terminate();
    process.reset();
    throw;
  }
}
}  // namespace

StorageTestServer::StorageTestServer(std::unique_ptr<storage::StorageServer> local)
    : local_(std::move(local)),
      info_(local_->appInfo()) {}

StorageTestServer::StorageTestServer(std::unique_ptr<CxlTestProcess> process, flat::AppInfo info)
    : process_(std::move(process)),
      info_(std::move(info)) {}

StorageTestServer::~StorageTestServer() {
  try {
    stopAndJoin();
  } catch (const std::exception &e) {
    ADD_FAILURE() << "CXL storage teardown: " << e.what();
  }
}

void StorageTestServer::stopAndJoin() {
  if (local_) {
    local_->stopAndJoin();
    local_.reset();
  }
  stopProcess(process_);
}

void StorageTestServer::setFakeRoutingInfo(const std::shared_ptr<flat::RoutingInfo> &routing) {
  if (local_) {
    auto client = RoutingStoreHelper::getMgmtdClient(*local_);
    if (auto *fake = dynamic_cast<FakeMgmtdClient *>(client.get()))
      fake->setRoutingInfo(routing);
    else
      RoutingStoreHelper::setMgmtdClient(*local_, std::make_unique<FakeMgmtdClient>(routing));
    return;
  }
  CxlStorageTestRequest request;
  request.command = "fake-routing";
  request.routing = *routing;
  cxlTestRequest(*process_, request);
}

void StorageTestServer::refreshRoutingInfo() {
  if (local_) {
    RoutingStoreHelper::refreshRoutingInfo(*local_);
    return;
  }
  CxlStorageTestRequest request;
  request.command = "refresh-routing";
  cxlTestRequest(*process_, request);
}

bool StorageTestServer::checkLocalTargetState(flat::LocalTargetState state) {
  if (local_) return TargetMapHelper::checkLocalTargetState(*local_, state);
  CxlStorageTestRequest request;
  request.command = "check-target-state";
  request.targetState = state;
  return cxlTestRequest(*process_, request).value;
}

void StorageTestServer::updateConfig(const storage::StorageServer::Config &config) {
  if (local_) return;  // The original local server references this config.
  CxlStorageTestRequest request;
  request.command = "hot-config";
  request.config = config.toString();
  cxlTestRequest(*process_, request);
}

uint64_t StorageTestServer::startCxl(const net::TransportRuntime::Config &runtime) {
  CxlStorageTestRequest request;
  request.command = "start";
  request.runtime = runtime.toString();
  return cxlTestRequest(*process_, request).generation;
}

CxlStorageTestFixture::CxlStorageTestFixture(uint32_t storageNodes)
    : executable_(std::filesystem::read_symlink("/proc/self/exe").parent_path() / "cxl_storage_test_helper"),
      addresses_(storageNodes + 3),
      generations_(storageNodes + 3) {
  const char *selected = std::getenv("HF3FS_CXL_TEST_ARTIFACT_DIR");
  // Unit tests live in build/tests; storage_bench lives in build/bin.
  if (!std::filesystem::exists(executable_)) {
    executable_ = executable_.parent_path().parent_path() / "tests" / "cxl_storage_test_helper";
  }
  const auto base =
      selected ? std::filesystem::path(selected) : std::filesystem::temp_directory_path() / "hf3fs-cxl-storage-tests";
  std::filesystem::create_directories(base);
  auto pattern = (std::filesystem::absolute(base) / "fixture-XXXXXX").string();
  if (!::mkdtemp(pattern.data())) throw std::runtime_error("cannot create CXL fixture directory");
  directory_ = pattern;
  std::cout << "CXL storage fixture evidence: " << directory_ << std::endl;
  addresses_.front() = reserveAddress();
  addresses_.back() = reserveAddress();
}

CxlStorageTestFixture::~CxlStorageTestFixture() {
  try {
    stop(true);
  } catch (const std::exception &e) {
    ADD_FAILURE() << "CXL fabric teardown: " << e.what();
  }
  for (int fd : reservedSockets_) ::close(fd);
}

net::Address CxlStorageTestFixture::reserveAddress() {
  int fd = ::socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
  if (fd < 0) throw std::runtime_error("cannot reserve fixture address");
  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  socklen_t size = sizeof(address);
  if (::bind(fd, reinterpret_cast<sockaddr *>(&address), size) ||
      ::getsockname(fd, reinterpret_cast<sockaddr *>(&address), &size)) {
    ::close(fd);
    throw std::runtime_error("cannot bind fixture address");
  }
  reservedSockets_.push_back(fd);
  return net::Address{address.sin_addr.s_addr, ntohs(address.sin_port), net::Address::CXL};
}

void CxlStorageTestFixture::prepareMgmtd(mgmtd::MgmtdServer::Config &config) {
  prepareGroups(config.base());
  mgmtd_ = CxlTestProcess::launch(executable_, {"mgmtd"}, directory_ / "mgmtd.log", 60s);
  CxlStorageTestRequest request;
  request.command = "prepare";
  request.config = config.toString();
  request.clusterId = "test";
  request.appInfo.nodeId = flat::NodeId(10000);
  request.appInfo.hostname = "hostname.10000";
  request.appInfo.pid = 1;
  auto reply = cxlTestRequest(*mgmtd_, request);
  mgmtdAddresses_ = reply.appInfo.serviceGroups.front().endpoints;
  addresses_[1] = mgmtdAddresses_.front();
  save(directory_ / "mgmtd.toml", request.config);
}

std::unique_ptr<StorageTestServer> CxlStorageTestFixture::prepareStorage(size_t index,
                                                                         storage::StorageServer::Config &config,
                                                                         flat::NodeId nodeId) {
  prepareGroups(config.base());
  const auto endpoint = index + 3;
  for (auto *client : {&config.client(), &config.forward_client()}) {
    client->io_worker().cxlsocket().set_queue_depth(8);
    client->io_worker().cxlsocket().set_cell_bytes(65536);
  }
  if (started_) {
    // Server setup must bind the same bootstrap address on every incarnation.
    config.base().groups(0).listener().set_listen_port(addresses_.at(endpoint - 1).port);
  }
  const auto name = fmt::format("storage-{}-generation-{}", index, generations_.at(endpoint - 1) + 1);
  auto process = CxlTestProcess::launch(executable_, {"storage"}, directory_ / (name + ".log"), 60s);
  CxlStorageTestRequest request;
  request.command = "prepare";
  request.config = config.toString();
  request.clusterId = "test";
  request.appInfo.nodeId = nodeId;
  auto reply = cxlTestRequest(*process, request);
  reply.appInfo.clusterId = request.clusterId;
  auto proxy = std::make_unique<StorageTestServer>(std::move(process), reply.appInfo);
  if (started_ && proxy->address() != addresses_.at(endpoint - 1)) {
    throw std::runtime_error("restarted storage changed immutable CXL route");
  }
  addresses_.at(endpoint - 1) = proxy->address();
  save(directory_ / (name + ".toml"), request.config);
  if (started_) {
    auto generation = proxy->startCxl(runtimeConfig(endpoint));
    if (generation != generations_.at(endpoint - 1) + 1) throw std::runtime_error("wrong restart generation");
    generations_.at(endpoint - 1) = generation;
  }
  return proxy;
}

net::TransportRuntime::Config CxlStorageTestFixture::runtimeConfig(uint32_t endpoint) const {
  net::TransportRuntime::Config config;
  config.set_enabled(true);
  config.set_region_type(cxl::CxlFabric::RegionType::File);
  config.set_region_path((directory_ / "fabric.raw").string());
  config.set_manifest_path((directory_ / "manifest.json").string());
  config.set_endpoint(endpoint);
  config.set_endpoint_generation(endpoint == 1 ? 1 : 0);
  config.set_capability_bits(1);
  config.set_attach_timeout(10_s);
  config.set_heartbeat_interval(10_ms);
  config.set_authority_stale_timeout(10_s);
  config.set_shutdown_timeout(10_s);
  if (endpoint == 1) {
    config.set_mode(cxl::CxlFabric::StartMode::InitializeAuthority);
    config.set_authority_owner_lock((directory_ / "authority.owner").string());
    config.set_authority_receipt("manifest=" + manifest_.manifestSha256 + "\n");
  }
  return config;
}

void CxlStorageTestFixture::start(const std::vector<std::unique_ptr<StorageTestServer>> &servers,
                                  const std::vector<storage::StorageServer::Config> &configs,
                                  uint32_t clientConnections) {
  manifest_.schema = "hf3fs.cxl-runtime-manifest.v1";
  manifest_.sessionGeneration = 1;
  manifest_.authorityEndpoint = 1;
  manifest_.authorityGeneration = 1;
  manifest_.endpointCount = addresses_.size();
  // Bound each acceptor by all configured outgoing pools. The default client
  // has two pools of 256; forwarding/resync/management retain their own pools.
  uint32_t lanesPerEndpoint = clientConnections + 64;
  for (const auto &config : configs) {
    lanesPerEndpoint += config.client().io_worker().transport_pool().max_connections() +
                        config.forward_client().io_worker().transport_pool().max_connections();
  }
  manifest_.laneCount = lanesPerEndpoint * addresses_.size();
  uint64_t bulkPerEndpoint = 128_MB;
  for (const auto &config : configs) {
    const auto &pool = config.buffer_pool();
    uint64_t poolBytes = pool.effectiveBufferSize().toInt() * pool.effectiveBufferCount() +
                         pool.effectiveBigBufferSize().toInt() * pool.effectiveBigBufferCount();
    bulkPerEndpoint = std::max(bulkPerEndpoint, poolBytes * 2);
  }
  const uint64_t lanes = manifest_.laneCount;
  const std::array<uint64_t, cxl::kCxlRangeCount> lengths{4096 * addresses_.size(),
                                                          lanes * 512,
                                                          lanes * 16384,
                                                          lanes * 2 * 8 * sizeof(cxl::CxlFrameEntry),
                                                          lanes * 2 * 8 * 65536,
                                                          addresses_.size() * 16384 * sizeof(cxl::CxlAllocationRecord),
                                                          addresses_.size() * bulkPerEndpoint,
                                                          4096};
  uint64_t offset = cxl::kCxlSuperblockBytes;
  for (size_t i = 0; i < lengths.size(); ++i) {
    offset = (offset + 4095) & ~uint64_t{4095};
    net::CxlManifestRange range;
    range.kind = static_cast<cxl::CxlRangeKind>(i + 1);
    range.offset = offset;
    range.length = lengths[i];
    manifest_.ranges.push_back(range);
    offset += range.length;
  }
  manifest_.totalRegionBytes = (offset + 4095) & ~uint64_t{4095};
  manifest_.lifecycleRecordOffset = manifest_.ranges.back().offset;
  for (size_t i = 0; i < addresses_.size(); ++i) {
    if (!addresses_[i].isCXL() || !addresses_[i].port) throw std::runtime_error("unprepared CXL participant");
    net::CxlManifestParticipant participant;
    participant.endpoint = i + 1;
    participant.localAddress = addresses_[i].str();
    manifest_.participants.push_back(participant);
    net::CxlManifestRoute route;
    route.address = addresses_[i].str();
    route.plane = net::ServicePlane::Data;
    route.targetEndpoint = i + 1;
    manifest_.routes.push_back(route);
  }
  const auto identity = serde::toJsonString(manifest_);
  unsigned char digest[SHA256_DIGEST_LENGTH];
  SHA256(reinterpret_cast<const unsigned char *>(identity.data()), identity.size(), digest);
  for (auto byte : digest) manifest_.manifestSha256 += fmt::format("{:02x}", byte);
  save(directory_ / "manifest.json", serde::toJsonString(manifest_));
  save(directory_ / "manifest-identity.json", identity);
  const int fd = ::open((directory_ / "fabric.raw").c_str(), O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
  if (fd < 0) throw std::runtime_error("cannot create fixture fabric");
  const auto truncated = ::ftruncate(fd, manifest_.totalRegionBytes);
  ::close(fd);
  if (truncated) throw std::runtime_error("cannot size fixture fabric");
  auto authorityConfig = runtimeConfig(1);
  save(directory_ / "authority.owner", authorityConfig.authority_receipt());
  authority_ = CxlTestProcess::launch(executable_, {"authority"}, directory_ / "authority.log", 60s);
  CxlStorageTestRequest request;
  request.command = "start";
  request.runtime = authorityConfig.toString();
  generations_[0] = cxlTestRequest(*authority_, request).generation;
  request.runtime = runtimeConfig(2).toString();
  generations_[1] = cxlTestRequest(*mgmtd_, request).generation;
  for (size_t i = 0; i < servers.size(); ++i) {
    generations_[i + 2] = servers[i]->startCxl(runtimeConfig(i + 3));
  }
  requireCxlTestResult(net::TransportRuntime::startConfigured(runtimeConfig(addresses_.size())));
  clientStarted_ = true;
  generations_.back() = net::TransportRuntime::cxlFabric()->config().endpointGeneration;
  started_ = true;
}

void CxlStorageTestFixture::editRouting(const std::function<void(flat::RoutingInfo &)> &callback) {
  CxlStorageTestRequest request;
  request.command = "edit-routing";
  decodeCxlTestReply(mgmtd_->requestWithCallback(serde::serialize(request), [&](std::string_view snapshot) {
    flat::RoutingInfo routing;
    requireCxlTestResult(serde::deserialize(routing, snapshot));
    callback(routing);
    return serde::serialize(routing);
  }));
}

void CxlStorageTestFixture::updateMgmtdConfig(const mgmtd::MgmtdServer::Config &config) {
  CxlStorageTestRequest request;
  request.command = "hot-config";
  request.config = config.toString();
  cxlTestRequest(*mgmtd_, request);
}

net::Address CxlStorageTestFixture::prepareMigration(std::string config) {
  if (started_ || migration_) throw std::runtime_error("migration must be prepared once before manifest freeze");
  migration_ = CxlTestProcess::launch(executable_, {"migration"}, directory_ / "migration.log", 60s);
  CxlStorageTestRequest request;
  request.command = "prepare";
  request.config = std::move(config);
  request.clusterId = "test";
  const auto reply = cxlTestRequest(*migration_, request);
  const auto address = reply.appInfo.serviceGroups.front().endpoints.front();
  migrationEndpoint_ = addresses_.size();
  addresses_.insert(addresses_.end() - 1, address);
  generations_.insert(generations_.end() - 1, 0);
  save(directory_ / "migration.toml", request.config);
  return address;
}

void CxlStorageTestFixture::startMigration() {
  if (!started_ || !migration_ || generations_.at(migrationEndpoint_ - 1))
    throw std::runtime_error("migration start requires its prepared, unused endpoint");
  CxlStorageTestRequest request;
  request.command = "start";
  request.runtime = runtimeConfig(migrationEndpoint_).toString();
  generations_[migrationEndpoint_ - 1] = cxlTestRequest(*migration_, request).generation;
}

void CxlStorageTestFixture::stopMigration() { stopProcess(migration_); }

void CxlStorageTestFixture::verifyRetirement() {
  auto region = cxl::CxlRegion::mapFile(directory_ / "fabric.raw", manifest_.totalRegionBytes);
  requireCxlTestResult(region);
  std::string evidence = "{\"status\":\"passed\",\"endpoints\":[";
  for (size_t i = 0; i < generations_.size(); ++i) {
    auto bytes = region->checkedRange(manifest_.ranges.front().offset + i * sizeof(cxl::CxlEndpointRecord),
                                      sizeof(cxl::CxlEndpointRecord));
    requireCxlTestResult(bytes);
    cxl::CxlEndpointRecord record;
    std::memcpy(&record, bytes->data(), sizeof(record));
    auto sequence = cxl::loadLe64(&record.recordSequence);
    if (!generations_[i] || !sequence || (sequence & 1) ||
        cxl::loadLe64(&record.sessionGeneration) != manifest_.sessionGeneration ||
        cxl::loadLe32(&record.endpointId) != i + 1 || cxl::loadLe64(&record.endpointGeneration) != generations_[i] ||
        cxl::loadLe32(&record.lifecycle) != static_cast<uint32_t>(cxl::CxlEndpointLifecycle::Retired) ||
        cxl::loadLe32(&record.recordCrc32c) !=
            cxl::cxlCrc32cWithZeroedU32(record, offsetof(cxl::CxlEndpointRecord, recordCrc32c))) {
      throw std::runtime_error(fmt::format("CXL endpoint {} failed retirement validation", i + 1));
    }
    if (i) evidence += ',';
    evidence += fmt::format("{{\"endpoint\":{},\"generation\":{},\"sequence\":{},\"crc32c\":{}}}",
                            i + 1,
                            generations_[i],
                            sequence,
                            cxl::loadLe32(&record.recordCrc32c));
  }
  evidence += "]}\n";
  save(directory_ / "retirement.json", evidence);
  std::cout << "HF3FS_CXL_STORAGE_FIXTURE_RETIRED endpoints=" << generations_.size() << std::endl;
}

void CxlStorageTestFixture::stop(bool preserveBacking) {
  std::string failures;
  auto attempt = [&](auto action) {
    try {
      action();
    } catch (const std::exception &e) {
      failures += std::string(e.what()) + '\n';
    }
  };
  attempt([&] { stopMigration(); });
  if (clientStarted_) {
    attempt([&] { requireCxlTestResult(net::TransportRuntime::stopCxl()); });
    clientStarted_ = false;
  }
  attempt([&] { stopProcess(mgmtd_); });
  attempt([&] { stopProcess(authority_); });
  if (started_) {
    started_ = false;
    attempt([&] { verifyRetirement(); });
    if (failures.empty() && !preserveBacking) std::filesystem::remove(directory_ / "fabric.raw");
  }
  if (!failures.empty()) throw std::runtime_error(failures + "evidence: " + directory_.string());
}

}  // namespace hf3fs::test
