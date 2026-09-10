#pragma once

#include <memory>
#include <span>
#include <folly/Executor.h>

#include "common/net/BulkTransfer.h"
#include "common/net/cxl/CxlFabric.h"

namespace hf3fs::net::cxl {

class CxlBulkTransfer final : public BulkTransfer {
 public:
  explicit CxlBulkTransfer(std::shared_ptr<CxlFabric> fabric, folly::Executor::KeepAlive<> copyExecutor = {})
      : fabric_(std::move(fabric)), copyExecutor_(std::move(copyExecutor)) {}

  CoTryTask<void> pull(const RemoteBufferHandle &remote, std::span<SharedBuffer> local) override;
  CoTryTask<void> push(const RemoteBufferHandle &remote, std::span<SharedBuffer> local) override;

 private:
  enum class Direction { Pull, Push };

  static CoTryTask<void> copy(std::shared_ptr<CxlFabric> fabric,
                              RemoteBufferHandle remote,
                              std::vector<SharedBuffer> local,
                              Direction direction);

  Result<std::span<std::byte>> validateAndMap(const RemoteBufferHandle &remote,
                                              std::span<SharedBuffer> local,
                                              Direction direction) const;

  std::shared_ptr<CxlFabric> fabric_;
  folly::Executor::KeepAlive<> copyExecutor_;
};

}  // namespace hf3fs::net::cxl
