#include "common/net/BulkTransfer.h"

namespace hf3fs::net {

Result<Void> BulkTransferBatch::add(RemoteBufferHandle remote, SharedBuffer local) {
  return add(std::move(remote), std::span(&local, 1));
}

Result<Void> BulkTransferBatch::add(RemoteBufferHandle remote, std::span<SharedBuffer> local) {
  if (transfer_ == nullptr || local.empty()) {
    return makeError(RPCCode::kTransportCapabilityMissing, "bulk transfer backend or local buffers are missing");
  }
  RETURN_ON_ERROR(validateRemoteBufferHandle(remote));
  Entry entry{.remote = std::move(remote), .local = {}};
  entry.local.reserve(local.size());
  for (const auto &buffer : local) {
    if (!buffer) {
      return makeError(StatusCode::kInvalidArg, "bulk transfer batch contains an invalid local buffer");
    }
    entry.local.push_back(buffer);
  }
  entries_.push_back(std::move(entry));
  return Void{};
}

CoTryTask<void> BulkTransferBatch::post() {
  if (transfer_ == nullptr) {
    co_return makeError(RPCCode::kTransportCapabilityMissing, "bulk transfer backend is missing");
  }
  for (auto &entry : entries_) {
    auto result = direction_ == BulkDirection::Pull ? co_await transfer_->pull(entry.remote, entry.local)
                                                    : co_await transfer_->push(entry.remote, entry.local);
    if (!result) {
      co_return makeError(std::move(result.error()));
    }
  }
  co_return Void{};
}

}  // namespace hf3fs::net
