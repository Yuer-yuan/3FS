#pragma once

#include <memory>
#include <span>

#include "common/net/BulkTransfer.h"
#include "common/net/cxl/CxlFabric.h"

namespace hf3fs::net::cxl {

class CxlBulkTransfer final : public BulkTransfer {
 public:
  explicit CxlBulkTransfer(std::shared_ptr<CxlFabric> fabric)
      : fabric_(std::move(fabric)) {}

  CoTryTask<void> pull(const RemoteBufferHandle &remote, std::span<SharedBuffer> local) override;
  CoTryTask<void> push(const RemoteBufferHandle &remote, std::span<SharedBuffer> local) override;

 private:
  enum class Direction { Pull, Push };

  Result<std::span<std::byte>> validateAndMap(const RemoteBufferHandle &remote,
                                              std::span<SharedBuffer> local,
                                              Direction direction) const;

  std::shared_ptr<CxlFabric> fabric_;
};

}  // namespace hf3fs::net::cxl
