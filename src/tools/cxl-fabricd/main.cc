#include <atomic>
#include <chrono>
#include <csignal>
#include <iostream>
#include <thread>

#include <folly/ScopeGuard.h>

#include "common/net/TransportRuntime.h"
#include "common/utils/ConfigBase.h"

namespace {

std::atomic<bool> stopRequested{false};
static_assert(std::atomic<bool>::is_always_lock_free);

void handleSignal(int) { stopRequested.store(true, std::memory_order_relaxed); }

class Config : public hf3fs::ConfigBase<Config> {
 public:
  CONFIG_OBJ(cxl, hf3fs::net::TransportRuntime::Config);
  CONFIG_ITEM(health_check_interval, hf3fs::Duration(std::chrono::milliseconds(100)));
};

int run(const Config &config) {
  if (!config.cxl().enabled() ||
      config.cxl().mode() != hf3fs::net::cxl::CxlFabric::StartMode::InitializeAuthority) {
    std::cerr << "cxl-fabricd: cxl.enabled must be true and cxl.mode must be InitializeAuthority\n";
    return 2;
  }

  auto started = hf3fs::net::TransportRuntime::startConfigured(config.cxl());
  if (!started) {
    std::cerr << "cxl-fabricd: startup failed: " << started.error().describe() << '\n';
    return 1;
  }
  auto cleanup = folly::makeGuard([] { (void)hf3fs::net::TransportRuntime::stopCxl(); });

  std::signal(SIGINT, handleSignal);
  std::signal(SIGTERM, handleSignal);
  std::cout << "HF3FS_CXL_FABRIC_READY endpoint=" << config.cxl().endpoint()
            << " manifest=" << config.cxl().manifest_path() << '\n';
  std::cout.flush();

  while (!stopRequested.load(std::memory_order_relaxed)) {
    auto fabric = hf3fs::net::TransportRuntime::cxlFabric();
    if (!fabric) {
      std::cerr << "cxl-fabricd: transport runtime disappeared\n";
      return 1;
    }
    auto checked = fabric->check();
    if (!checked) {
      std::cerr << "cxl-fabricd: fabric faulted: " << checked.error().describe() << '\n';
      return 1;
    }
    std::this_thread::sleep_for(config.health_check_interval().asMs());
  }

  auto stopped = hf3fs::net::TransportRuntime::stopCxl();
  cleanup.dismiss();
  if (!stopped) {
    std::cerr << "cxl-fabricd: shutdown failed: " << stopped.error().describe() << '\n';
    return 1;
  }
  std::cout << "HF3FS_CXL_FABRIC_RETIRED\n";
  return 0;
}

}  // namespace

int main(int argc, char **argv) {
  Config config;
  auto initialized = config.init(&argc, &argv);
  if (!initialized) {
    std::cerr << "cxl-fabricd: configuration failed: " << initialized.error().describe() << '\n';
    return 2;
  }
  return run(config);
}
