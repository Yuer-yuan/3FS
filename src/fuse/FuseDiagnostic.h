#pragma once

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <thread>
#include <folly/experimental/coro/BlockingWait.h>
#include "client/meta/MetaClient.h"

namespace hf3fs::fuse {
// Explicit finite diagnostic mode only. Uses the mounted client's existing
// MetaClient/connection, so a health reply can be correlated with business RPCs.
inline std::jthread startFuseDiagnostic(std::shared_ptr<meta::client::MetaClient> client) {
  const char *directory = std::getenv("HF3FS_CXL_DIAGNOSTIC_DIR");
  if (!directory || !*directory) return {};
  return std::jthread([client = std::move(client), directory = std::string(directory)](std::stop_token stop) {
    uint64_t previous = 0;
    while (!stop.stop_requested()) {
      uint64_t sequence = 0;
      std::ifstream command(directory + "/testRpc.command");
      if ((command >> sequence) && sequence > previous) {
        previous = sequence;
        auto result = folly::coro::blockingWait(client->testRpc());
        auto path = directory + "/testRpc.result";
        { std::ofstream output(path + ".tmp");
          output << "HF3FS_DIAGNOSTIC_TEST_RPC sequence=" << sequence << " status="
                 << (result ? 0 : result.error().code()) << '\n'; }
        std::rename((path + ".tmp").c_str(), path.c_str());
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
  });
}
}  // namespace hf3fs::fuse
