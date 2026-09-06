#pragma once

#include <array>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <spawn.h>
#include <string>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>

extern char **environ;

namespace hf3fs::net::test {

// The gtest process can already have Folly worker threads. posix_spawn keeps
// allocator and executor work out of a forked child before exec.
inline int runCxlTransportFixture(const char *mode) {
  std::array<char, 4096> executable{};
  const auto size = ::readlink("/proc/self/exe", executable.data(), executable.size() - 1U);
  if (size <= 0) {
    return -errno;
  }
  const auto separator = std::string(executable.data()).find_last_of('/');
  if (separator == std::string::npos) {
    return -EINVAL;
  }
  const auto helper = std::string(executable.data()).substr(0, separator + 1U) + "cxl_transport_e2e_helper";
  std::array<char *, 3> arguments{const_cast<char *>(helper.c_str()), const_cast<char *>(mode), nullptr};
  posix_spawnattr_t attributes;
  int prepared = ::posix_spawnattr_init(&attributes);
  if (prepared != 0) {
    return -prepared;
  }
  prepared = ::posix_spawnattr_setflags(&attributes, POSIX_SPAWN_SETPGROUP);
  if (prepared == 0) {
    prepared = ::posix_spawnattr_setpgroup(&attributes, 0);
  }
  pid_t child = -1;
  const int spawned =
      prepared == 0 ? ::posix_spawn(&child, helper.c_str(), nullptr, &attributes, arguments.data(), environ) : prepared;
  ::posix_spawnattr_destroy(&attributes);
  if (spawned != 0) {
    return -spawned;
  }
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(120);
  int status = 0;
  while (std::chrono::steady_clock::now() < deadline) {
    const auto waited = ::waitpid(child, &status, WNOHANG);
    if (waited == child) {
      return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
    }
    if (waited < 0 && errno != EINTR) {
      return -errno;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  }
  // The orchestrator and its exec children are all in this owned group.
  ::kill(-child, SIGKILL);
  while (::waitpid(child, &status, 0) < 0 && errno == EINTR) {
  }
  return -ETIMEDOUT;
}

}  // namespace hf3fs::net::test
