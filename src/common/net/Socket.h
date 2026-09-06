#pragma once

#include <chrono>
#include <folly/Range.h>
#include <memory>
#include <optional>
#include <sys/uio.h>

#include "common/net/CompletionDisposition.h"
#include "common/net/Network.h"
#include "common/net/TransportKind.h"
#include "common/utils/Coroutine.h"
#include "common/utils/Result.h"

namespace hf3fs::net {

class BulkTransfer;

class Socket {
 public:
  class ExecutionScope {
   public:
    explicit ExecutionScope(const void *owner) noexcept
        : previous_(executionOwner_) {
      executionOwner_ = owner;
    }
    ~ExecutionScope() { executionOwner_ = previous_; }

    ExecutionScope(const ExecutionScope &) = delete;
    ExecutionScope &operator=(const ExecutionScope &) = delete;

   private:
    const void *previous_;
  };

  virtual ~Socket() = default;

  // describe this socket.
  virtual std::string describe() = 0;
  virtual folly::IPAddressV4 peerIP() = 0;

  virtual TransportKind kind() const noexcept = 0;
  virtual BulkTransfer *bulkTransfer() noexcept { return nullptr; }
  virtual std::optional<PublicationSnapshot> publicationSnapshot() const noexcept { return std::nullopt; }
  virtual Result<Void> bindExecutionOwner(const void *) { return Void{}; }

  static const void *currentExecutionOwner() noexcept { return executionOwner_; }

  // file descriptor monitored by epoll.
  virtual int fd() const = 0;

  using Events = uint32_t;
  constexpr static auto kEventReadableFlag = (1u << 0);
  constexpr static auto kEventWritableFlag = (1u << 1);
  // poll read and/or write events for this socket.
  virtual Result<Events> poll(uint32_t events) = 0;

  // receive a piece of buffer. return the received size.
  virtual Result<size_t> recv(folly::MutableByteRange buf) = 0;
  // send a batch of buffers. return the send size.
  virtual Result<size_t> send(struct iovec iov[], uint32_t len) = 0;
  // flush the write buffers.
  virtual Result<Void> flush() = 0;
  // check the liveness of the socket.
  virtual Result<Void> check() = 0;

 private:
  inline static thread_local const void *executionOwner_{};
};
using SocketPtr = std::shared_ptr<Socket>;

}  // namespace hf3fs::net
