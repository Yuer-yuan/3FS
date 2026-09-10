#include "common/net/cxl/CxlProgressEngine.h"

#include <chrono>

#include "common/net/cxl/CxlSocket.h"

namespace hf3fs::net::cxl {

CxlProgressEngine::CxlProgressEngine(CxlPollConfig config, std::shared_ptr<CxlMetrics> metrics)
    : config_(config),
      metrics_(metrics ? std::move(metrics) : std::make_shared<CxlMetrics>()) {}

CxlProgressEngine::~CxlProgressEngine() { stopAndJoin(); }

Result<Void> CxlProgressEngine::registerSocket(CxlSocket &socket) {
  std::lock_guard lock(mutex_);
  if (!sockets_.insert(&socket).second) {
    return makeError(StatusCode::kInvalidArg, "CXL socket is already registered");
  }
  return Void{};
}

Result<Void> CxlProgressEngine::unregisterSocket(CxlSocket &socket) {
  std::lock_guard lock(mutex_);
  sockets_.erase(&socket);
  return Void{};
}

Result<Void> CxlProgressEngine::start() {
  bool expected = false;
  if (!running_.compare_exchange_strong(expected, true, std::memory_order_acq_rel)) {
    return makeError(StatusCode::kInvalidArg, "CXL progress engine is already running");
  }
  stop_.store(false, std::memory_order_release);
  try {
    thread_ = std::thread(&CxlProgressEngine::loop, this);
  } catch (...) {
    running_.store(false, std::memory_order_release);
    return makeError(RPCCode::kDataPlaneInitFailed, "failed to start CXL progress thread");
  }
  return Void{};
}

void CxlProgressEngine::stopAndJoin() {
  stop_.store(true, std::memory_order_release);
  if (thread_.joinable()) {
    thread_.join();
  }
  running_.store(false, std::memory_order_release);
}

void CxlProgressEngine::loop() {
  if (config_.adaptive) {
    CxlIdleBackoff backoff(config_.idleSleepMin, config_.idleSleepMax);
    while (!stop_.load(std::memory_order_acquire)) {
      const auto delay = backoff.next(scanOnce());
      std::this_thread::sleep_for(delay);
      metrics_->addProgressSleep();
    }
    return;
  }

  while (!stop_.load(std::memory_order_acquire)) {
    const auto spinStart = std::chrono::steady_clock::now();
    const auto spinEnd = spinStart + config_.spin;
    do {
      scanOnce();
    } while (!stop_.load(std::memory_order_relaxed) && std::chrono::steady_clock::now() < spinEnd);
    const auto spun =
        std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now() - spinStart);
    metrics_->addProgressSpinNanoseconds(spun.count());

    for (uint32_t count = 0; count < config_.yields && !stop_.load(std::memory_order_relaxed); ++count) {
      std::this_thread::yield();
      metrics_->addProgressYield();
      scanOnce();
    }
    if (!stop_.load(std::memory_order_relaxed) && config_.sleep > Duration::zero()) {
      metrics_->addProgressSleep();
      std::this_thread::sleep_for(config_.sleep);
    }
  }
}

bool CxlProgressEngine::scanOnce() noexcept {
  bool progressed = false;
  std::lock_guard lock(mutex_);
  for (auto *socket : sockets_) {
    progressed |= socket->progress();
  }
  return progressed;
}

}  // namespace hf3fs::net::cxl
