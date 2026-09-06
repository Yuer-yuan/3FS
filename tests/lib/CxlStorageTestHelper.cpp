#include <folly/init/Init.h>
#include <iostream>

#include "common/logging/LogInit.h"
#include "common/utils/SysResource.h"
#include "migration/service/Server.h"
#include "tests/lib/CxlStorageTestProtocol.h"
#include "tests/lib/Helper.h"
#include "tests/lib/UnitTestFabric.h"

using namespace hf3fs;
using namespace hf3fs::test;
using namespace std::chrono_literals;

int main(int argc, char **argv) {
  if (argc != 3) return 2;
  const int controlFd = std::stoi(argv[1]);
  const std::string role = argv[2];
  folly::init(&argc, &argv, false);
  logging::initLogHandlers();
  logging::initOrDie("INFO");
  SysResource::increaseProcessFDLimit(524288);
  CxlTestChannel channel(controlFd);
  storage::StorageServer::Config storageConfig;
  mgmtd::MgmtdServer::Config mgmtdConfig;
  migration::server::MigrationServer::Config migrationConfig;
  std::unique_ptr<storage::StorageServer> storage;
  std::unique_ptr<mgmtd::MgmtdServer> mgmtd;
  std::unique_ptr<migration::server::MigrationServer> migration;
  flat::AppInfo info;
  bool runtimeStarted = false;
  auto deadline = [] { return std::chrono::steady_clock::now() + 60s; };
  try {
    channel.send(CxlTestProcess::kReady, deadline());
    while (true) {
      // Tests can deliberately wait several minutes for retries and resync.
      // Each actual command still has the parent's 60-second transaction bound.
      auto message = channel.receive(std::chrono::steady_clock::now() + 30min);
      CxlStorageTestRequest request;
      requireCxlTestResult(serde::deserialize(request, message));
      CxlStorageTestReply reply;
      const auto transactionDeadline = deadline();
      try {
        if (request.command == "prepare") {
          info = request.appInfo;
          info.clusterId = request.clusterId;
          if (info.clusterId.empty()) throw std::runtime_error("missing fixture cluster id");
          net::Server *server;
          if (role == "storage") {
            if (storage) throw std::runtime_error("storage already prepared");
            requireCxlTestResult(storageConfig.atomicallyUpdate(std::string_view(request.config), false));
            storage = std::make_unique<storage::StorageServer>(storageConfig);
            server = storage.get();
          } else if (role == "mgmtd") {
            if (mgmtd) throw std::runtime_error("mgmtd already prepared");
            requireCxlTestResult(mgmtdConfig.atomicallyUpdate(std::string_view(request.config), false));
            mgmtd = std::make_unique<mgmtd::MgmtdServer>(mgmtdConfig);
            server = mgmtd.get();
          } else if (role == "migration") {
            if (migration) throw std::runtime_error("migration already prepared");
            requireCxlTestResult(migrationConfig.atomicallyUpdate(std::string_view(request.config), false));
            migration = std::make_unique<migration::server::MigrationServer>(migrationConfig);
            server = migration.get();
          } else {
            throw std::runtime_error("invalid prepare role");
          }
          requireCxlTestResult(server->setup());
          info.serviceGroups = server->getServiceGroupInfos();
          reply.appInfo = info;
        } else if (request.command == "start") {
          if (runtimeStarted) throw std::runtime_error("runtime already started");
          net::TransportRuntime::Config runtime;
          requireCxlTestResult(runtime.atomicallyUpdate(std::string_view(request.runtime), false));
          requireCxlTestResult(net::TransportRuntime::startConfigured(runtime));
          runtimeStarted = true;
          if (storage)
            requireCxlTestResult(storage->start(info));
          else if (mgmtd)
            requireCxlTestResult(mgmtd->start(info, std::make_shared<kv::MemKVEngine>()));
          else if (migration)
            requireCxlTestResult(migration->start(info));
          else if (role != "authority")
            throw std::runtime_error("service not prepared");
          reply.generation = net::TransportRuntime::cxlFabric()->config().endpointGeneration;
        } else if (request.command == "fake-routing" && storage) {
          auto routing = std::make_shared<flat::RoutingInfo>(std::move(request.routing));
          auto client = RoutingStoreHelper::getMgmtdClient(*storage);
          if (auto *fake = dynamic_cast<FakeMgmtdClient *>(client.get()))
            fake->setRoutingInfo(routing);
          else
            RoutingStoreHelper::setMgmtdClient(*storage, std::make_unique<FakeMgmtdClient>(routing));
        } else if (request.command == "refresh-routing" && storage) {
          RoutingStoreHelper::refreshRoutingInfo(*storage);
        } else if (request.command == "edit-routing" && mgmtd) {
          mgmtd::testing::MgmtdTestHelper helper(*mgmtd);
          requireCxlTestResult(folly::coro::blockingWait(helper.setRoutingInfo([&](flat::RoutingInfo &routing) {
            // The original mgmtd writer lock spans this test-only exchange.
            channel.send(serde::serialize(routing), transactionDeadline);
            auto updated = channel.receive(transactionDeadline);
            requireCxlTestResult(serde::deserialize(routing, updated));
          })));
        } else if (request.command == "check-target-state" && storage) {
          reply.value = TargetMapHelper::checkLocalTargetState(*storage, request.targetState);
        } else if (request.command == "hot-config") {
          if (storage)
            requireCxlTestResult(storageConfig.atomicallyUpdate(std::string_view(request.config), true));
          else if (mgmtd)
            requireCxlTestResult(mgmtdConfig.atomicallyUpdate(std::string_view(request.config), true));
          else
            throw std::runtime_error("no configurable service");
        } else if (request.command == "stop") {
          if (storage) storage->stopAndJoin();
          if (mgmtd) mgmtd->stopAndJoin();
          if (migration) migration->stopAndJoin();
          storage.reset();
          mgmtd.reset();
          migration.reset();
          if (runtimeStarted) requireCxlTestResult(net::TransportRuntime::stopCxl());
          runtimeStarted = false;
          channel.send(serde::serialize(reply), transactionDeadline);
          std::cout << "HF3FS_CXL_SERVICE_EXIT role=" << role << " status=0" << std::endl;
          return 0;
        } else {
          throw std::runtime_error("unsupported fixture command: " + request.command);
        }
      } catch (const std::exception &error) {
        reply.error = error.what();
        std::cerr << "CXL fixture command " << request.command << " failed: " << error.what() << std::endl;
      }
      channel.send(serde::serialize(reply), transactionDeadline);
    }
  } catch (const std::exception &error) {
    std::cerr << "CXL service control failed: " << error.what() << std::endl;
    // Destruction can itself wait on service workers. The parent owns a bounded
    // wait and kills only this helper's private process group if necessary.
    return 3;
  }
}
