#pragma once

#include <atomic>
#include <memory>
#include <mutex>
#include <thread>
#include <unordered_set>

#include "common/net/cxl/CxlMetrics.h"
#include "common/utils/Duration.h"
#include "common/utils/Result.h"

namespace hf3fs::net::cxl {

class CxlSocket;

struct CxlPollConfig {
  Duration spin{std::chrono::microseconds(20)};
  uint32_t yields{8};
  Duration sleep{std::chrono::microseconds(10)};
};

class CxlProgressEngine {
 public:
  explicit CxlProgressEngine(CxlPollConfig config = {}, std::shared_ptr<CxlMetrics> metrics = nullptr);
  ~CxlProgressEngine();

  CxlProgressEngine(const CxlProgressEngine &) = delete;
  CxlProgressEngine &operator=(const CxlProgressEngine &) = delete;

  Result<Void> registerSocket(CxlSocket &socket);
  Result<Void> unregisterSocket(CxlSocket &socket);
  Result<Void> start();
  void stopAndJoin();

  bool running() const noexcept { return running_.load(std::memory_order_acquire); }
  const std::shared_ptr<CxlMetrics> &metrics() const noexcept { return metrics_; }

 private:
  void loop();
  void scanOnce() noexcept;

  CxlPollConfig config_;
  std::shared_ptr<CxlMetrics> metrics_;
  std::mutex mutex_;
  std::unordered_set<CxlSocket *> sockets_;
  std::thread thread_;
  std::atomic<bool> stop_{};
  std::atomic<bool> running_{};
};

}  // namespace hf3fs::net::cxl
