#include <atomic>
#include <cerrno>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>

namespace {

constexpr uint64_t kMagic = 0x484633465343584cULL;  // "HF3FSCXL"

struct alignas(64) SmokeHeader {
  uint64_t magic;
  uint64_t generation;
  uint64_t length;
  uint64_t checksum;
  uint32_t atomic32Probe;
  uint32_t reserved0;
  uint64_t payloadLength;
  uint64_t reserved;
  uint64_t published;
};

static_assert(sizeof(SmokeHeader) == 64);
static_assert(alignof(SmokeHeader) == 64);
static_assert(std::atomic_ref<uint32_t>::is_always_lock_free);
static_assert(std::atomic_ref<uint64_t>::is_always_lock_free);

class CliError : public std::runtime_error {
 public:
  using std::runtime_error::runtime_error;
};

struct Options {
  std::filesystem::path device;
  uint64_t offset = 0;
  uint64_t length = 0;
  uint64_t generation = 0;
  uint64_t timeoutMs = 5000;
  uint64_t payloadBytes = 0;
  std::string role;
};

uint64_t parseUnsigned(std::string_view text, std::string_view name) {
  uint64_t value = 0;
  const auto result = std::from_chars(text.data(), text.data() + text.size(), value);
  if (text.empty() || result.ec != std::errc() || result.ptr != text.data() + text.size()) {
    throw CliError(std::string(name) + " must be an unsigned decimal integer");
  }
  return value;
}

Options parseOptions(int argc, char **argv) {
  Options options;
  for (int index = 1; index < argc; index += 2) {
    if (index + 1 >= argc) {
      throw CliError(std::string("missing value for ") + argv[index]);
    }
    const std::string_view name(argv[index]);
    const std::string_view value(argv[index + 1]);
    if (name == "--device") {
      options.device = value;
    } else if (name == "--offset") {
      options.offset = parseUnsigned(value, "offset");
    } else if (name == "--length") {
      options.length = parseUnsigned(value, "length");
    } else if (name == "--role") {
      options.role = value;
    } else if (name == "--generation") {
      options.generation = parseUnsigned(value, "generation");
    } else if (name == "--timeout-ms") {
      options.timeoutMs = parseUnsigned(value, "timeout-ms");
    } else if (name == "--payload-bytes") {
      options.payloadBytes = parseUnsigned(value, "payload-bytes");
    } else {
      throw CliError("unknown argument: " + std::string(name));
    }
  }
  if (options.device.empty()) {
    throw CliError("--device is required");
  }
  if (options.role != "writer" && options.role != "reader") {
    throw CliError("--role must be writer or reader");
  }
  if (options.length < sizeof(SmokeHeader)) {
    throw CliError("length must include the 64-byte smoke header");
  }
  if (options.generation == 0) {
    throw CliError("generation must be nonzero");
  }
  if (options.timeoutMs == 0) {
    throw CliError("timeout-ms must be nonzero");
  }
  if (options.payloadBytes > options.length - sizeof(SmokeHeader)) {
    throw CliError("payload-bytes exceeds the mapped payload capacity");
  }
  if (options.offset > std::numeric_limits<uint64_t>::max() - options.length) {
    throw CliError("offset plus length overflows");
  }
  const long page = ::sysconf(_SC_PAGESIZE);
  if (page <= 0) {
    throw std::runtime_error("cannot determine the system page size");
  }
  if (options.offset % static_cast<uint64_t>(page) != 0) {
    throw CliError("offset must satisfy mapping alignment");
  }
  return options;
}

class FileDescriptor {
 public:
  explicit FileDescriptor(const std::filesystem::path &path)
      : value_(::open(path.c_str(), O_RDWR | O_CLOEXEC)) {
    if (value_ < 0) {
      throw std::runtime_error("cannot open device: " + std::string(std::strerror(errno)));
    }
  }
  ~FileDescriptor() {
    if (value_ >= 0) {
      ::close(value_);
    }
  }
  FileDescriptor(const FileDescriptor &) = delete;
  FileDescriptor &operator=(const FileDescriptor &) = delete;
  int get() const { return value_; }

 private:
  int value_;
};

class Mapping {
 public:
  Mapping(int fd, uint64_t offset, uint64_t length) : length_(length) {
    if (length > std::numeric_limits<size_t>::max() ||
        offset > static_cast<uint64_t>(std::numeric_limits<off_t>::max())) {
      throw CliError("mapping range is not representable on this target");
    }
    value_ = ::mmap(nullptr,
                    static_cast<size_t>(length),
                    PROT_READ | PROT_WRITE,
                    MAP_SHARED,
                    fd,
                    static_cast<off_t>(offset));
    if (value_ == MAP_FAILED) {
      value_ = nullptr;
      throw std::runtime_error("mmap failed: " + std::string(std::strerror(errno)));
    }
    if (reinterpret_cast<uintptr_t>(value_) % alignof(SmokeHeader) != 0) {
      throw std::runtime_error("mapping does not satisfy smoke-header alignment");
    }
  }
  ~Mapping() {
    if (value_ != nullptr) {
      ::munmap(value_, static_cast<size_t>(length_));
    }
  }
  Mapping(const Mapping &) = delete;
  Mapping &operator=(const Mapping &) = delete;
  void *get() const { return value_; }

 private:
  void *value_ = nullptr;
  uint64_t length_;
};

uint64_t readSysfsUnsigned(const std::filesystem::path &path) {
  std::ifstream input(path);
  std::string text;
  if (!input || !(input >> text)) {
    throw std::runtime_error("cannot read devdax attribute: " + path.string());
  }
  size_t consumed = 0;
  uint64_t value = 0;
  try {
    value = std::stoull(text, &consumed, 0);
  } catch (const std::exception &) {
    throw std::runtime_error("invalid devdax attribute: " + path.string());
  }
  if (consumed != text.size() || value == 0) {
    throw std::runtime_error("invalid devdax attribute: " + path.string());
  }
  return value;
}

struct Geometry {
  std::string kind;
  uint64_t capacity;
  uint64_t alignment;
};

Geometry inspectGeometry(int fd, const Options &options) {
  struct stat status {};
  if (::fstat(fd, &status) != 0) {
    throw std::runtime_error("fstat failed: " + std::string(std::strerror(errno)));
  }
  const uint64_t page = static_cast<uint64_t>(::sysconf(_SC_PAGESIZE));
  Geometry geometry;
  if (S_ISREG(status.st_mode)) {
    if (status.st_size < 0) {
      throw std::runtime_error("regular backing file has a negative size");
    }
    geometry = {"regular", static_cast<uint64_t>(status.st_size), page};
  } else if (S_ISCHR(status.st_mode)) {
    const std::filesystem::path deviceLink =
        std::filesystem::path("/sys/dev/char") /
        (std::to_string(major(status.st_rdev)) + ":" + std::to_string(minor(status.st_rdev)));
    std::error_code error;
    const auto canonical = std::filesystem::canonical(deviceLink, error);
    if (error || canonical.filename().string().rfind("dax", 0) != 0) {
      throw std::runtime_error("character device is not a devdax device");
    }
    const uint64_t daxAlignment = readSysfsUnsigned(canonical / "align");
    geometry = {
        "devdax", readSysfsUnsigned(canonical / "size"), std::max(page, daxAlignment)};
  } else {
    throw std::runtime_error("backing path must be a regular file or devdax character device");
  }
  if (options.offset % geometry.alignment != 0 || options.length % geometry.alignment != 0) {
    throw CliError("offset and length must satisfy mapping alignment");
  }
  if (options.offset + options.length > geometry.capacity) {
    throw CliError("mapping range exceeds backing capacity");
  }
  return geometry;
}

uint8_t payloadByte(uint64_t index, uint64_t generation) {
  return static_cast<uint8_t>((index * 1315423911ULL + generation * 2654435761ULL) & 0xffU);
}

uint64_t checksum(const uint8_t *payload, uint64_t length) {
  uint64_t value = 1469598103934665603ULL;
  for (uint64_t index = 0; index < length; ++index) {
    value ^= payload[index];
    value *= 1099511628211ULL;
  }
  return value;
}

void emitRecord(const Options &options,
                const Geometry &geometry,
                uint64_t payloadLength,
                uint64_t payloadChecksum,
                bool atomic32LockFree,
                bool atomic64LockFree) {
  std::cout << "{\"status\":\"passed\",\"role\":\"" << options.role
            << "\",\"device_kind\":\"" << geometry.kind << "\",\"offset\":"
            << options.offset << ",\"bytes\":" << options.length
            << ",\"payload_bytes\":" << payloadLength << ",\"generation\":"
            << options.generation << ",\"checksum\":" << payloadChecksum
            << ",\"mapping_alignment\":" << geometry.alignment
            << ",\"device_size\":" << geometry.capacity
            << ",\"atomic_u32_lock_free\":" << (atomic32LockFree ? "true" : "false")
            << ",\"atomic_u64_lock_free\":" << (atomic64LockFree ? "true" : "false")
            << "}\n";
}

int run(const Options &options) {
  FileDescriptor fd(options.device);
  const Geometry geometry = inspectGeometry(fd.get(), options);
  Mapping mapping(fd.get(), options.offset, options.length);
  auto *header = static_cast<SmokeHeader *>(mapping.get());
  auto *payload = reinterpret_cast<uint8_t *>(header) + sizeof(SmokeHeader);
  const uint64_t payloadLength = options.payloadBytes == 0 ? options.length - sizeof(SmokeHeader)
                                                           : options.payloadBytes;
  std::atomic_ref<uint32_t> atomic32(header->atomic32Probe);
  std::atomic_ref<uint64_t> published(header->published);
  const bool atomic32LockFree = atomic32.is_lock_free();
  const bool atomic64LockFree = published.is_lock_free();
  if (!atomic32LockFree || !atomic64LockFree) {
    throw std::runtime_error("mapped 32-bit and 64-bit atomics must be lock-free");
  }

  if (options.role == "writer") {
    published.store(0, std::memory_order_release);
    for (uint64_t index = 0; index < payloadLength; ++index) {
      payload[index] = payloadByte(index, options.generation);
    }
    const uint64_t payloadChecksum = checksum(payload, payloadLength);
    header->magic = kMagic;
    header->generation = options.generation;
    header->length = options.length;
    header->checksum = payloadChecksum;
    header->reserved0 = 0;
    header->payloadLength = payloadLength;
    header->reserved = 0;
    atomic32.store(static_cast<uint32_t>(options.generation), std::memory_order_relaxed);
    published.store(options.generation, std::memory_order_release);
    emitRecord(options,
               geometry,
               payloadLength,
               payloadChecksum,
               atomic32LockFree,
               atomic64LockFree);
    return 0;
  }

  const auto deadline = std::chrono::steady_clock::now() +
                        std::chrono::milliseconds(options.timeoutMs);
  while (published.load(std::memory_order_acquire) != options.generation) {
    if (std::chrono::steady_clock::now() >= deadline) {
      throw std::runtime_error("timed out waiting for the requested generation");
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  }
  if (header->magic != kMagic || header->generation != options.generation ||
      header->length != options.length || header->payloadLength != payloadLength || header->reserved != 0 ||
      atomic32.load(std::memory_order_relaxed) != static_cast<uint32_t>(options.generation)) {
    throw std::runtime_error("published smoke header does not match the requested generation");
  }
  for (uint64_t index = 0; index < payloadLength; ++index) {
    if (payload[index] != payloadByte(index, options.generation)) {
      throw std::runtime_error("published payload bytes do not match");
    }
  }
  const uint64_t payloadChecksum = checksum(payload, payloadLength);
  if (payloadChecksum != header->checksum) {
    throw std::runtime_error("published payload checksum does not match");
  }
  emitRecord(options,
             geometry,
             payloadLength,
             payloadChecksum,
             atomic32LockFree,
             atomic64LockFree);
  return 0;
}

}  // namespace

int main(int argc, char **argv) {
  try {
    return run(parseOptions(argc, argv));
  } catch (const CliError &error) {
    std::cerr << "cxl_dax_smoke: " << error.what() << '\n';
    return 2;
  } catch (const std::exception &error) {
    std::cerr << "cxl_dax_smoke: " << error.what() << '\n';
    return 1;
  }
}
