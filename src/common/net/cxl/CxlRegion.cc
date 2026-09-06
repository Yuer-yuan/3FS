#include "common/net/cxl/CxlRegion.h"

#include <algorithm>
#include <cerrno>
#include <charconv>
#include <cstring>
#include <fcntl.h>
#include <fmt/format.h>
#include <fstream>
#include <limits>
#include <string>
#include <string_view>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>

namespace hf3fs::net::cxl {
namespace {

Result<uint64_t> checkedEnd(uint64_t offset, uint64_t length) {
  if (length > std::numeric_limits<uint64_t>::max() - offset) {
    return makeError(StatusCode::kInvalidArg, "CXL range addition overflows uint64_t");
  }
  return offset + length;
}

Result<uint64_t> readSysfsInteger(const std::filesystem::path &path) {
  std::ifstream input(path);
  std::string text;
  if (!input || !(input >> text)) {
    return makeError(StatusCode::kIOError, fmt::format("failed to read {}", path.string()));
  }

  int base = 10;
  std::string_view digits = text;
  if (digits.starts_with("0x") || digits.starts_with("0X")) {
    base = 16;
    digits.remove_prefix(2);
  }
  uint64_t value = 0;
  auto [end, error] = std::from_chars(digits.data(), digits.data() + digits.size(), value, base);
  if (error != std::errc{} || end != digits.data() + digits.size()) {
    return makeError(StatusCode::kInvalidFormat, fmt::format("invalid integer in {}", path.string()));
  }
  return value;
}

}  // namespace

Result<CxlRegion> CxlRegion::mapOpenedFd(int fd, uint64_t offset, uint64_t length, uint64_t requiredAlignment) {
  if (length == 0 || length > std::numeric_limits<size_t>::max()) {
    ::close(fd);
    return makeError(StatusCode::kInvalidArg, "CXL mapping length is zero or exceeds size_t");
  }
  if (requiredAlignment == 0 || offset % requiredAlignment != 0 || length % requiredAlignment != 0) {
    ::close(fd);
    return makeError(StatusCode::kInvalidArg, "CXL mapping offset or length is not aligned");
  }
  if (offset > static_cast<uint64_t>(std::numeric_limits<off_t>::max())) {
    ::close(fd);
    return makeError(StatusCode::kInvalidArg, "CXL mapping offset exceeds off_t");
  }

  void *mapping =
      ::mmap(nullptr, static_cast<size_t>(length), PROT_READ | PROT_WRITE, MAP_SHARED, fd, static_cast<off_t>(offset));
  if (mapping == MAP_FAILED) {
    const int error = errno;
    ::close(fd);
    return makeError(StatusCode::kIOError, fmt::format("mmap failed: {}", std::strerror(error)));
  }
  if (reinterpret_cast<uintptr_t>(mapping) % requiredAlignment != 0) {
    ::munmap(mapping, static_cast<size_t>(length));
    ::close(fd);
    return makeError(StatusCode::kInvalidArg, "CXL mapping base is not aligned");
  }
  return CxlRegion(fd, static_cast<std::byte *>(mapping), static_cast<size_t>(length));
}

CxlRegion::~CxlRegion() { reset(); }

CxlRegion::CxlRegion(CxlRegion &&other) noexcept
    : fd_(other.fd_),
      data_(other.data_),
      size_(other.size_) {
  other.fd_ = -1;
  other.data_ = nullptr;
  other.size_ = 0;
}

CxlRegion &CxlRegion::operator=(CxlRegion &&other) noexcept {
  if (this != &other) {
    reset();
    fd_ = other.fd_;
    data_ = other.data_;
    size_ = other.size_;
    other.fd_ = -1;
    other.data_ = nullptr;
    other.size_ = 0;
  }
  return *this;
}

Result<CxlRegion> CxlRegion::mapFile(const std::filesystem::path &path, uint64_t length, uint64_t offset) {
  int fd = ::open(path.c_str(), O_RDWR | O_CLOEXEC);
  if (fd < 0) {
    return makeError(StatusCode::kIOError, fmt::format("open {} failed: {}", path.string(), std::strerror(errno)));
  }

  struct stat statbuf {};
  if (::fstat(fd, &statbuf) != 0) {
    const int error = errno;
    ::close(fd);
    return makeError(StatusCode::kIOError, fmt::format("fstat {} failed: {}", path.string(), std::strerror(error)));
  }
  if (!S_ISREG(statbuf.st_mode) || statbuf.st_size < 0) {
    ::close(fd);
    return makeError(StatusCode::kInvalidArg, "CXL file mapping requires a regular file");
  }

  const uint64_t capacity = static_cast<uint64_t>(statbuf.st_size);
  if (offset > capacity) {
    ::close(fd);
    return makeError(StatusCode::kInvalidArg, "CXL file mapping offset exceeds file size");
  }
  if (length == 0) {
    length = capacity - offset;
  }
  auto end = checkedEnd(offset, length);
  if (!end || *end > capacity) {
    ::close(fd);
    return makeError(StatusCode::kInvalidArg, "CXL file mapping exceeds file size");
  }

  const long pageSize = ::sysconf(_SC_PAGESIZE);
  if (pageSize <= 0) {
    ::close(fd);
    return makeError(StatusCode::kIOError, "sysconf(_SC_PAGESIZE) failed");
  }
  // mmap only requires a page-aligned file offset. Regular-file test mappings
  // intentionally allow a final partial page.
  if (offset % static_cast<uint64_t>(pageSize) != 0) {
    ::close(fd);
    return makeError(StatusCode::kInvalidArg, "CXL file mapping offset is not page aligned");
  }
  return mapOpenedFd(fd, offset, length, 1);
}

Result<CxlRegion> CxlRegion::mapDax(const std::filesystem::path &path,
                                    uint64_t offset,
                                    uint64_t length,
                                    const std::filesystem::path &sysfsRoot) {
  int fd = ::open(path.c_str(), O_RDWR | O_CLOEXEC);
  if (fd < 0) {
    return makeError(StatusCode::kIOError, fmt::format("open {} failed: {}", path.string(), std::strerror(errno)));
  }

  struct stat statbuf {};
  if (::fstat(fd, &statbuf) != 0) {
    const int error = errno;
    ::close(fd);
    return makeError(StatusCode::kIOError, fmt::format("fstat {} failed: {}", path.string(), std::strerror(error)));
  }
  if (!S_ISCHR(statbuf.st_mode)) {
    ::close(fd);
    return makeError(StatusCode::kInvalidArg, "CXL DAX mapping requires a character device");
  }

  const auto identity = fmt::format("{}:{}", major(statbuf.st_rdev), minor(statbuf.st_rdev));
  std::error_code fsError;
  auto devicePath = std::filesystem::canonical(sysfsRoot / "dev/char" / identity, fsError);
  if (fsError) {
    ::close(fd);
    return makeError(StatusCode::kIOError, fmt::format("cannot resolve devdax sysfs identity {}", identity));
  }
  auto capacityResult = readSysfsInteger(devicePath / "size");
  auto alignmentResult = readSysfsInteger(devicePath / "align");
  if (!capacityResult || !alignmentResult) {
    ::close(fd);
    return !capacityResult ? makeError(std::move(capacityResult.error()))
                           : makeError(std::move(alignmentResult.error()));
  }

  const long pageSize = ::sysconf(_SC_PAGESIZE);
  if (pageSize <= 0 || *alignmentResult == 0) {
    ::close(fd);
    return makeError(StatusCode::kInvalidArg, "invalid devdax or system page alignment");
  }
  const uint64_t requiredAlignment = std::max(*alignmentResult, static_cast<uint64_t>(pageSize));
  const uint64_t capacity = *capacityResult;
  if (offset > capacity) {
    ::close(fd);
    return makeError(StatusCode::kInvalidArg, "CXL DAX mapping offset exceeds device size");
  }
  if (length == 0) {
    length = capacity - offset;
  }
  auto end = checkedEnd(offset, length);
  if (!end || *end > capacity) {
    ::close(fd);
    return makeError(StatusCode::kInvalidArg, "CXL DAX mapping exceeds device size");
  }
  return mapOpenedFd(fd, offset, length, requiredAlignment);
}

Result<std::span<std::byte>> CxlRegion::checkedRange(uint64_t offset, uint64_t length, size_t alignment) {
  if (alignment == 0 || (alignment & (alignment - 1U)) != 0) {
    return makeError(StatusCode::kInvalidArg, "CXL range alignment must be a nonzero power of two");
  }
  auto end = checkedEnd(offset, length);
  if (!end || *end > size_ || offset > std::numeric_limits<size_t>::max() ||
      length > std::numeric_limits<size_t>::max()) {
    return makeError(StatusCode::kInvalidArg, "CXL range is outside the mapped region");
  }
  auto *begin = data_ + static_cast<size_t>(offset);
  if (reinterpret_cast<uintptr_t>(begin) % alignment != 0) {
    return makeError(StatusCode::kInvalidArg, "CXL range address is not aligned");
  }
  return std::span<std::byte>(begin, static_cast<size_t>(length));
}

Result<std::span<const std::byte>> CxlRegion::checkedRange(uint64_t offset, uint64_t length, size_t alignment) const {
  auto range = const_cast<CxlRegion *>(this)->checkedRange(offset, length, alignment);
  if (!range) {
    return makeError(std::move(range.error()));
  }
  return std::span<const std::byte>(range->data(), range->size());
}

void CxlRegion::reset() noexcept {
  if (data_ != nullptr) {
    ::munmap(data_, size_);
  }
  if (fd_ >= 0) {
    ::close(fd_);
  }
  fd_ = -1;
  data_ = nullptr;
  size_ = 0;
}

}  // namespace hf3fs::net::cxl
