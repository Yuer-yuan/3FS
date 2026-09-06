#include "common/net/Buffer.h"

#include <charconv>
#include <cstdlib>
#include <cstring>
#include <fmt/format.h>
#include <limits>
#include <new>

#include "common/net/cxl/CxlAbi.h"

namespace hf3fs::net {
namespace {

bool parseDecimalField(std::string_view &input, std::string_view key, uint64_t &value, bool last = false) {
  if (!input.starts_with(key)) {
    return false;
  }
  input.remove_prefix(key.size());
  const auto delimiter = input.find(',');
  if ((last && delimiter != std::string_view::npos) || (!last && delimiter == std::string_view::npos)) {
    return false;
  }
  const auto token = last ? input : input.substr(0, delimiter);
  const auto [end, error] = std::from_chars(token.data(), token.data() + token.size(), value);
  if (error != std::errc{} || end != token.data() + token.size()) {
    return false;
  }
  input = last ? std::string_view{} : input.substr(delimiter + 1);
  return true;
}

}  // namespace

std::string RemoteBufferHandle::serdeToReadable() const {
  return fmt::format(
      "abi={},transport={},permissions={},owner={},arena={},slot={},sessionGeneration={},ownerGeneration={},"
      "allocationGeneration={},offset={},length={},checksum={},reserved={}",
      abiVersion,
      transportKind,
      permissions,
      ownerEndpoint,
      arenaId,
      allocationSlot,
      sessionGeneration,
      ownerGeneration,
      allocationGeneration,
      offset,
      length,
      checksum,
      reserved0);
}

Result<RemoteBufferHandle> RemoteBufferHandle::serdeFromReadable(std::string_view input) {
  uint64_t abiVersion = 0;
  uint64_t transportKind = 0;
  uint64_t permissions = 0;
  uint64_t ownerEndpoint = 0;
  uint64_t arenaId = 0;
  uint64_t allocationSlot = 0;
  uint64_t sessionGeneration = 0;
  uint64_t ownerGeneration = 0;
  uint64_t allocationGeneration = 0;
  uint64_t offset = 0;
  uint64_t length = 0;
  uint64_t checksum = 0;
  uint64_t reserved0 = 0;
  if (!parseDecimalField(input, "abi=", abiVersion) || !parseDecimalField(input, "transport=", transportKind) ||
      !parseDecimalField(input, "permissions=", permissions) || !parseDecimalField(input, "owner=", ownerEndpoint) ||
      !parseDecimalField(input, "arena=", arenaId) || !parseDecimalField(input, "slot=", allocationSlot) ||
      !parseDecimalField(input, "sessionGeneration=", sessionGeneration) ||
      !parseDecimalField(input, "ownerGeneration=", ownerGeneration) ||
      !parseDecimalField(input, "allocationGeneration=", allocationGeneration) ||
      !parseDecimalField(input, "offset=", offset) || !parseDecimalField(input, "length=", length) ||
      !parseDecimalField(input, "checksum=", checksum) || !parseDecimalField(input, "reserved=", reserved0, true) ||
      abiVersion > std::numeric_limits<uint16_t>::max() || transportKind > std::numeric_limits<uint8_t>::max() ||
      permissions > std::numeric_limits<uint8_t>::max() || ownerEndpoint > std::numeric_limits<uint32_t>::max() ||
      arenaId > std::numeric_limits<uint32_t>::max() || allocationSlot > std::numeric_limits<uint32_t>::max() ||
      checksum > std::numeric_limits<uint32_t>::max() || reserved0 > std::numeric_limits<uint32_t>::max()) {
    return makeError(StatusCode::kInvalidFormat, "invalid readable remote buffer handle");
  }
  return RemoteBufferHandle{static_cast<uint16_t>(abiVersion),
                            static_cast<uint8_t>(transportKind),
                            static_cast<uint8_t>(permissions),
                            static_cast<uint32_t>(ownerEndpoint),
                            static_cast<uint32_t>(arenaId),
                            static_cast<uint32_t>(allocationSlot),
                            sessionGeneration,
                            ownerGeneration,
                            allocationGeneration,
                            offset,
                            length,
                            static_cast<uint32_t>(checksum),
                            static_cast<uint32_t>(reserved0)};
}

uint32_t remoteBufferHandleChecksum(const RemoteBufferHandle &handle) {
  return cxl::cxlCrc32cWithZeroedU32(handle, offsetof(RemoteBufferHandle, checksum));
}

void sealRemoteBufferHandle(RemoteBufferHandle &handle) {
  handle.reserved0 = 0;
  handle.checksum = remoteBufferHandleChecksum(handle);
}

Result<Void> validateRemoteBufferHandle(const RemoteBufferHandle &handle) {
  if (handle.abiVersion != cxl::kCxlAbiVersion || handle.reserved0 != 0 || handle.length == 0 ||
      handle.offset > std::numeric_limits<uint64_t>::max() - handle.length ||
      handle.checksum != remoteBufferHandleChecksum(handle)) {
    return makeError(StatusCode::kInvalidFormat, "invalid remote buffer handle ABI, range or checksum");
  }
  if (handle.transportKind > static_cast<uint8_t>(TransportKind::CXL) || handle.permissions == 0 ||
      (handle.permissions & ~(remoteAccessBits(RemoteAccess::Read) | remoteAccessBits(RemoteAccess::Write))) != 0) {
    return makeError(StatusCode::kInvalidFormat, "invalid remote buffer transport or permissions");
  }
  return Void{};
}

uint8_t *SharedBuffer::data() noexcept { return storage_ ? storage_->data + offset_ : nullptr; }

const uint8_t *SharedBuffer::data() const noexcept { return storage_ ? storage_->data + offset_ : nullptr; }

Result<SharedBuffer> SharedBuffer::subrange(size_t offset, size_t length) const {
  if (!storage_ || offset > length_ || length > length_ - offset) {
    return makeError(StatusCode::kInvalidArg, "shared-buffer subrange is out of bounds");
  }
  return SharedBuffer(storage_, offset_ + offset, length);
}

Result<RemoteExport> SharedBuffer::exportRemote(RemoteAccess access) const {
  if (!storage_ || !storage_->exporter || length_ == 0) {
    return makeError(RPCCode::kTransportCapabilityMissing, "shared buffer is not remotely exportable");
  }
  auto handle = storage_->exporter(access, storage_->logicalOffset + offset_, length_);
  if (!handle) {
    return makeError(std::move(handle.error()));
  }
  return RemoteExport(*handle, RemoteExportLease(storage_));
}

Result<SharedBuffer> SharedBuffer::allocateHeap(size_t length) {
  if (length == 0) {
    return makeError(StatusCode::kInvalidArg, "cannot allocate an empty shared buffer");
  }
  auto bytes = std::shared_ptr<uint8_t[]>(new (std::nothrow) uint8_t[length]);
  if (!bytes) {
    return makeError(StatusCode::kNotEnoughMemory, "heap shared-buffer allocation failed");
  }
  std::shared_ptr<void> owner(bytes, bytes.get());
  return fromStorage(bytes.get(), length, 0, std::move(owner));
}

Result<SharedBuffer> SharedBuffer::allocateAligned(size_t length, size_t alignment) {
  if (length == 0 || alignment < sizeof(void *) || (alignment & (alignment - 1U)) != 0) {
    return makeError(StatusCode::kInvalidArg, "invalid aligned shared-buffer size or alignment");
  }
  void *allocation = nullptr;
  if (::posix_memalign(&allocation, alignment, length) != 0) {
    return makeError(StatusCode::kNotEnoughMemory, "aligned shared-buffer allocation failed");
  }
  std::shared_ptr<void> owner(allocation, std::free);
  return fromStorage(static_cast<uint8_t *>(allocation), length, 0, std::move(owner));
}

Result<SharedBuffer> SharedBuffer::fromStorage(uint8_t *data,
                                               size_t length,
                                               uint64_t logicalOffset,
                                               std::shared_ptr<void> owner,
                                               Exporter exporter) {
  if (data == nullptr || length == 0 || !owner || logicalOffset > std::numeric_limits<uint64_t>::max() - length) {
    return makeError(StatusCode::kInvalidArg, "invalid shared-buffer storage");
  }
  auto storage = std::make_shared<Storage>(Storage{data, length, logicalOffset, std::move(owner), std::move(exporter)});
  return SharedBuffer(std::move(storage), 0, length);
}

}  // namespace hf3fs::net
