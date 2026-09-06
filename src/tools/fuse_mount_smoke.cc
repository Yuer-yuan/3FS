#define FUSE_USE_VERSION 35
#include <fuse3/fuse_lowlevel.h>

#include <atomic>
#include <cerrno>
#include <cstddef>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

namespace {

constexpr fuse_ino_t kRootInode = 1;
constexpr fuse_ino_t kSentinelInode = 2;
constexpr std::string_view kSentinelName = "sentinel";
constexpr std::string_view kSentinelContents = "riscv-fuse-ok";

struct ProbeState {
  std::atomic<bool> lookupSeen{false};
  std::atomic<bool> openSeen{false};
  std::atomic<bool> readSeen{false};
};

void fillAttributes(fuse_ino_t inode, struct stat &attributes) {
  std::memset(&attributes, 0, sizeof(attributes));
  attributes.st_ino = static_cast<ino_t>(inode);
  if (inode == kRootInode) {
    attributes.st_mode = S_IFDIR | 0755;
    attributes.st_nlink = 2;
  } else {
    attributes.st_mode = S_IFREG | 0444;
    attributes.st_nlink = 1;
    attributes.st_size = static_cast<off_t>(kSentinelContents.size());
  }
}

void lookup(fuse_req_t request, fuse_ino_t parent, const char *name) {
  auto *state = static_cast<ProbeState *>(fuse_req_userdata(request));
  if (parent != kRootInode || name == nullptr || name != kSentinelName) {
    fuse_reply_err(request, ENOENT);
    return;
  }
  state->lookupSeen.store(true, std::memory_order_relaxed);
  struct fuse_entry_param entry {};
  entry.ino = kSentinelInode;
  entry.generation = 1;
  entry.attr_timeout = 0;
  entry.entry_timeout = 0;
  fillAttributes(kSentinelInode, entry.attr);
  fuse_reply_entry(request, &entry);
}

void getattr(fuse_req_t request, fuse_ino_t inode, struct fuse_file_info *) {
  if (inode != kRootInode && inode != kSentinelInode) {
    fuse_reply_err(request, ENOENT);
    return;
  }
  struct stat attributes {};
  fillAttributes(inode, attributes);
  fuse_reply_attr(request, &attributes, 0);
}

void openFile(fuse_req_t request, fuse_ino_t inode, struct fuse_file_info *info) {
  if (inode != kSentinelInode) {
    fuse_reply_err(request, EISDIR);
    return;
  }
  if ((info->flags & O_ACCMODE) != O_RDONLY) {
    fuse_reply_err(request, EACCES);
    return;
  }
  auto *state = static_cast<ProbeState *>(fuse_req_userdata(request));
  state->openSeen.store(true, std::memory_order_relaxed);
  info->direct_io = 1;
  fuse_reply_open(request, info);
}

void readFile(fuse_req_t request,
              fuse_ino_t inode,
              size_t size,
              off_t offset,
              struct fuse_file_info *) {
  if (inode != kSentinelInode || offset < 0) {
    fuse_reply_err(request, EINVAL);
    return;
  }
  auto *state = static_cast<ProbeState *>(fuse_req_userdata(request));
  state->readSeen.store(true, std::memory_order_relaxed);
  const auto start = static_cast<size_t>(offset);
  if (start >= kSentinelContents.size()) {
    fuse_reply_buf(request, nullptr, 0);
    return;
  }
  const size_t available = kSentinelContents.size() - start;
  const size_t count = std::min(size, available);
  fuse_reply_buf(request, kSentinelContents.data() + start, count);
}

std::filesystem::path parseMountpoint(int argc, char **argv) {
  if (argc != 3 || std::string_view(argv[1]) != "--mountpoint" ||
      std::string_view(argv[2]).empty()) {
    throw std::invalid_argument("usage: fuse_mount_smoke --mountpoint PATH");
  }
  std::filesystem::path mountpoint(argv[2]);
  if (!mountpoint.is_absolute()) {
    throw std::invalid_argument("mountpoint must be absolute");
  }
  std::error_code error;
  std::filesystem::create_directories(mountpoint, error);
  if (error || !std::filesystem::is_directory(mountpoint)) {
    throw std::runtime_error("cannot create mountpoint");
  }
  return mountpoint;
}

std::string readSentinel(const std::filesystem::path &mountpoint) {
  const auto target = mountpoint / kSentinelName;
  const int descriptor = ::open(target.c_str(), O_RDONLY | O_CLOEXEC);
  if (descriptor < 0) {
    throw std::runtime_error("cannot open mounted sentinel: " +
                             std::string(std::strerror(errno)));
  }
  char buffer[64]{};
  const ssize_t count = ::read(descriptor, buffer, sizeof(buffer));
  const int savedErrno = errno;
  ::close(descriptor);
  if (count < 0) {
    throw std::runtime_error("cannot read mounted sentinel: " +
                             std::string(std::strerror(savedErrno)));
  }
  return std::string(buffer, static_cast<size_t>(count));
}

int run(const std::filesystem::path &mountpoint, const char *program) {
  ProbeState state;
  struct fuse_lowlevel_ops operations {};
  operations.lookup = lookup;
  operations.getattr = getattr;
  operations.open = openFile;
  operations.read = readFile;

  char *fuseArgv[] = {const_cast<char *>(program)};
  struct fuse_args arguments = FUSE_ARGS_INIT(1, fuseArgv);
  fuse_session *session = fuse_session_new(&arguments, &operations, sizeof(operations), &state);
  fuse_opt_free_args(&arguments);
  if (session == nullptr) {
    throw std::runtime_error("fuse_session_new failed");
  }
  if (fuse_session_mount(session, mountpoint.c_str()) != 0) {
    fuse_session_destroy(session);
    throw std::runtime_error("fuse_session_mount failed");
  }

  int loopResult = -1;
  std::thread loop([&] { loopResult = fuse_session_loop(session); });
  std::string contents;
  try {
    contents = readSentinel(mountpoint);
  } catch (...) {
    fuse_session_exit(session);
    fuse_session_unmount(session);
    loop.join();
    fuse_session_destroy(session);
    throw;
  }
  fuse_session_exit(session);
  fuse_session_unmount(session);
  loop.join();
  fuse_session_destroy(session);

  const bool lookupSeen = state.lookupSeen.load(std::memory_order_relaxed);
  const bool openSeen = state.openSeen.load(std::memory_order_relaxed);
  const bool readSeen = state.readSeen.load(std::memory_order_relaxed);
  const bool passed = loopResult == 0 && contents == kSentinelContents && lookupSeen &&
                      openSeen && readSeen;
  std::cout << "{\"status\":\"" << (passed ? "passed" : "failed")
            << "\",\"lookup_seen\":" << (lookupSeen ? "true" : "false")
            << ",\"open_seen\":" << (openSeen ? "true" : "false")
            << ",\"read_seen\":" << (readSeen ? "true" : "false")
            << ",\"unmounted\":true,\"value_match\":"
            << (contents == kSentinelContents ? "true" : "false") << "}\n";
  return passed ? 0 : 1;
}

}  // namespace

int main(int argc, char **argv) {
  try {
    return run(parseMountpoint(argc, argv), argv[0]);
  } catch (const std::invalid_argument &error) {
    std::cerr << "fuse_mount_smoke: " << error.what() << '\n';
    return 2;
  } catch (const std::exception &error) {
    std::cerr << "fuse_mount_smoke: " << error.what() << '\n';
    return 1;
  }
}
