#include "common/net/cxl/CxlSocket.h"

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <fmt/format.h>
#include <limits>
#include <sys/epoll.h>
#include <sys/eventfd.h>
#include <unistd.h>

namespace hf3fs::net::cxl {
namespace {

constexpr uint64_t kCursorPageBytes = 4096;

CxlLaneConfig defaultLaneConfig(const CxlLayout &layout) {
  constexpr uint32_t depth = 64;
  constexpr uint32_t cellBytes = 64U * 1024U;
  const auto &cursorRange = layout.ranges()[static_cast<size_t>(CxlRangeKind::CursorPages) - 1U];
  const auto &frameRange = layout.ranges()[static_cast<size_t>(CxlRangeKind::FrameRings) - 1U];
  const auto &payloadRange = layout.ranges()[static_cast<size_t>(CxlRangeKind::RpcPayloadCells) - 1U];
  const uint64_t ringBytes = depth * sizeof(CxlFrameEntry);
  const uint64_t payloadBytes = static_cast<uint64_t>(depth) * cellBytes;
  return CxlLaneConfig{
      .sessionGeneration = layout.sessionGeneration(),
      .laneGeneration = 1,
      .depth = depth,
      .cellBytes = cellBytes,
      .directions =
          {
              CxlLaneDirectionLayout{
                  .producerCursorOffset = cursorRange.offset,
                  .consumerCursorOffset = cursorRange.offset + kCursorPageBytes,
                  .deliveredRecordOffset = cursorRange.offset + kCursorPageBytes + kCxlCacheLineBytes,
                  .frameRingOffset = frameRange.offset,
                  .payloadCellsOffset = payloadRange.offset,
              },
              CxlLaneDirectionLayout{
                  .producerCursorOffset = cursorRange.offset + 2U * kCursorPageBytes,
                  .consumerCursorOffset = cursorRange.offset + 3U * kCursorPageBytes,
                  .deliveredRecordOffset = cursorRange.offset + 3U * kCursorPageBytes + kCxlCacheLineBytes,
                  .frameRingOffset = frameRange.offset + ringBytes,
                  .payloadCellsOffset = payloadRange.offset + payloadBytes,
              },
          },
  };
}

}  // namespace

Result<std::shared_ptr<CxlSocket>> CxlSocket::create(CxlLane lane,
                                                     Address peer,
                                                     std::shared_ptr<CxlProgressEngine> progressEngine,
                                                     std::shared_ptr<BulkTransfer> bulkTransfer) {
  auto unique = createUnique(std::move(lane), peer, std::move(progressEngine), std::move(bulkTransfer));
  if (!unique) {
    return makeError(std::move(unique.error()));
  }
  return std::shared_ptr<CxlSocket>(std::move(*unique));
}

Result<std::unique_ptr<CxlSocket>> CxlSocket::createUnique(CxlLane lane,
                                                           Address peer,
                                                           std::shared_ptr<CxlProgressEngine> progressEngine,
                                                           std::shared_ptr<BulkTransfer> bulkTransfer) {
  if (!peer.isCXL() || !progressEngine) {
    return makeError(StatusCode::kInvalidArg, "CXL socket requires a CXL peer and progress engine");
  }
  const int eventFd = ::eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
  if (eventFd < 0) {
    return makeError(RPCCode::kEpollInitError, fmt::format("CXL eventfd failed: {}", std::strerror(errno)));
  }

  auto socket = std::unique_ptr<CxlSocket>(new CxlSocket(std::move(lane),
                                                         peer,
                                                         eventFd,
                                                         progressEngine,
                                                         progressEngine->metrics(),
                                                         std::move(bulkTransfer)));
  auto registered = progressEngine->registerSocket(*socket);
  if (!registered) {
    ::close(eventFd);
    socket->eventFd_.store(-1, std::memory_order_release);
    return makeError(std::move(registered.error()));
  }
  return socket;
}

Result<CxlSocket::ConnectedPair> CxlSocket::createConnectedPairForTest(
    CxlRegion &region,
    const CxlLayout &layout,
    const CxlLaneConfig &config,
    std::shared_ptr<CxlProgressEngine> progressEngine,
    Address requesterPeer,
    Address acceptorPeer) {
  auto requesterLane = CxlLane::create(region, layout, config, CxlLaneRole::Requester);
  if (!requesterLane) {
    return makeError(std::move(requesterLane.error()));
  }
  auto acceptorLane = CxlLane::create(region, layout, config, CxlLaneRole::Acceptor);
  if (!acceptorLane) {
    return makeError(std::move(acceptorLane.error()));
  }
  auto requester = create(std::move(*requesterLane), requesterPeer, progressEngine);
  if (!requester) {
    return makeError(std::move(requester.error()));
  }
  auto acceptor = create(std::move(*acceptorLane), acceptorPeer, std::move(progressEngine));
  if (!acceptor) {
    (*requester)->close();
    return makeError(std::move(acceptor.error()));
  }
  return ConnectedPair{std::move(*requester), std::move(*acceptor)};
}

Result<CxlSocket::ConnectedPair> CxlSocket::createConnectedPairForTest(
    CxlRegion &region,
    const CxlLayout &layout,
    std::shared_ptr<CxlProgressEngine> progressEngine,
    Address requesterPeer,
    Address acceptorPeer) {
  return createConnectedPairForTest(region,
                                    layout,
                                    defaultLaneConfig(layout),
                                    std::move(progressEngine),
                                    requesterPeer,
                                    acceptorPeer);
}

CxlSocket::CxlSocket(CxlLane lane,
                     Address peer,
                     int eventFd,
                     std::shared_ptr<CxlProgressEngine> progressEngine,
                     std::shared_ptr<CxlMetrics> metrics,
                     std::shared_ptr<BulkTransfer> bulkTransfer)
    : lane_(std::move(lane)),
      peer_(peer),
      outbound_(lane_.role() == CxlLaneRole::Requester ? Direction::Submission : Direction::Completion),
      inbound_(lane_.role() == CxlLaneRole::Requester ? Direction::Completion : Direction::Submission),
      eventFd_(eventFd),
      progressEngine_(std::move(progressEngine)),
      metrics_(std::move(metrics)),
      bulkTransfer_(std::move(bulkTransfer)),
      staging_(lane_.config().cellBytes) {}

CxlSocket::~CxlSocket() { close(); }

std::string CxlSocket::describe() {
  return fmt::format("CXL(peer={}, session={}, lane={})",
                     peer_,
                     lane_.config().sessionGeneration,
                     lane_.config().laneGeneration);
}

folly::IPAddressV4 CxlSocket::peerIP() { return peer_.toFollyIP(); }

std::optional<PublicationSnapshot> CxlSocket::publicationSnapshot() const noexcept {
  const uint64_t published = publishedOffset_.load(std::memory_order_acquire);
  const uint64_t accepted = acceptedOffset_.load(std::memory_order_acquire);
  auto deliveredResult = lane_.observePeerDeliveredOffset(outbound_, published);
  bool trustworthy = deliveredResult.hasValue() && published <= accepted;
  uint64_t delivered = trustworthy ? *deliveredResult : 0;
  if (trustworthy) {
    uint64_t previous = lastDeliveredOffset_.load(std::memory_order_acquire);
    while (delivered > previous &&
           !lastDeliveredOffset_.compare_exchange_weak(previous, delivered, std::memory_order_acq_rel)) {
    }
    trustworthy = delivered >= previous;
  }
  if (!trustworthy) {
    metrics_->addCorruptPublication();
  }
  return PublicationSnapshot{
      .laneGeneration = lane_.config().laneGeneration,
      .acceptedOffset = accepted,
      .publishedOffset = published,
      .peerDeliveredOffset = delivered,
      .trustworthy = trustworthy,
  };
}

Result<Void> CxlSocket::bindExecutionOwner(const void *owner) {
  if (owner == nullptr) {
    return makeError(StatusCode::kInvalidArg, "CXL execution owner cannot be null");
  }
  const void *expected = nullptr;
  if (!executionOwner_.compare_exchange_strong(expected, owner, std::memory_order_acq_rel) && expected != owner) {
    return makeError(StatusCode::kInvalidArg, "CXL socket is already bound to another I/O worker");
  }
  return Void{};
}

Result<CxlSocket::Events> CxlSocket::poll(uint32_t events) {
  if (closed()) {
    return makeError(RPCCode::kSocketClosed);
  }
  if ((events & (EPOLLERR | EPOLLHUP)) != 0) {
    return makeError(RPCCode::kSocketError);
  }

  const int localFd = fd();
  uint64_t value;
  while (true) {
    const auto readBytes = ::read(localFd, &value, sizeof(value));
    if (readBytes == sizeof(value)) {
      continue;
    }
    if (readBytes < 0 && errno == EINTR) {
      continue;
    }
    if (readBytes < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
      break;
    }
    return makeError(RPCCode::kSocketError,
                     fmt::format("CXL eventfd read failed: {}", readBytes < 0 ? std::strerror(errno) : "short read"));
  }

  constexpr uint32_t allEvents = kEventReadableFlag | kEventWritableFlag;
  // One-shot arm followed by a shared-state recheck. If readiness appears
  // between these operations, either this call observes it or the progress
  // engine consumes the armed bit and signals eventfd.
  armedMask_.store(allEvents, std::memory_order_release);
  const auto ready = computeReady();
  const auto peerError = peerError_.load(std::memory_order_acquire);
  if (peerError != StatusCode::kOK) {
    // Drain bytes published before an orderly close before reporting EOF.
    auto readable = lane_.observeReadable(inbound_);
    if (peerError != RPCCode::kSocketClosed || !readable || !*readable) {
      return makeError(peerError, "CXL peer left the lane");
    }
  }
  if (faulted_.load(std::memory_order_acquire)) {
    return makeError(RPCCode::kBulkTransferError, "CXL lane readiness is corrupt");
  }
  armedMask_.fetch_and(~ready, std::memory_order_acq_rel);
  return ready;
}

Result<size_t> CxlSocket::recv(folly::MutableByteRange buffer) {
  RETURN_ON_ERROR(bindConsumer());
  if (closed()) {
    return makeError(RPCCode::kSocketClosed);
  }
  auto destination = std::span(reinterpret_cast<std::byte *>(buffer.data()), buffer.size());
  auto result = lane_.tryPop(inbound_, destination);
  if (!result) {
    if (result.error().code() == StatusCode::kQueueEmpty) {
      RETURN_ON_ERROR(checkPeer());
      rearmAndRecheck(kEventReadableFlag);
      return 0;
    }
    markFault();
    return makeError(std::move(result.error()));
  }
  metrics_->addDeliveredBytes(*result);
  return result;
}

Result<size_t> CxlSocket::send(struct iovec iov[], uint32_t length) {
  RETURN_ON_ERROR(bindProducer());
  if (closed()) {
    return makeError(RPCCode::kSocketClosed);
  }
  RETURN_ON_ERROR(checkPeer());
  if (length != 0 && iov == nullptr) {
    return makeError(StatusCode::kInvalidArg, "null CXL iovec array");
  }
  for (uint32_t index = 0; index < length; ++index) {
    if (iov[index].iov_len != 0 && iov[index].iov_base == nullptr) {
      return makeError(StatusCode::kInvalidArg, "null CXL iovec payload");
    }
  }

  size_t acceptedThisCall = 0;
  for (uint32_t index = 0; index < length; ++index) {
    auto *source = static_cast<const std::byte *>(iov[index].iov_base);
    size_t remaining = iov[index].iov_len;
    while (remaining != 0) {
      if (stagedBytes_ == 0) {
        auto writable = lane_.writable(outbound_);
        if (!writable) {
          markFault();
          return makeError(std::move(writable.error()));
        }
        if (!*writable) {
          rearmAndRecheck(kEventWritableFlag);
          return acceptedThisCall;
        }
        reservedSlot_ = true;
      }

      const size_t copied = std::min(remaining, staging_.size() - stagedBytes_);
      const uint64_t accepted = acceptedOffset_.load(std::memory_order_relaxed);
      if (copied > std::numeric_limits<uint64_t>::max() - accepted ||
          copied > std::numeric_limits<size_t>::max() - acceptedThisCall) {
        markFault();
        return makeError(RPCCode::kStaleGeneration, "CXL accepted byte offset would overflow");
      }
      std::memcpy(staging_.data() + stagedBytes_, source, copied);
      stagedBytes_ += copied;
      source += copied;
      remaining -= copied;
      acceptedThisCall += copied;
      acceptedOffset_.store(accepted + copied, std::memory_order_release);
      metrics_->addAcceptedBytes(copied);
      if (stagedBytes_ == staging_.size()) {
        auto published = publishStaging();
        if (!published) {
          return makeError(std::move(published.error()));
        }
      }
    }
  }
  return acceptedThisCall;
}

Result<Void> CxlSocket::flush() {
  RETURN_ON_ERROR(bindProducer());
  if (closed()) {
    return makeError(RPCCode::kSocketClosed);
  }
  RETURN_ON_ERROR(checkPeer());
  return publishStaging();
}

Result<Void> CxlSocket::check() {
  if (closed()) {
    return makeError(RPCCode::kSocketClosed);
  }
  if (auto retired = lane_.retirementStatus()) {
    return makeError(*retired);
  }
  if (faulted_.load(std::memory_order_acquire)) {
    return makeError(RPCCode::kBulkTransferError, "CXL socket is faulted");
  }
  return checkPeer();
}

void CxlSocket::setPeerCheck(PeerCheck check) {
  std::lock_guard lock(peerCheckMutex_);
  peerCheck_ = std::move(check);
}

Result<Void> CxlSocket::checkPeer() const {
  auto code = peerError_.load(std::memory_order_acquire);
  if (code != StatusCode::kOK) {
    return makeError(code, "CXL peer left the lane");
  }
  std::lock_guard lock(peerCheckMutex_);
  if (peerCheck_) {
    auto result = peerCheck_();
    if (!result) {
      peerError_.store(result.error().code(), std::memory_order_release);
      return result;
    }
  }
  return Void{};
}

void CxlSocket::close() noexcept {
  bool expected = false;
  if (!closed_.compare_exchange_strong(expected, true, std::memory_order_acq_rel)) {
    return;
  }
  lane_.retire(Status(RPCCode::kSocketClosed));
  metrics_->addRetiredLane();
  if (progressEngine_) {
    (void)progressEngine_->unregisterSocket(*this);
  }
  const int localFd = eventFd_.exchange(-1, std::memory_order_acq_rel);
  if (localFd >= 0) {
    ::close(localFd);
  }
  if (closeHook_) {
    try {
      closeHook_(faulted_.load(std::memory_order_acquire));
    } catch (...) {
      // close() is noexcept. The hook only retires shared lane metadata;
      // failure to do so leaves the slot quarantined rather than reusable.
    }
    closeHook_ = {};
  }
}

Result<Void> CxlSocket::bindProducer() {
  if (const auto *owner = executionOwner_.load(std::memory_order_acquire)) {
    if (Socket::currentExecutionOwner() != owner) {
      return makeError(StatusCode::kInvalidArg, "CXL producer called from a different I/O worker");
    }
    return Void{};
  }
  std::lock_guard lock(ownerMutex_);
  const auto current = std::this_thread::get_id();
  if (!producerOwner_) {
    producerOwner_ = current;
  } else if (*producerOwner_ != current) {
    return makeError(StatusCode::kInvalidArg, "CXL producer called from a different I/O worker");
  }
  return Void{};
}

Result<Void> CxlSocket::bindConsumer() {
  if (const auto *owner = executionOwner_.load(std::memory_order_acquire)) {
    if (Socket::currentExecutionOwner() != owner) {
      return makeError(StatusCode::kInvalidArg, "CXL consumer called from a different I/O worker");
    }
    return Void{};
  }
  std::lock_guard lock(ownerMutex_);
  const auto current = std::this_thread::get_id();
  if (!consumerOwner_) {
    consumerOwner_ = current;
  } else if (*consumerOwner_ != current) {
    return makeError(StatusCode::kInvalidArg, "CXL consumer called from a different I/O worker");
  }
  return Void{};
}

Result<Void> CxlSocket::publishStaging() {
  if (stagedBytes_ == 0) {
    reservedSlot_ = false;
    return Void{};
  }
  if (!reservedSlot_) {
    markFault();
    return makeError(StatusCode::kDataCorruption, "CXL staging bytes exist without reserved ring credit");
  }
  auto result = lane_.tryPush(outbound_, FrameView{std::span(staging_.data(), stagedBytes_)});
  if (!result || *result != stagedBytes_) {
    markFault();
    return !result ? makeError(std::move(result.error()))
                   : makeError(StatusCode::kDataCorruption, "CXL lane accepted a partial staged cell");
  }
  const uint64_t published = publishedOffset_.load(std::memory_order_relaxed);
  if (stagedBytes_ > std::numeric_limits<uint64_t>::max() - published) {
    markFault();
    return makeError(RPCCode::kStaleGeneration, "CXL published byte offset would overflow");
  }
  publishedOffset_.store(published + stagedBytes_, std::memory_order_release);
  metrics_->addPublishedBytes(stagedBytes_);
  stagedBytes_ = 0;
  reservedSlot_ = false;
  return Void{};
}

CxlSocket::Events CxlSocket::computeReady() const noexcept {
  if (!checkPeer()) {
    // Wake the owning I/O worker to drain/close. Never reclaim its lane on
    // the progress thread while the worker may still be accessing cursors.
    return kEventReadableFlag | kEventWritableFlag;
  }
  Events ready{};
  auto readable = lane_.observeReadable(inbound_);
  auto writable = lane_.observeWritable(outbound_);
  if (!readable || !writable) {
    markFault();
    return kEventReadableFlag | kEventWritableFlag;
  }
  if (*readable) {
    ready |= kEventReadableFlag;
  }
  if (*writable) {
    ready |= kEventWritableFlag;
  }
  return ready;
}

void CxlSocket::rearmAndRecheck(Events interest) noexcept {
  armedMask_.fetch_or(interest, std::memory_order_release);
  const Events ready = computeReady() & interest;
  if (ready == 0) {
    return;
  }
  const Events armed = armedMask_.fetch_and(~ready, std::memory_order_acq_rel);
  if ((ready & armed) != 0) {
    signal();
  }
}

void CxlSocket::progress() noexcept {
  if (closed()) {
    return;
  }
  const Events ready = computeReady();
  lastReadyMask_.store(ready, std::memory_order_release);
  const Events armed = armedMask_.fetch_and(~ready, std::memory_order_acq_rel);
  if ((ready & armed) != 0) {
    signal();
  }
}

void CxlSocket::signal() noexcept {
  const int localFd = fd();
  if (localFd < 0) {
    return;
  }
  constexpr uint64_t one = 1;
  while (true) {
    const auto result = ::write(localFd, &one, sizeof(one));
    if (result == sizeof(one)) {
      metrics_->addEventfdWakeup();
      return;
    }
    if (result < 0 && errno == EINTR) {
      continue;
    }
    // EAGAIN means an earlier notification is already pending, so no wakeup
    // is lost and the progress thread must not block.
    return;
  }
}

void CxlSocket::markFault() const noexcept {
  bool expected = false;
  if (faulted_.compare_exchange_strong(expected, true, std::memory_order_acq_rel)) {
    metrics_->addCorruptPublication();
  }
}

}  // namespace hf3fs::net::cxl
