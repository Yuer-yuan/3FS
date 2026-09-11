// Opt-in POSIX frontend for trusted file-backed CXL IOVs. No heap remapping,
// kernel changes, storage shortcuts or replacement completion-wait loop.
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "hf3fs_posix.h"
#include "hf3fs_usrbio.h"
#include <algorithm>
#include <atomic>
#include <cerrno>
#include <climits>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <map>
#include <memory>
#include <mutex>
#include <new>
#include <pthread.h>
#include <string>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <unistd.h>

namespace {
static_assert(sizeof(off_t) == 8, "this frontend requires a 64-bit Linux ABI");
constexpr size_t kStageBytes = 32ul << 20;
thread_local unsigned inside = 0;
const pid_t loadedPid = getpid();
struct Guard { Guard() { ++inside; } ~Guard() { --inside; } };
struct ApiError { int code; };
[[noreturn]] void fail(int error) { throw ApiError{error}; }
void require(bool condition, int error) { if (!condition) fail(error); }
void checked(int code) { if (code) fail(code < 0 ? -code : code); }
void systemChecked(long result) { if (result < 0) fail(errno); }

struct Counters {
  std::atomic<uint64_t> directRead{}, directWrite{}, stagedRead{}, stagedWrite{};
  std::atomic<uint64_t> copyIn{}, copyOut{}, reads{}, writes{}, rejected{};
} counters;

bool enabled() {
  const char *mode = getenv("HF3FS_CXL_POSIX_MODE");
  return !inside && mode && strcmp(mode, "fuse") != 0;
}

bool inScope(int fd) {
  const int saved = errno;
  const char *mount = getenv("HF3FS_CXL_POSIX_MOUNT");
  if (!mount || !*mount || fd < 0) return false;
  char proc[64], path[PATH_MAX + 1];
  snprintf(proc, sizeof(proc), "/proc/self/fd/%d", fd);
  ssize_t n = syscall(SYS_readlinkat, AT_FDCWD, proc, path, PATH_MAX);
  size_t length = strlen(mount);
  while (length > 1 && mount[length - 1] == '/') --length;
  bool result = n > static_cast<ssize_t>(length) && !memcmp(path, mount, length) && path[length] == '/';
  // IOV/IOR control objects retain their original API handling.
  if (result && n >= static_cast<ssize_t>(length + 10) &&
      !memcmp(path + length, "/3fs-virt/", 10)) result = false;
  errno = saved;
  return result;
}

struct Configuration {
  std::string mount;
  pid_t pid = getpid();
  Configuration() {
    require(pid == loadedPid, EOPNOTSUPP);
    const char *value = getenv("HF3FS_CXL_POSIX_MOUNT");
    require(value && *value, EINVAL);
    char resolved[PATH_MAX];
    if (!realpath(value, resolved)) fail(errno);
    mount = resolved;
    const char *direct = getenv("HF3FS_CXL_NATIVE_IOV");
    require(direct && !strcmp(direct, "1"), EOPNOTSUPP);
    require(getuid() == 0 && geteuid() == 0, EACCES);
    const char *mode = getenv("HF3FS_CXL_POSIX_MODE");
    require(mode && (!strcmp(mode, "iov") || !strcmp(mode, "strict") || !strcmp(mode, "fuse")), EINVAL);
    // Shared IOR writes do not maintain kernel page-cache coherence.
    char valueBuf[32];
    std::string configPath = mount + "/3fs-virt/get-conf/usr.enable_read_cache";
    ssize_t n = readlink(configPath.c_str(), valueBuf, sizeof(valueBuf));
    require(n == 5 && !memcmp(valueBuf, "false", 5), EOPNOTSUPP);
  }
};
Configuration &configuration() {
  // Process-owned state outlives the API DSO's static destructors.
  static auto *config = new Configuration;
  require(getpid() == config->pid, EOPNOTSUPP);
  return *config;
}

struct Allocation {
  hf3fs_iov iov{};
  pid_t pid = getpid();
  explicit Allocation(size_t size) {
    require(size && size <= static_cast<size_t>(PTRDIFF_MAX), EINVAL);
    checked(hf3fs_iovcreate(&iov, configuration().mount.c_str(), size, 0, 1));
  }
  ~Allocation() {
    const int saved = errno;
    if (getpid() == pid) { Guard guard; hf3fs_iovdestroy(&iov); }
    errno = saved;
  }
};
struct AllocationTable {
  std::mutex mutex;
  std::map<uintptr_t, std::shared_ptr<Allocation>> values;
};
AllocationTable &allocations() { static auto *table = new AllocationTable; return *table; }

std::shared_ptr<Allocation> findAllocation(const void *ptr, size_t size) {
  auto &table = allocations();
  const auto address = reinterpret_cast<uintptr_t>(ptr);
  std::lock_guard<std::mutex> lock(table.mutex);
  auto next = table.values.upper_bound(address);
  if (next != table.values.begin()) {
    auto previous = std::prev(next);
    const size_t delta = address - previous->first;
    if (delta <= previous->second->iov.size) {
      require(size <= previous->second->iov.size - delta, EFAULT);
      return previous->second;
    }
  }
  // A partly registered request must not silently become a staging request.
  if (next != table.values.end()) require(size <= next->first - address, EFAULT);
  return {};
}

struct Description {
  std::mutex mutex;
  int pin = -1, registered = 0;
  bool registrationLive = false;
  pid_t pid = getpid();
  explicit Description(int fd) {
    configuration();
    require(hf3fs_is_hf3fs(fd), EXDEV);
    pin = static_cast<int>(syscall(SYS_fcntl, fd, F_DUPFD_CLOEXEC, 3));
    require(pin >= 0, errno);
    registered = hf3fs_reg_fd(pin, 0);
    if (registered > 0) {
      syscall(SYS_close, pin);
      pin = -1;
      fail(registered);
    }
    registrationLive = true;
  }
  ~Description() {
    if (getpid() != pid) return;
    const int saved = errno;
    Guard guard;
    if (registrationLive) hf3fs_dereg_fd(pin);
    if (pin >= 0) syscall(SYS_close, pin);
    errno = saved;
  }
};
struct DescriptorTable {
  std::mutex mutex;
  std::map<int, std::shared_ptr<Description>> values;
};
DescriptorTable &descriptors() { static auto *table = new DescriptorTable; return *table; }

std::shared_ptr<Description> findDescription(int fd) {
  require(getpid() == loadedPid, EOPNOTSUPP);
  auto &table = descriptors();
  {
    std::lock_guard<std::mutex> lock(table.mutex);
    auto found = table.values.find(fd);
    if (found != table.values.end()) return found->second;
  }
  if (!inScope(fd)) return {};
  struct stat st{};
  systemChecked(syscall(SYS_fstat, fd, &st));
  if (!S_ISREG(st.st_mode)) return {};
  auto owner = std::make_shared<Description>(fd);
  std::lock_guard<std::mutex> lock(table.mutex);
  return table.values.emplace(fd, std::move(owner)).first->second;
}

struct IoContext {
  hf3fs_ior readRing{}, writeRing{};
  bool haveRead = false, haveWrite = false, poisoned = false;
  std::shared_ptr<Allocation> stage, pendingBuffer;
  std::shared_ptr<Description> pendingFile;
  hf3fs_ior &ring(bool reading) {
    require(!poisoned, EIO);
    auto &result = reading ? readRing : writeRing;
    auto &exists = reading ? haveRead : haveWrite;
    if (!exists) {
      checked(hf3fs_iorcreate4(&result, configuration().mount.c_str(), 8, reading, 0, 0, 1, 0));
      exists = true;
    }
    return result;
  }
  ~IoContext() {
    Guard guard;
    if (haveRead) hf3fs_iordestroy(&readRing);
    if (haveWrite) hf3fs_iordestroy(&writeRing);
  }
};
struct ThreadIo {
  IoContext *value = nullptr;
  ~ThreadIo() {
    // A failed submit/wait is not cancellation. Quarantine owners until process
    // exit instead of unmapping memory the server might still be using.
    if (value && getpid() == loadedPid && !value->poisoned) delete value;
  }
};
thread_local ThreadIo threadIo;
struct CancellationBlock {
  int previous;
  CancellationBlock() { pthread_setcancelstate(PTHREAD_CANCEL_DISABLE, &previous); }
  ~CancellationBlock() { pthread_setcancelstate(previous, nullptr); }
};

ssize_t transferOne(IoContext &context, const std::shared_ptr<Description> &file,
                    const std::shared_ptr<Allocation> &buffer, bool reading,
                    void *pointer, size_t length, off_t offset) {
  auto &ring = context.ring(reading);
  int index = hf3fs_prep_io(&ring, &buffer->iov, reading, pointer, file->registered, offset, length, &context);
  if (index < 0) fail(-index);
  context.pendingBuffer = buffer;
  context.pendingFile = file;
  context.poisoned = true;  // Cleared only by the matching completion.
  checked(hf3fs_submit_ios(&ring));
  hf3fs_cqe completion{};
  int count = hf3fs_wait_for_ios(&ring, &completion, 1, 1, nullptr);
  require(count == 1 && completion.index == index && completion.userdata == &context &&
          completion.result <= static_cast<int64_t>(length), EIO);
  context.poisoned = false;
  context.pendingBuffer.reset();
  context.pendingFile.reset();
  if (completion.result < 0) fail(static_cast<int>(-completion.result));
  return completion.result;
}

ssize_t safeCopy(void *destination, const void *source, size_t size, bool copyIn) {
  // Preserve EFAULT for inaccessible application memory without signal handlers.
  iovec local{copyIn ? destination : const_cast<void *>(source), size};
  iovec remote{copyIn ? const_cast<void *>(source) : destination, size};
  ssize_t copied = syscall(copyIn ? SYS_process_vm_readv : SYS_process_vm_writev,
                           getpid(), &local, 1, &remote, 1, 0);
  if (copied > 0) (copyIn ? counters.copyIn : counters.copyOut).fetch_add(copied, std::memory_order_relaxed);
  return copied;
}

ssize_t perform(const std::shared_ptr<Description> &file, void *pointer, size_t length,
                off_t offset, bool reading) {
  int flags = static_cast<int>(syscall(SYS_fcntl, file->pin, F_GETFL, 0));
  require(flags >= 0, errno);
  require(reading ? (flags & O_ACCMODE) != O_WRONLY : (flags & O_ACCMODE) != O_RDONLY, EBADF);
  require(!(flags & O_APPEND), EOPNOTSUPP);
  require(offset >= 0 && length <= static_cast<size_t>(SSIZE_MAX) &&
          length <= static_cast<uint64_t>(INT64_MAX - offset), EINVAL);
  if (length == 0) return 0;
  require(pointer && reinterpret_cast<uintptr_t>(pointer) <= UINTPTR_MAX - length, EFAULT);
  auto owner = findAllocation(pointer, length);
  const bool direct = static_cast<bool>(owner);
  const char *mode = getenv("HF3FS_CXL_POSIX_MODE");
  require(direct || !mode || strcmp(mode, "strict") != 0, EOPNOTSUPP);
  if (!threadIo.value) threadIo.value = new IoContext;
  auto &context = *threadIo.value;
  require(!context.poisoned, EIO);
  if (!direct) {
    if (!context.stage) context.stage = std::make_shared<Allocation>(kStageBytes);
    owner = context.stage;
  }
  (reading ? counters.reads : counters.writes).fetch_add(1, std::memory_order_relaxed);
  size_t done = 0;
  while (done < length) {
    size_t amount = std::min(length - done, kStageBytes);
    auto *application = static_cast<unsigned char *>(pointer) + done;
    auto *data = direct ? application : owner->iov.base;
    try {
      if (!direct && !reading) require(safeCopy(data, application, amount, true) == static_cast<ssize_t>(amount), EFAULT);
      ssize_t result = transferOne(context, file, owner, reading, data, amount, offset + done);
      (direct ? (reading ? counters.directRead : counters.directWrite)
              : (reading ? counters.stagedRead : counters.stagedWrite)).fetch_add(result, std::memory_order_relaxed);
      if (!direct && reading && result) {
        ssize_t copied = safeCopy(application, data, result, false);
        require(copied > 0, EFAULT);
        done += copied;
        if (copied != result) return done;
      } else done += result;
      if (result != static_cast<ssize_t>(amount)) break;
    } catch (ApiError error) {
      if (!done) throw;
      errno = error.code;
      break;  // Preserve a completed prefix instead of hiding it behind -1.
    }
  }
  if (!reading && done && (flags & O_DSYNC))
    systemChecked(syscall((flags & O_SYNC) == O_SYNC ? SYS_fsync : SYS_fdatasync, file->pin));
  return done;
}

ssize_t dataCall(int fd, void *pointer, size_t size, off_t offset, bool positioned, bool reading) {
  auto raw = [&]() -> ssize_t {
    if (positioned) return syscall(reading ? SYS_pread64 : SYS_pwrite64, fd, pointer, size, offset);
    return syscall(reading ? SYS_read : SYS_write, fd, pointer, size);
  };
  if (!enabled()) return raw();
  if (getpid() != loadedPid) {
    if (!inScope(fd)) return raw();
    errno = EOPNOTSUPP; return -1;
  }
  CancellationBlock cancellation;
  Guard guard;
  try {
    auto file = findDescription(fd);
    if (!file) return raw();
    std::lock_guard<std::mutex> lock(file->mutex);
    if (!positioned) {
      offset = syscall(SYS_lseek, file->pin, 0, SEEK_CUR);
      require(offset >= 0, errno);
    }
    ssize_t result = perform(file, pointer, size, offset, reading);
    if (!positioned && result > 0) systemChecked(syscall(SYS_lseek, file->pin, offset + result, SEEK_SET));
    return result;
  } catch (ApiError error) {
    counters.rejected.fetch_add(1, std::memory_order_relaxed);
    errno = error.code; return -1;
  } catch (const std::bad_alloc &) { errno = ENOMEM; return -1; }
}

template <typename Operation>
long metadataCall(int fd, Operation operation) {
  if (!enabled()) return operation(fd);
  if (getpid() != loadedPid) {
    if (!inScope(fd)) return operation(fd);
    errno = EOPNOTSUPP; return -1;
  }
  Guard guard;
  try {
    auto file = findDescription(fd);
    if (!file) return operation(fd);
    std::lock_guard<std::mutex> lock(file->mutex);
    return operation(file->pin);
  } catch (ApiError error) { errno = error.code; return -1; }
    catch (const std::bad_alloc &) { errno = ENOMEM; return -1; }
}

int duplicate(int oldfd, int newfd, int flags, int operation) {
  auto raw = [&]() -> int {
    if (operation == SYS_fcntl) return syscall(SYS_fcntl, oldfd, flags, newfd);
    if (operation == SYS_dup3) return syscall(SYS_dup3, oldfd, newfd, flags);
    if (operation == SYS_dup2) return syscall(SYS_dup2, oldfd, newfd);
    return syscall(SYS_dup, oldfd);
  };
  if (!enabled()) return raw();
  if (getpid() != loadedPid) {
    if (!inScope(oldfd)) return raw();
    errno = EOPNOTSUPP; return -1;
  }
  Guard guard;
  try {
    auto source = findDescription(oldfd);
    std::shared_ptr<Description> discarded;
    auto &table = descriptors();
    int result;
    {
      std::lock_guard<std::mutex> lock(table.mutex);
      result = raw();
      if (result >= 0 && result != oldfd) {
        auto previous = table.values.find(result);
        if (previous != table.values.end()) {
          discarded = std::move(previous->second);
          table.values.erase(previous);
        }
        if (source) table.values.emplace(result, std::move(source));
      }
    }
    return result;
  } catch (ApiError error) { errno = error.code; return -1; }
    catch (const std::bad_alloc &) { errno = ENOMEM; return -1; }
}

bool unsupported(int fd) {
  if (!enabled() || !inScope(fd)) return false;
  counters.rejected.fetch_add(1, std::memory_order_relaxed);
  errno = EOPNOTSUPP;
  return true;
}
}  // namespace

extern "C" void *hf3fs_posix_alloc(size_t size) {
  if (getpid() != loadedPid) { errno = EOPNOTSUPP; return nullptr; }
  Guard guard;
  try {
    configuration();
    auto owner = std::make_shared<Allocation>(size);
    void *pointer = owner->iov.base;
    auto &table = allocations();
    std::lock_guard<std::mutex> lock(table.mutex);
    require(table.values.emplace(reinterpret_cast<uintptr_t>(pointer), std::move(owner)).second, EEXIST);
    return pointer;
  } catch (ApiError error) { errno = error.code; return nullptr; }
    catch (const std::bad_alloc &) { errno = ENOMEM; return nullptr; }
}
extern "C" int hf3fs_posix_free(void *base) {
  if (getpid() != loadedPid) { errno = EOPNOTSUPP; return -1; }
  Guard guard;
  std::shared_ptr<Allocation> owner;
  auto &table = allocations();
  {
    std::lock_guard<std::mutex> lock(table.mutex);
    auto it = table.values.find(reinterpret_cast<uintptr_t>(base));
    if (it == table.values.end()) { errno = EINVAL; return -1; }
    owner = std::move(it->second);
    table.values.erase(it);
  }
  return 0;
}
extern "C" int hf3fs_posix_get_stats(hf3fs_posix_stats *out) {
  if (!out) { errno = EINVAL; return -1; }
  *out = {counters.directRead.load(), counters.directWrite.load(), counters.stagedRead.load(),
          counters.stagedWrite.load(), counters.copyIn.load(), counters.copyOut.load(),
          counters.reads.load(), counters.writes.load(), counters.rejected.load()};
  return 0;
}

// Optional process evidence for unmodified applications. No timing-path log or
// allocator hooks. Totals include setup/teardown; fork children do not duplicate
// the parent's inherited counters.
__attribute__((destructor)) static void reportPosixCounters() {
  const char *report = getenv("HF3FS_CXL_POSIX_REPORT");
  if (!report || strcmp(report, "1") || getpid() != loadedPid) return;
  hf3fs_posix_stats value{};
  hf3fs_posix_get_stats(&value);
  char line[1024];
  int length = snprintf(line, sizeof(line),
      "HF3FS_POSIX_COUNTERS {\"pid\":%ld,\"direct_read_bytes\":%llu,\"direct_write_bytes\":%llu,"
      "\"staged_read_bytes\":%llu,\"staged_write_bytes\":%llu,\"staging_copy_in_bytes\":%llu,"
      "\"staging_copy_out_bytes\":%llu,\"read_calls\":%llu,\"write_calls\":%llu,\"rejected_calls\":%llu}\n",
      static_cast<long>(getpid()), static_cast<unsigned long long>(value.direct_read_bytes),
      static_cast<unsigned long long>(value.direct_write_bytes),
      static_cast<unsigned long long>(value.staged_read_bytes),
      static_cast<unsigned long long>(value.staged_write_bytes),
      static_cast<unsigned long long>(value.staging_copy_in_bytes),
      static_cast<unsigned long long>(value.staging_copy_out_bytes),
      static_cast<unsigned long long>(value.read_calls), static_cast<unsigned long long>(value.write_calls),
      static_cast<unsigned long long>(value.rejected_calls));
  if (length > 0 && length < static_cast<int>(sizeof(line))) syscall(SYS_write, STDERR_FILENO, line, length);
}

extern "C" ssize_t read(int fd, void *buf, size_t count) { return dataCall(fd, buf, count, 0, false, true); }
extern "C" ssize_t write(int fd, const void *buf, size_t count) {
  return dataCall(fd, const_cast<void *>(buf), count, 0, false, false);
}
extern "C" ssize_t pread(int fd, void *buf, size_t count, off_t offset) {
  return dataCall(fd, buf, count, offset, true, true);
}
extern "C" ssize_t pwrite(int fd, const void *buf, size_t count, off_t offset) {
  return dataCall(fd, const_cast<void *>(buf), count, offset, true, false);
}
extern "C" ssize_t pread64(int fd, void *buf, size_t count, off64_t offset) { return pread(fd, buf, count, offset); }
extern "C" ssize_t pwrite64(int fd, const void *buf, size_t count, off64_t offset) { return pwrite(fd, buf, count, offset); }
extern "C" off_t lseek(int fd, off_t offset, int whence) noexcept {
  return metadataCall(fd, [&](int pin) { return syscall(SYS_lseek, pin, offset, whence); });
}
extern "C" off64_t lseek64(int fd, off64_t offset, int whence) noexcept { return lseek(fd, offset, whence); }
extern "C" int fsync(int fd) { return metadataCall(fd, [](int pin) { return syscall(SYS_fsync, pin); }); }
extern "C" int fdatasync(int fd) { return metadataCall(fd, [](int pin) { return syscall(SYS_fdatasync, pin); }); }
extern "C" int ftruncate(int fd, off_t size) noexcept {
  return metadataCall(fd, [&](int pin) { return syscall(SYS_ftruncate, pin, size); });
}
extern "C" int ftruncate64(int fd, off64_t size) noexcept { return ftruncate(fd, size); }
extern "C" int close(int fd) {
  if (!enabled() || getpid() != loadedPid) return syscall(SYS_close, fd);
  Guard guard;
  std::shared_ptr<Description> discarded;
  auto &table = descriptors();
  {
    std::lock_guard<std::mutex> lock(table.mutex);
    auto it = table.values.find(fd);
    if (it != table.values.end()) {
      discarded = std::move(it->second);
      table.values.erase(it);
    }
  }
  return syscall(SYS_close, fd);
}
extern "C" int dup(int fd) noexcept { return duplicate(fd, 0, 0, SYS_dup); }
extern "C" int dup2(int oldfd, int newfd) noexcept { return duplicate(oldfd, newfd, 0, SYS_dup2); }
extern "C" int dup3(int oldfd, int newfd, int flags) noexcept { return duplicate(oldfd, newfd, flags, SYS_dup3); }

static int dispatchFcntl(int fd, int command, va_list args) {
  unsigned long argument = 0;
  switch (command) {
    case F_GETFD: case F_GETFL: case F_GETOWN: case F_GETSIG: case F_GETLEASE:
    case F_GETPIPE_SZ: case F_GET_SEALS: break;
    case F_DUPFD: case F_DUPFD_CLOEXEC: case F_SETFD: case F_SETFL: case F_SETOWN:
    case F_SETSIG: case F_SETLEASE: case F_NOTIFY: case F_SETPIPE_SZ: case F_ADD_SEALS:
      argument = static_cast<unsigned long>(va_arg(args, int)); break;
    default: argument = reinterpret_cast<unsigned long>(va_arg(args, void *)); break;
  }
  if (command == F_DUPFD || command == F_DUPFD_CLOEXEC)
    return duplicate(fd, static_cast<int>(argument), command, SYS_fcntl);
  return syscall(SYS_fcntl, fd, command, argument);
}
extern "C" int fcntl(int fd, int command, ...) {
  va_list args;
  va_start(args, command);
  int result = dispatchFcntl(fd, command, args);
  va_end(args);
  return result;
}
extern "C" int fcntl64(int fd, int command, ...) {
  va_list args;
  va_start(args, command);
  int result = dispatchFcntl(fd, command, args);
  va_end(args);
  return result;
}
extern "C" ssize_t readv(int fd, const iovec *iov, int count) {
  if (unsupported(fd)) return -1;
  return syscall(SYS_readv, fd, iov, count);
}
extern "C" ssize_t writev(int fd, const iovec *iov, int count) {
  if (unsupported(fd)) return -1;
  return syscall(SYS_writev, fd, iov, count);
}
#define HF3FS_POSITIONED_VECTOR(name, number)                                             \
  extern "C" ssize_t name(int fd, const iovec *iov, int count, off_t offset) {             \
    if (unsupported(fd)) return -1;                                                     \
    auto value = static_cast<uint64_t>(offset);                                          \
    return syscall(number, fd, iov, count, static_cast<unsigned long>(value & UINT32_MAX),\
                   static_cast<unsigned long>(value >> 32));                            \
  }
HF3FS_POSITIONED_VECTOR(preadv, SYS_preadv)
HF3FS_POSITIONED_VECTOR(pwritev, SYS_pwritev)
HF3FS_POSITIONED_VECTOR(preadv64, SYS_preadv)
HF3FS_POSITIONED_VECTOR(pwritev64, SYS_pwritev)
#undef HF3FS_POSITIONED_VECTOR

#define HF3FS_POSITIONED_VECTOR2(name, number)                                            \
  extern "C" ssize_t name(int fd, const iovec *iov, int count, off_t offset, int flags) {  \
    if (unsupported(fd)) return -1;                                                     \
    auto value = static_cast<uint64_t>(offset);                                          \
    return syscall(number, fd, iov, count, static_cast<unsigned long>(value & UINT32_MAX),\
                   static_cast<unsigned long>(value >> 32), flags);                     \
  }
HF3FS_POSITIONED_VECTOR2(preadv2, SYS_preadv2)
HF3FS_POSITIONED_VECTOR2(pwritev2, SYS_pwritev2)
HF3FS_POSITIONED_VECTOR2(preadv64v2, SYS_preadv2)
HF3FS_POSITIONED_VECTOR2(pwritev64v2, SYS_pwritev2)
#undef HF3FS_POSITIONED_VECTOR2

extern "C" int close_range(unsigned first, unsigned last, int flags) noexcept {
  // A broad close could release internal IOV leases while buffers remain mapped.
  if (enabled() && getpid() == loadedPid) { errno = EOPNOTSUPP; return -1; }
  return syscall(SYS_close_range, first, last, flags);
}
extern "C" void *mmap(void *address, size_t length, int protection, int flags, int fd, off_t offset) noexcept {
  if (!(flags & MAP_ANONYMOUS) && unsupported(fd)) return MAP_FAILED;
  return reinterpret_cast<void *>(syscall(SYS_mmap, address, length, protection, flags, fd, offset));
}
extern "C" void *mmap64(void *address, size_t length, int protection, int flags, int fd, off64_t offset) noexcept {
  return mmap(address, length, protection, flags, fd, offset);
}
