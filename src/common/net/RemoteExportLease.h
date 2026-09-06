#pragma once

#include <memory>

namespace hf3fs::net {

class RemoteExportLease {
 public:
  RemoteExportLease() = default;
  explicit RemoteExportLease(std::shared_ptr<void> owner)
      : owner_(std::move(owner)) {}

  RemoteExportLease(RemoteExportLease &&) noexcept = default;
  RemoteExportLease &operator=(RemoteExportLease &&) noexcept = default;
  RemoteExportLease(const RemoteExportLease &) = delete;
  RemoteExportLease &operator=(const RemoteExportLease &) = delete;

  explicit operator bool() const noexcept { return bool(owner_); }
  void reset() noexcept { owner_.reset(); }

 private:
  std::shared_ptr<void> owner_;
};

}  // namespace hf3fs::net
