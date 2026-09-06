#include "CxlTestProcess.h"

#include <algorithm>
#include <arpa/inet.h>
#include <cerrno>
#include <charconv>
#include <csignal>
#include <cstdint>
#include <cstring>
#include <fcntl.h>
#include <fstream>
#include <poll.h>
#include <spawn.h>
#include <stdexcept>
#include <sys/socket.h>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>

extern char **environ;

namespace hf3fs::test {
namespace {

[[noreturn]] void fail(std::string_view operation, int error = errno) {
  throw std::runtime_error(std::string(operation) + ": " + std::strerror(error));
}

class OwnedFd {
 public:
  explicit OwnedFd(int fd)
      : fd_(fd) {}
  OwnedFd(const OwnedFd &) = delete;
  OwnedFd &operator=(const OwnedFd &) = delete;
  ~OwnedFd() {
    if (fd_ >= 0) ::close(fd_);
  }
  int get() const { return fd_; }
  int release() {
    const int fd = fd_;
    fd_ = -1;
    return fd;
  }

 private:
  int fd_;
};

int decodedStatus(int status) { return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status); }

bool otherGroupMembers(pid_t leader) {
  for (const auto &entry : std::filesystem::directory_iterator("/proc")) {
    const auto name = entry.path().filename().string();
    pid_t pid = -1;
    const auto parsed = std::from_chars(name.data(), name.data() + name.size(), pid);
    if (parsed.ec == std::errc{} && parsed.ptr == name.data() + name.size() && pid != leader &&
        ::getpgid(pid) == leader)
      return true;
  }
  return false;
}

}  // namespace

CxlTestChannel::~CxlTestChannel() { ::close(fd_); }

void CxlTestChannel::transfer(void *bytes, size_t length, bool sending, Deadline deadline) {
  auto *cursor = static_cast<char *>(bytes);
  while (length != 0) {
    const auto remaining =
        std::chrono::duration_cast<std::chrono::milliseconds>(deadline - std::chrono::steady_clock::now());
    if (remaining.count() <= 0) {
      fail("CXL fixture control deadline", ETIMEDOUT);
    }
    pollfd descriptor{fd_, static_cast<short>(sending ? POLLOUT : POLLIN), 0};
    const int ready = ::poll(&descriptor, 1, static_cast<int>(std::min<int64_t>(remaining.count(), INT32_MAX)));
    if (ready < 0) {
      if (errno == EINTR) continue;
      fail("CXL fixture poll");
    }
    if (ready == 0) continue;
    const auto count =
        sending ? ::send(fd_, cursor, length, MSG_NOSIGNAL | MSG_DONTWAIT) : ::recv(fd_, cursor, length, MSG_DONTWAIT);
    if (count == 0) fail("CXL fixture peer closed", EPIPE);
    if (count < 0) {
      if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) continue;
      fail("CXL fixture control I/O");
    }
    cursor += count;
    length -= count;
  }
}

void CxlTestChannel::send(std::string_view message, Deadline deadline) {
  if (message.empty() || message.size() > kMaxFrameBytes) {
    throw std::runtime_error("invalid CXL fixture control frame length");
  }
  uint32_t length = htonl(static_cast<uint32_t>(message.size()));
  transfer(&length, sizeof(length), true, deadline);
  transfer(const_cast<char *>(message.data()), message.size(), true, deadline);
}

std::string CxlTestChannel::receive(Deadline deadline) {
  uint32_t encoded = 0;
  transfer(&encoded, sizeof(encoded), false, deadline);
  const uint32_t length = ntohl(encoded);
  if (length == 0 || length > kMaxFrameBytes) {
    throw std::runtime_error("invalid CXL fixture control frame length");
  }
  std::string message(length, '\0');
  transfer(message.data(), message.size(), false, deadline);
  return message;
}

CxlTestProcess::CxlTestProcess(pid_t pid, int fd, std::filesystem::path log, std::chrono::milliseconds timeout)
    : pid_(pid),
      channel_(fd),
      log_(std::move(log)),
      timeout_(timeout) {}

std::unique_ptr<CxlTestProcess> CxlTestProcess::launch(const std::filesystem::path &executable,
                                                       const std::vector<std::string> &arguments,
                                                       const std::filesystem::path &log,
                                                       std::chrono::milliseconds timeout) {
  if (timeout.count() <= 0) throw std::runtime_error("CXL fixture timeout must be positive");
  int pair[2];
  if (::socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0, pair) != 0) fail("socketpair");
  OwnedFd parent(pair[0]), child(pair[1]);
  OwnedFd output(::open(log.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600));
  if (output.get() < 0) fail("create CXL fixture log");

  posix_spawn_file_actions_t actions;
  int error = ::posix_spawn_file_actions_init(&actions);
  if (error != 0) fail("posix_spawn_file_actions_init", error);
  posix_spawnattr_t attributes;
  error = ::posix_spawnattr_init(&attributes);
  if (error != 0) {
    ::posix_spawn_file_actions_destroy(&actions);
    fail("posix_spawnattr_init", error);
  }
  auto prepare = [&](int value) {
    if (error == 0) error = value;
  };
  constexpr int controlFd = 3;
  prepare(::posix_spawn_file_actions_addclose(&actions, parent.get()));
  prepare(::posix_spawn_file_actions_adddup2(&actions, child.get(), controlFd));
  if (child.get() != controlFd) prepare(::posix_spawn_file_actions_addclose(&actions, child.get()));
  prepare(::posix_spawn_file_actions_adddup2(&actions, output.get(), STDOUT_FILENO));
  prepare(::posix_spawn_file_actions_adddup2(&actions, output.get(), STDERR_FILENO));
  prepare(::posix_spawn_file_actions_addclose(&actions, output.get()));
  prepare(::posix_spawnattr_setflags(&attributes, POSIX_SPAWN_SETPGROUP));
  prepare(::posix_spawnattr_setpgroup(&attributes, 0));

  std::vector<std::string> values{executable.string(), std::to_string(controlFd)};
  values.insert(values.end(), arguments.begin(), arguments.end());
  std::vector<char *> argv;
  for (auto &value : values) argv.push_back(value.data());
  argv.push_back(nullptr);
  pid_t pid = -1;
  if (error == 0) error = ::posix_spawn(&pid, executable.c_str(), &actions, &attributes, argv.data(), environ);
  ::posix_spawnattr_destroy(&attributes);
  ::posix_spawn_file_actions_destroy(&actions);
  if (error != 0) fail("spawn CXL fixture helper", error);

  auto process = std::unique_ptr<CxlTestProcess>(new CxlTestProcess(pid, parent.release(), log, timeout));
  // Close the parent's copy of the child socket before waiting for EOF/READY.
  ::close(child.release());
  try {
    if (process->channel_.receive(std::chrono::steady_clock::now() + timeout) != kReady) {
      throw std::runtime_error("invalid helper startup response");
    }
  } catch (const std::exception &error) {
    process->terminate();
    throw std::runtime_error("CXL fixture startup failed, status=" + std::to_string(process->status_) + ": " +
                             error.what() + "; log=" + log.string());
  }
  return process;
}

CxlTestProcess::~CxlTestProcess() { terminate(); }

std::string CxlTestProcess::request(std::string_view message) { return requestWithCallback(message, {}); }

std::string CxlTestProcess::requestWithCallback(std::string_view message,
                                                const std::function<std::string(std::string_view)> &callback) {
  std::lock_guard lock(mutex_);
  if (!usable_ || pid_ <= 0)
    throw std::runtime_error("CXL fixture control channel is no longer usable: " + log_.string());
  const auto deadline = std::chrono::steady_clock::now() + timeout_;
  try {
    channel_.send(message, deadline);
    if (callback) {
      const auto snapshot = channel_.receive(deadline);
      channel_.send(callback(snapshot), deadline);
    }
    return channel_.receive(deadline);
  } catch (...) {
    usable_ = false;
    throw;
  }
}

int CxlTestProcess::wait(std::chrono::milliseconds timeout) {
  if (pid_ <= 0) return status_;
  const auto deadline = std::chrono::steady_clock::now() + timeout;
  while (std::chrono::steady_clock::now() < deadline) {
    siginfo_t info{};
    const int waited = ::waitid(P_PID, pid_, &info, WEXITED | WNOHANG | WNOWAIT);
    if (waited == 0 && info.si_pid == pid_) {
      terminate();
      if (forcedCleanup_) throw std::runtime_error("CXL fixture helper left child processes: " + log_.string());
      return status_;
    }
    if (waited < 0 && errno != EINTR) fail("wait for CXL fixture helper");
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  }
  terminate();
  throw std::runtime_error("CXL fixture helper required forced cleanup: " + log_.string());
}

void CxlTestProcess::terminate() noexcept {
  if (pid_ <= 0) return;
  int status = 0;
  siginfo_t info{};
  int observed;
  do {
    observed = ::waitid(P_PID, pid_, &info, WEXITED | WNOHANG | WNOWAIT);
  } while (observed < 0 && errno == EINTR);
  if (observed < 0) {
    pid_ = -1;
    usable_ = false;
    return;
  }
  bool descendants = true;
  try {
    descendants = otherGroupMembers(pid_);
  } catch (...) {
  }
  // Keep the leader unreaped until signals have been sent, so its process
  // group identity cannot be recycled underneath this cleanup.
  if (info.si_pid == 0 || descendants) {
    forcedCleanup_ = true;
    ::kill(-pid_, SIGKILL);
  }
  pid_t waited;
  do {
    waited = ::waitpid(pid_, &status, 0);
  } while (waited < 0 && errno == EINTR);
  if (waited > 0) status_ = decodedStatus(status);
  try {
    std::ofstream(log_, std::ios::app) << "\nHF3FS_CXL_FIXTURE_EXIT pid=" << pid_ << " status=" << status_
                                       << " forced=" << forcedCleanup_ << '\n';
  } catch (...) {
  }
  pid_ = -1;
  usable_ = false;
}

}  // namespace hf3fs::test
