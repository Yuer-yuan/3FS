#pragma once

#include <array>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <fcntl.h>
#include <mutex>
#include <string>
#include <string_view>
#include <sys/syscall.h>
#include <unistd.h>

namespace hf3fs::net {

// Diagnostic-only, process-local records; no changes to RPC framing/shared ABI.
// A fixed header and a capped append area make lost records explicit. Export
// after quiescence, or reject a snapshot whose header/sequence count disagrees.
class RpcTrace {
 public:
  static constexpr size_t kHeaderBytes = 256;
  static constexpr size_t kCapacityBytes = 4 * 1024 * 1024;
  explicit RpcTrace(const std::string &path, size_t capacity = kCapacityBytes)
      : capacity_(capacity), fd_(::open(path.c_str(), O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC, 0600)) {
    if (fd_ >= 0) header();
  }
  ~RpcTrace() { if (fd_ >= 0) ::close(fd_); }
  RpcTrace(const RpcTrace &) = delete;
  RpcTrace &operator=(const RpcTrace &) = delete;

  static bool enabled() {
    const char *value = std::getenv("HF3FS_RPC_TRACE_DIR");
    return value != nullptr && value[0] != '\0';
  }
  static RpcTrace &process() {
    static RpcTrace instance(std::string(std::getenv("HF3FS_RPC_TRACE_DIR")) + "/rpc-" +
                             std::to_string(::getpid()) + ".trace");
    return instance;
  }
  void record(uint16_t service, uint16_t method, uint64_t uuid, std::string_view stage,
              uint32_t status, std::string_view detail) {
    std::lock_guard lock(mutex_);
    ++attempted_;
    std::array<char, 1536> line{};
    auto ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
    int length = std::snprintf(line.data(), line.size(),
        "seq=%llu mono_ns=%lld pid=%ld tid=%ld service=%u method=%u uuid=%llu stage=%.*s status=%u %.*s\n",
        static_cast<unsigned long long>(attempted_), static_cast<long long>(ns),
        static_cast<long>(::getpid()), static_cast<long>(::syscall(SYS_gettid)), service, method,
        static_cast<unsigned long long>(uuid), static_cast<int>(stage.size()), stage.data(), status,
        static_cast<int>(detail.size()), detail.data());
    if (length <= 0 || static_cast<size_t>(length) >= line.size() ||
        bytes_ + static_cast<size_t>(length) > capacity_) {
      ++dropped_;
    } else {
      // Multiline error descriptions must not create apparently separate events.
      for (int i = 0; i < length - 1; ++i) if (line[i] == '\n' || line[i] == '\r') line[i] = ' ';
      if (writeAt(line.data(), length, kHeaderBytes + bytes_)) {
        bytes_ += length;
        ++committed_;
      } else {
        ++errors_;
      }
    }
    header();
  }

 private:
  bool writeAt(const char *data, size_t bytes, size_t offset) {
    while (bytes) {
      auto count = ::pwrite(fd_, data, bytes, offset);
      if (count < 0 && errno == EINTR) continue;
      if (count <= 0) return false;
      data += count;
      bytes -= count;
      offset += count;
    }
    return true;
  }
  void header() {
    std::array<char, kHeaderBytes> value{};
    value.fill(' ');
    int count = std::snprintf(value.data(), value.size(),
        "HF3FS_RPC_TRACE_V1 attempted=%llu committed=%llu dropped=%llu errors=%llu bytes=%llu capacity=%llu",
        static_cast<unsigned long long>(attempted_), static_cast<unsigned long long>(committed_),
        static_cast<unsigned long long>(dropped_), static_cast<unsigned long long>(errors_),
        static_cast<unsigned long long>(bytes_), static_cast<unsigned long long>(capacity_));
    if (count > 0 && static_cast<size_t>(count) < value.size()) value[count] = ' ';
    value.back() = '\n';
    if (!writeAt(value.data(), value.size(), 0)) ++errors_;
  }
  std::mutex mutex_;
  size_t capacity_, bytes_ = 0;
  uint64_t attempted_ = 0, committed_ = 0, dropped_ = 0, errors_ = 0;
  int fd_;
};

inline bool tracedRpc(uint16_t service, uint16_t method) {
  if (!RpcTrace::enabled()) return false;
  const char *selected = std::getenv("HF3FS_RPC_TRACE_METHODS");
  if (selected == nullptr || selected[0] == '\0') {
    return service == 4 && (method == 2 || method == 3 || method == 50);
  }
  // Diagnostic-only selector: a comma-separated list of exact service:method
  // pairs. Malformed entries are ignored rather than widening trace coverage.
  const char *cursor = selected;
  while (*cursor != '\0') {
    char *serviceEnd = nullptr;
    errno = 0;
    auto selectedService = std::strtoul(cursor, &serviceEnd, 10);
    if (errno == 0 && serviceEnd != cursor && *serviceEnd == ':') {
      char *methodEnd = nullptr;
      errno = 0;
      auto selectedMethod = std::strtoul(serviceEnd + 1, &methodEnd, 10);
      if (errno == 0 && methodEnd != serviceEnd + 1 &&
          (*methodEnd == ',' || *methodEnd == '\0') && selectedService == service &&
          selectedMethod == method) {
        return true;
      }
    }
    while (*cursor != '\0' && *cursor != ',') ++cursor;
    if (*cursor == ',') ++cursor;
  }
  return false;
}
inline void rpcTrace(uint16_t service, uint16_t method, uint64_t uuid, std::string_view stage,
                     uint32_t status = 0, std::string_view detail = {}) {
  if (RpcTrace::enabled()) RpcTrace::process().record(service, method, uuid, stage, status, detail);
}
}  // namespace hf3fs::net
