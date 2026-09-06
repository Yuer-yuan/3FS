#pragma once

#include "mgmtd/MgmtdServer.h"
#include "storage/service/StorageServer.h"
#include "tests/lib/CxlStorageTestProtocol.h"

namespace hf3fs::test {

// The same explicit test operations are available for the legacy local RDMA
// server and the exec CXL server. This proxy never carries product requests.
class StorageTestServer {
 public:
  explicit StorageTestServer(std::unique_ptr<storage::StorageServer> local);
  StorageTestServer(std::unique_ptr<CxlTestProcess> process, flat::AppInfo info);
  ~StorageTestServer();
  const flat::AppInfo &appInfo() const { return info_; }
  net::Address address() const { return info_.serviceGroups.front().endpoints.front(); }
  void stopAndJoin();
  void setFakeRoutingInfo(const std::shared_ptr<flat::RoutingInfo> &routing);
  void refreshRoutingInfo();
  bool checkLocalTargetState(flat::LocalTargetState state);
  void updateConfig(const storage::StorageServer::Config &config);
  uint64_t startCxl(const net::TransportRuntime::Config &runtime);

 private:
  std::unique_ptr<storage::StorageServer> local_;
  std::unique_ptr<CxlTestProcess> process_;
  flat::AppInfo info_;
};

class CxlStorageTestFixture {
 public:
  explicit CxlStorageTestFixture(uint32_t storageNodes);
  ~CxlStorageTestFixture();
  const std::filesystem::path &directory() const { return directory_; }
  void prepareMgmtd(mgmtd::MgmtdServer::Config &config);
  const std::vector<net::Address> &mgmtdAddresses() const { return mgmtdAddresses_; }
  std::unique_ptr<StorageTestServer> prepareStorage(size_t index,
                                                    storage::StorageServer::Config &config,
                                                    flat::NodeId nodeId);
  void start(const std::vector<std::unique_ptr<StorageTestServer>> &servers,
             const std::vector<storage::StorageServer::Config> &configs,
             uint32_t clientConnections);
  void editRouting(const std::function<void(flat::RoutingInfo &)> &callback);
  void updateMgmtdConfig(const mgmtd::MgmtdServer::Config &config);
  net::Address prepareMigration(std::string config);
  void startMigration();
  void stopMigration();
  void stop(bool preserveBacking);

 private:
  net::TransportRuntime::Config runtimeConfig(uint32_t endpoint) const;
  net::Address reserveAddress();
  void verifyRetirement();
  std::filesystem::path directory_;
  std::filesystem::path executable_;
  std::unique_ptr<CxlTestProcess> authority_;
  std::unique_ptr<CxlTestProcess> mgmtd_;
  std::unique_ptr<CxlTestProcess> migration_;
  uint32_t migrationEndpoint_{};
  std::vector<net::Address> mgmtdAddresses_;
  std::vector<int> reservedSockets_;
  std::vector<net::Address> addresses_;
  std::vector<uint64_t> generations_;
  net::CxlRuntimeManifest manifest_;
  bool started_{};
  bool clientStarted_{};
};

}  // namespace hf3fs::test
