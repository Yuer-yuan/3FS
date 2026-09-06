#include "common/serde/CallContext.h"

#include "common/monitor/Recorder.h"
#include "common/serde/ClientContext.h"

namespace hf3fs::serde {
namespace {

monitor::CountRecorder deserilizeFails{"common.rpc.deserilize.fails"};
monitor::OperationRecorder applyBulkTransmission{"common.apply_bulk_transmission"};
monitor::CountRecorder applyBulkTransmissionTimeout{"common.apply_bulk_transmission.timeout"};

}  // namespace

CoTask<void> CallContext::BulkTransmission::applyTransmission(Duration timeout) {
  auto recordGuard = applyBulkTransmission.record();
  net::UserRequestOptions options;
  options.timeout = timeout;
  net::BulkTransmissionReq req{ctx_.packet().uuid};
  serde::ClientContext clientCtx(ctx_.transport());
  auto applyResult = co_await net::BulkControl<>::apply(clientCtx, req, &options);
  if (UNLIKELY(!applyResult)) {
    XLOGF(DBG, "apply transmission error: {}", applyResult.error());
    applyBulkTransmissionTimeout.addSample(1);
  }
  recordGuard.succ();
}

#if HF3FS_ENABLE_RDMA
CoTask<void> CallContext::RDMATransmission::applyTransmission(Duration timeout) {
  auto recordGuard = applyBulkTransmission.record();
  net::UserRequestOptions options;
  options.timeout = timeout;
  net::BulkTransmissionReq req{ctx_.packet().uuid};
  serde::ClientContext clientCtx(ctx_.transport());
  auto applyResult = co_await net::BulkControl<>::apply(clientCtx, req, &options);
  if (UNLIKELY(!applyResult)) {
    XLOGF(DBG, "apply transmission error: {}", applyResult.error());
    applyBulkTransmissionTimeout.addSample(1);
  }
  recordGuard.succ();
}
#endif

void CallContext::onDeserializeFailed() {
  packet_.payload = std::string_view{};
  XLOGF(ERR, "deserialize request failed: {}, peer: {}", packet_, peer());
  deserilizeFails.addSample(1);
  onError(makeError(RPCCode::kVerifyRequestFailed));
}

}  // namespace hf3fs::serde
