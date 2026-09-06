#pragma once

#include "common/serde/CallContext.h"
#include "common/serde/Service.h"
#include "common/utils/ConfigBase.h"
#include "common/utils/Semaphore.h"

namespace hf3fs::net {

struct BulkTransmissionReq {
  SERDE_STRUCT_FIELD(uuid, size_t{});
};

struct BulkTransmissionRsp {
  SERDE_STRUCT_FIELD(dummy, Void{});
};

// Service and method IDs are retained from the phase-0 protocol.
SERDE_SERVICE(BulkControl, 10) { SERDE_SERVICE_METHOD(apply, 1, BulkTransmissionReq, BulkTransmissionRsp); };

class BulkTransmissionLimiter {
 public:
  explicit BulkTransmissionLimiter(uint32_t maxConcurrentTransmission)
      : semaphore_(maxConcurrentTransmission) {}

  CoTask<void> co_wait();
  void signal(Duration latency);
  void updateMaxConcurrentTransmission(uint32_t value) { semaphore_.changeUsableTokens(value); }

 private:
  Semaphore semaphore_;
  std::atomic<uint32_t> current_{};
};
using BulkTransmissionLimiterPtr = std::shared_ptr<BulkTransmissionLimiter>;

class BulkControlImpl : public serde::ServiceWrapper<BulkControlImpl, BulkControl> {
 public:
  class Config : public ConfigBase<Config> {
    CONFIG_HOT_UPDATED_ITEM(max_concurrent_transmission, 64u);
  };

  explicit BulkControlImpl(const Config &config)
      : config_(config),
        limiter_(std::make_shared<BulkTransmissionLimiter>(config_.max_concurrent_transmission())),
        guard_(config_.addCallbackGuard(
            [&] { limiter_->updateMaxConcurrentTransmission(config_.max_concurrent_transmission()); })) {}

  CoTryTask<BulkTransmissionRsp> apply(serde::CallContext &, const BulkTransmissionReq &req);

 private:
  const Config &config_;
  BulkTransmissionLimiterPtr limiter_;
  std::unique_ptr<ConfigCallbackGuard> guard_;
};

}  // namespace hf3fs::net
