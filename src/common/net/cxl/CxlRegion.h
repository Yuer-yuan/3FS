#pragma once

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <span>

#include "common/utils/Result.h"

namespace hf3fs::net::cxl {

class CxlRegion {
 public:
  CxlRegion() = default;
  ~CxlRegion();

  CxlRegion(const CxlRegion &) = delete;
  CxlRegion &operator=(const CxlRegion &) = delete;

  CxlRegion(CxlRegion &&other) noexcept;
  CxlRegion &operator=(CxlRegion &&other) noexcept;

  // length == 0 maps from offset through the end of the existing file.
  // This function never creates or resizes the file.
  static Result<CxlRegion> mapFile(const std::filesystem::path &path, uint64_t length = 0, uint64_t offset = 0);

  // devdax capacity and alignment are obtained from sysfs. st_size is not a
  // valid capacity source for a character device. sysfsRoot is injectable so
  // the identity parsing can be tested without changing the host system.
  static Result<CxlRegion> mapDax(const std::filesystem::path &path,
                                  uint64_t offset = 0,
                                  uint64_t length = 0,
                                  const std::filesystem::path &sysfsRoot = "/sys");

  Result<std::span<std::byte>> checkedRange(uint64_t offset, uint64_t length, size_t alignment = 1);
  Result<std::span<const std::byte>> checkedRange(uint64_t offset, uint64_t length, size_t alignment = 1) const;

  std::span<std::byte> bytes() noexcept { return {data_, size_}; }
  std::span<const std::byte> bytes() const noexcept { return {data_, size_}; }
  size_t size() const noexcept { return size_; }
  int fd() const noexcept { return fd_; }
  explicit operator bool() const noexcept { return data_ != nullptr; }

 private:
  CxlRegion(int fd, std::byte *data, size_t size) noexcept
      : fd_(fd),
        data_(data),
        size_(size) {}

  static Result<CxlRegion> mapOpenedFd(int fd, uint64_t offset, uint64_t length, uint64_t requiredAlignment);

  void reset() noexcept;

  int fd_{-1};
  std::byte *data_{nullptr};
  size_t size_{0};
};

}  // namespace hf3fs::net::cxl
