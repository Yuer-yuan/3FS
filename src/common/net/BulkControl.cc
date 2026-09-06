#include "common/net/BulkControl.h"

#include "common/monitor/Recorder.h"
#include "common/net/Waiter.h"
#include "common/serde/ClientContext.h"
#include "common/utils/Duration.h"

namespace hf3fs::net {
namespace {

monitor::ValueRecorder currentBulkTransmission{"common.bulk_control.current", std::nullopt, false};
monitor::LatencyRecorder transmissionPrepareLatency{"common.transmission.prepare_latency"};
monitor::LatencyRecorder transmissionWaitLatency{"common.transmission.wait_latency"};
monitor::LatencyRecorder transmissionNetworkLatency{"common.transmission.network_latency"};

}  // namespace

CoTask<void> BulkTransmissionLimiter::co_wait() {
  co_await semaphore_.co_wait();
  currentBulkTransmission.set(++current_);
}

void BulkTransmissionLimiter::signal(Duration latency) {
  currentBulkTransmission.set(--current_);
  semaphore_.signal();
  transmissionNetworkLatency.addSample(latency);
}

CoTryTask<BulkTransmissionRsp> BulkControlImpl::apply(serde::CallContext &ctx, const BulkTransmissionReq &req) {
  auto startTime = RelativeTime::now();
  co_await limiter_->co_wait();
  transmissionWaitLatency.addSample(RelativeTime::now() - startTime);
  auto prepareLatency = Waiter::instance().setTransmissionLimiterPtr(req.uuid, limiter_, startTime);
  if (UNLIKELY(!prepareLatency)) {
    limiter_->signal(0_ms);
  } else {
    transmissionPrepareLatency.addSample(*prepareLatency);
  }
  co_return BulkTransmissionRsp{};
}

}  // namespace hf3fs::net
