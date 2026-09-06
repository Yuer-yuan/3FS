#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <string_view>

#include "common/net/RemoteExportLease.h"
#include "common/net/TransportKind.h"
#include "common/utils/Result.h"

namespace hf3fs::net {

enum class RemoteAccess : uint8_t {
  Read = 1U << 0U,
  Write = 1U << 1U,
  ReadWrite = (1U << 0U) | (1U << 1U),
};

constexpr uint8_t remoteAccessBits(RemoteAccess access) { return static_cast<uint8_t>(access); }

// This serialized value also lives in coroutine frames, which do not guarantee
// cache-line alignment. Only shared fabric records require alignas(64).
struct RemoteBufferHandle {
  uint16_t abiVersion{};
  uint8_t transportKind{};
  uint8_t permissions{};
  uint32_t ownerEndpoint{};
  uint32_t arenaId{};
  uint32_t allocationSlot{};
  uint64_t sessionGeneration{};
  uint64_t ownerGeneration{};
  uint64_t allocationGeneration{};
  uint64_t offset{};
  uint64_t length{};
  uint32_t checksum{};
  uint32_t reserved0{};

  using is_serde_copyable = void;
  bool operator==(const RemoteBufferHandle &) const = default;
  std::string serdeToReadable() const;
  static Result<RemoteBufferHandle> serdeFromReadable(std::string_view input);
};
static_assert(sizeof(RemoteBufferHandle) == 64);
static_assert(alignof(RemoteBufferHandle) == alignof(uint64_t));
static_assert(offsetof(RemoteBufferHandle, sessionGeneration) == 16);
static_assert(offsetof(RemoteBufferHandle, offset) == 40);
static_assert(offsetof(RemoteBufferHandle, checksum) == 56);

uint32_t remoteBufferHandleChecksum(const RemoteBufferHandle &handle);
void sealRemoteBufferHandle(RemoteBufferHandle &handle);
Result<Void> validateRemoteBufferHandle(const RemoteBufferHandle &handle);

class RemoteExport {
 public:
  RemoteExport(RemoteBufferHandle handle, RemoteExportLease lease)
      : handle_(handle),
        lease_(std::move(lease)) {}

  const RemoteBufferHandle &handle() const noexcept { return handle_; }
  RemoteExportLease takeLease() noexcept { return std::move(lease_); }

 private:
  RemoteBufferHandle handle_;
  RemoteExportLease lease_;
};

class SharedBuffer {
 public:
  using Exporter = std::function<Result<RemoteBufferHandle>(RemoteAccess, uint64_t, uint64_t)>;

  SharedBuffer() = default;

  uint8_t *data() noexcept;
  const uint8_t *data() const noexcept;
  size_t size() const noexcept { return length_; }
  explicit operator bool() const noexcept { return bool(storage_); }

  Result<SharedBuffer> subrange(size_t offset, size_t length) const;
  Result<RemoteExport> exportRemote(RemoteAccess access) const;

  static Result<SharedBuffer> allocateHeap(size_t length);
  static Result<SharedBuffer> allocateAligned(size_t length, size_t alignment);
  static Result<SharedBuffer> fromStorage(uint8_t *data,
                                          size_t length,
                                          uint64_t logicalOffset,
                                          std::shared_ptr<void> owner,
                                          Exporter exporter = {});

 private:
  struct Storage {
    uint8_t *data{};
    size_t length{};
    uint64_t logicalOffset{};
    std::shared_ptr<void> owner;
    Exporter exporter;
  };

  SharedBuffer(std::shared_ptr<Storage> storage, size_t offset, size_t length)
      : storage_(std::move(storage)),
        offset_(offset),
        length_(length) {}

  std::shared_ptr<Storage> storage_;
  size_t offset_{};
  size_t length_{};
};

}  // namespace hf3fs::net
