#pragma once

#include <chrono>
#include <filesystem>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <sys/types.h>
#include <vector>

namespace hf3fs::test {

// Test setup/introspection only. Product RPC and bulk data never use this
// channel. A single deadline bounds a whole length-prefixed message.
class CxlTestChannel {
 public:
  using Deadline = std::chrono::steady_clock::time_point;
  static constexpr size_t kMaxFrameBytes = 8 * 1024 * 1024;
  explicit CxlTestChannel(int fd)
      : fd_(fd) {}
  ~CxlTestChannel();
  CxlTestChannel(const CxlTestChannel &) = delete;
  CxlTestChannel &operator=(const CxlTestChannel &) = delete;
  void send(std::string_view message, Deadline deadline);
  std::string receive(Deadline deadline);

 private:
  void transfer(void *bytes, size_t length, bool sending, Deadline deadline);
  int fd_;
};

class CxlTestProcess {
 public:
  static constexpr std::string_view kReady = "HF3FS_CXL_TEST_PROCESS_READY";
  static std::unique_ptr<CxlTestProcess> launch(const std::filesystem::path &executable,
                                                const std::vector<std::string> &arguments,
                                                const std::filesystem::path &log,
                                                std::chrono::milliseconds timeout = std::chrono::seconds(30));
  ~CxlTestProcess();
  CxlTestProcess(const CxlTestProcess &) = delete;
  CxlTestProcess &operator=(const CxlTestProcess &) = delete;

  std::string request(std::string_view message);
  // Lets the service keep its existing mutation lock while a test transforms
  // the current snapshot. No other control request can interleave the exchange.
  std::string requestWithCallback(std::string_view message,
                                  const std::function<std::string(std::string_view)> &callback);
  int wait(std::chrono::milliseconds timeout = std::chrono::seconds(5));
  void terminate() noexcept;
  pid_t pid() const { return pid_; }
  bool forcedCleanup() const { return forcedCleanup_; }
  const std::filesystem::path &log() const { return log_; }

 private:
  CxlTestProcess(pid_t pid, int fd, std::filesystem::path log, std::chrono::milliseconds timeout);
  pid_t pid_;
  CxlTestChannel channel_;
  std::filesystem::path log_;
  std::chrono::milliseconds timeout_;
  std::mutex mutex_;
  bool usable_{true};
  bool forcedCleanup_{};
  int status_{-1};
};

}  // namespace hf3fs::test
