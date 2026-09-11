#include "common/utils/AtomicSharedPtr.h"
#include "common/net/Transport.h"

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <folly/experimental/coro/BlockingWait.h>
#include <folly/experimental/coro/Sleep.h>
#include <folly/logging/xlog.h>
#include <iostream>
#include <memory>
#include <random>
#include <sys/random.h>
#include <thread>

#include "common/monitor/Recorder.h"
#include "common/net/IOWorker.h"
#include "common/net/MessageHeader.h"
#include "common/net/Socket.h"
#include "common/net/TransportEvidence.h"
#include "common/net/TransportRuntime.h"
#include "common/net/Waiter.h"
#include "common/net/RpcTrace.h"
#include "common/net/WriteItem.h"
#include "common/net/cxl/CxlConnectService.h"
#include "common/net/cxl/CxlSocket.h"
#if HF3FS_ENABLE_RDMA
#include "common/net/ib/IBDevice.h"
#include "common/net/ib/IBSocket.h"
#endif
#include "common/net/tcp/TcpSocket.h"
#include "common/serde/ClientContext.h"
#include "common/utils/Address.h"
#include "common/utils/Coroutine.h"
#include "common/utils/Result.h"
#include "common/utils/Size.h"

namespace hf3fs::net {
namespace {

constexpr uint32_t kInvalidatedFlag = 1u << 0u;     // Bit 0
constexpr uint32_t kReadAvailableFlag = 1u << 1u;   // Bit 1
constexpr uint32_t kReadNewWakedFlag = 1u << 2u;    // Bit 2
constexpr uint32_t kWriteAvailableFlag = 1u << 3u;  // Bit 3
constexpr uint32_t kWriteNewWakedFlag = 1u << 4u;   // Bit 4
constexpr uint32_t kWriteHasMsgFlag = 1u << 5u;     // Bit 5
constexpr uint32_t kWriteNewMsgFlag = 1u << 6u;     // Bit 6
constexpr uint32_t kLastReadFinished = 1u << 7u;    // Bit 7
constexpr uint32_t kLastWriteFinished = 1u << 8u;   // Bit 8

monitor::CountRecorder readCPUTime{"common_net_read_cpu_time"};
monitor::CountRecorder doWriteCPUTime{"common_net_do_write_cpu_time"};
monitor::CountRecorder writeAllCPUTime{"common_net_write_all_cpu_time"};
monitor::CountRecorder readBytes{"common_net_read_bytes"};
monitor::CountRecorder writeBytes{"common_net_write_bytes"};
monitor::DistributionRecorder batchReadSize{"common_net_batch_read_size"};
monitor::DistributionRecorder batchWriteSize{"common_net_batch_write_size"};

Result<cxl::CxlConnectionNonce> randomConnectionNonce() {
  for (unsigned attempt = 0; attempt < 4; ++attempt) {
    cxl::CxlConnectionNonce nonce{};
    auto *destination = reinterpret_cast<std::byte *>(&nonce);
    size_t remaining = sizeof(nonce);
    while (remaining != 0) {
      const auto result = ::getrandom(destination, remaining, 0);
      if (result > 0) {
        destination += result;
        remaining -= static_cast<size_t>(result);
      } else if (result < 0 && errno == EINTR) {
        continue;
      } else {
        return makeError(RPCCode::kConnectFailed,
                         fmt::format("getrandom for CXL connection nonce failed: {}", std::strerror(errno)));
      }
    }
    if (nonce.low != 0 || nonce.high != 0) {
      return nonce;
    }
  }
  return makeError(RPCCode::kConnectFailed, "getrandom returned an all-zero CXL connection nonce");
}

}  // namespace

Transport::Transport(std::unique_ptr<Socket> socket, IOWorker &io_worker, Address serverAddr, ServicePlane servicePlane)
    : socket_(std::move(socket)),
      kind_(socket_ ? socket_->kind() : transportKind(serverAddr.type)),
      ioWorker_(io_worker),
      connExecutor_(io_worker.connExecutorWeak()),
      serverAddr_(serverAddr),
      servicePlane_(servicePlane) {
  if (socket_) {
    RUNTIME_ASSERT_RESULT(socket_->bindExecutionOwner(&ioWorker_), "failed to bind socket to its I/O worker");
    initializePublicationLedger();
  }
}

Transport::~Transport() {
  if (auto eventLoop = eventLoop_.lock()) {
    eventLoop->remove(this);
  }
  if (!socket_) {
    return;
  }
  switch (kind_) {
    case TransportKind::RDMA:
#if HF3FS_ENABLE_RDMA
      IBManager::close(IBSocket::Ptr(dynamic_cast<IBSocket *>(socket_.release())));
#else
      socket_.reset();
#endif
      break;
    case TransportKind::TCP:
      dynamic_cast<TcpSocket *>(socket_.get())->close();
      break;
    case TransportKind::CXL:
      dynamic_cast<cxl::CxlSocket *>(socket_.get())->close();
      break;
  }
}

TransportPtr Transport::create(std::unique_ptr<Socket> socket, IOWorker &io_worker, Address::Type addrType) {
  auto plane = legacyServicePlane(addrType);
  if (!plane) {
    return nullptr;
  }
  return create(std::move(socket), io_worker, addrType, *plane);
}

TransportPtr Transport::create(std::unique_ptr<Socket> socket,
                               IOWorker &io_worker,
                               Address::Type addrType,
                               ServicePlane servicePlane) {
  if (!socket || socket->kind() != transportKind(addrType)) {
    return nullptr;
  }
  auto transport = enable_shared_from_this::create(std::move(socket), io_worker, Address{0, 0, addrType}, servicePlane);
  if (transport->kind() == TransportKind::CXL && !transport->publicationLedger_) {
    return nullptr;
  }
  return transport;
}

std::shared_ptr<Transport> Transport::create(Address addr, IOWorker &io_worker) {
  auto plane = legacyServicePlane(addr.type);
  if (!plane) {
    return nullptr;
  }
  return create(ServiceEndpoint{addr, *plane}, io_worker);
}

std::shared_ptr<Transport> Transport::create(ServiceEndpoint endpoint, IOWorker &io_worker) {
  switch (transportKind(endpoint.address.type)) {
    case TransportKind::TCP:
      return enable_shared_from_this::create(std::make_unique<TcpSocket>(),
                                             io_worker,
                                             endpoint.address,
                                             endpoint.plane);
    case TransportKind::RDMA:
      TransportEvidence::process().add(TransportEvidence::RdmaOpenAttempts);
#if HF3FS_ENABLE_RDMA
      return enable_shared_from_this::create(std::make_unique<IBSocket>(io_worker.config_.ibsocket()),
                                             io_worker,
                                             endpoint.address,
                                             endpoint.plane);
#else
      return nullptr;
#endif
    case TransportKind::CXL:
      return enable_shared_from_this::create(nullptr, io_worker, endpoint.address, endpoint.plane);
  }
  return nullptr;
}

CoTryTask<void> Transport::connect(ServiceEndpoint endpoint, Duration timeout) {
  if (endpoint != serverEndpoint()) {
    co_return makeError(StatusCode::kInvalidArg, "transport connect endpoint does not match its pool key");
  }
  const auto addr = endpoint.address;
  if (kind() == TransportKind::RDMA) {
#if HF3FS_ENABLE_RDMA
    auto tcpAddr = Address(addr.ip, addr.port, Address::TCP);
    static const hf3fs::AtomicSharedPtr<const CoreRequestOptions> connectOptions{
        std::make_shared<CoreRequestOptions>()};
    auto tcpCtx = serde::ClientContext(ioWorker_, tcpAddr, connectOptions);
    auto ibSocket = dynamic_cast<IBSocket *>(socket_.get());
    co_return co_await ibSocket->connect(tcpCtx, timeout);
#else
    co_return makeError(RPCCode::kDataPlaneNotInitialized, "RDMA support is disabled in this build");
#endif
  } else if (kind() == TransportKind::TCP) {
    auto tcpSocket = dynamic_cast<TcpSocket *>(socket_.get());
    co_return co_await tcpSocket->connect(addr, timeout);
  }

  auto context = TransportRuntime::cxlConnection(endpoint);
  CO_RETURN_ON_ERROR(context);
  auto nonce = randomConnectionNonce();
  CO_RETURN_ON_ERROR(nonce);

  cxl::CxlConnectReq req;
  req.session_generation = context->fabric->layout().sessionGeneration();
  req.requester_endpoint = context->fabric->config().endpoint.value;
  req.requester_endpoint_generation = context->fabric->config().endpointGeneration;
  req.requester_address = static_cast<uint64_t>(context->localAddress);
  req.target_endpoint = context->target.value;
  req.service_plane = static_cast<uint8_t>(endpoint.plane);
  req.queue_depth = ioWorker_.config_.cxlsocket().queue_depth();
  req.cell_bytes = ioWorker_.config_.cxlsocket().cell_bytes();
  req.capability_bits = context->fabric->config().capabilityBits;
  req.connection_nonce = *nonce;

  static const hf3fs::AtomicSharedPtr<const CoreRequestOptions> connectOptions{
      std::make_shared<CoreRequestOptions>()};
  auto tcpCtx = serde::ClientContext(ioWorker_, addr.tcp(), connectOptions);
  UserRequestOptions opts;
  opts.timeout = timeout;
  auto rsp = co_await cxl::CxlConnect<>::connect(tcpCtx, req, &opts);
  CO_RETURN_ON_ERROR(rsp);
  if (rsp->service_plane != static_cast<uint8_t>(endpoint.plane)) {
    co_return makeError(RPCCode::kDataPlaneHandshakeFailed, "CXL bootstrap returned the wrong service plane");
  }
  auto cxlSocket = cxl::CxlConnectService::finishRequesterUnique(context->fabric, req, *rsp, addr);
  CO_RETURN_ON_ERROR(cxlSocket);
  auto activation = cxl::CxlConnectService::activationRequest(*rsp);
  auto activated = co_await cxl::CxlConnect<>::activate(tcpCtx, activation, &opts);
  if (!activated || !activated->activated) {
    (void)cxl::CxlConnectService::faultRequester(context->fabric, *rsp);
    (*cxlSocket)->close();
    if (!activated) {
      co_return makeError(std::move(activated.error()));
    }
    co_return makeError(RPCCode::kDataPlaneHandshakeFailed, "CXL bootstrap activation was not acknowledged");
  }
  CO_RETURN_ON_ERROR((*cxlSocket)->bindExecutionOwner(&ioWorker_));
  socket_ = std::move(*cxlSocket);
  initializePublicationLedger();
  if (!publicationLedger_) {
    co_return makeError(StatusCode::kDataCorruption, "CXL socket has no trustworthy initial publication snapshot");
  }
  co_return Void{};
}

folly::IPAddressV4 Transport::peerIP() const { return socket_ ? socket_->peerIP() : serverAddr_.toFollyIP(); }

std::string Transport::describe() const {
  return socket_ ? socket_->describe() : fmt::format("{}(connecting)", serverEndpoint().address);
}

Result<Void> Transport::check() {
  return socket_ ? socket_->check() : makeError(RPCCode::kConnectFailed, "transport is not connected");
}

void Transport::completePublication(size_t uuid) {
  if (publicationLedger_) {
    auto completed = publicationLedger_->complete(uuid);
    if (RpcTrace::enabled()) rpcTrace(0, 0, uuid, "publication_complete",
                                     completed ? 0 : completed.error().code(), describe());
  }
}

bool Transport::rdmaConnectFinished() const {
#if HF3FS_ENABLE_RDMA
  if (!socket_) {
    return false;
  }
  auto *socket = dynamic_cast<const IBSocket *>(socket_.get());
  return socket != nullptr && socket->checkConnectFinished();
#else
  return false;
#endif
}

std::optional<WriteList> Transport::send(WriteList list) {
  list.setTransport(shared_from_this());
  if (list.empty()) {
    return std::nullopt;
  }
  mpscWriteList_.add(std::move(list));

  // This is the first write item. Try to start a write task.
  auto flags = flags_.fetch_or(kWriteHasMsgFlag | kWriteNewMsgFlag);
  if ((flags & (kInvalidatedFlag | kWriteHasMsgFlag)) == 0 && (flags & kWriteAvailableFlag) != 0) {
    ioWorker_.startWriteTask(this, false);
  } else if (UNLIKELY(flags & kInvalidatedFlag)) {
    return mpscWriteList_.takeOut().extractForRetry();
  }
  return std::nullopt;
}

void Transport::invalidate(bool logError /* = true */) {
  // mask a invalidated flag and try to start a write task with error mark.
  auto flags = flags_.fetch_or(kInvalidatedFlag | kReadAvailableFlag | kWriteAvailableFlag | kWriteHasMsgFlag);
  if ((flags & kInvalidatedFlag) == 0) {
    ioWorker_.remove(shared_from_this());
  }
  // if no read task is executing, start one.
  if ((flags & kReadAvailableFlag) == 0) {
    ioWorker_.startReadTask(this, true, logError);
  }
  // if no write task is executing, start one.
  if ((flags & kWriteAvailableFlag) == 0 || (flags & kWriteHasMsgFlag) == 0) {
    ioWorker_.startWriteTask(this, true, logError);
  }
}

bool Transport::invalidated() const { return flags_ & kInvalidatedFlag; }

CoTask<void> Transport::closeIB() {
#if HF3FS_ENABLE_RDMA
  if (kind() == TransportKind::RDMA && socket_) {
    invalidate();
    XLOGF(DBG, "Wait ib socket {} last read/write finished begin", fmt::ptr(socket_.get()));
    co_await lastReadAndWriteFinished_;
    XLOGF(DBG, "Wait ib socket {} last read/write finished end", fmt::ptr(socket_.get()));
    co_await dynamic_cast<IBSocket *>(socket_.get())->close();
  }
#endif
  co_return;
}

CoTask<void> Transport::closeDataPlane() {
  if (kind() == TransportKind::RDMA) {
    co_await closeIB();
  } else if (kind() == TransportKind::CXL && socket_) {
    invalidate();
    XLOGF(DBG, "Wait CXL socket {} last read/write finished begin", fmt::ptr(socket_.get()));
    co_await lastReadAndWriteFinished_;
    XLOGF(DBG, "Wait CXL socket {} last read/write finished end", fmt::ptr(socket_.get()));
    if (auto eventLoop = eventLoop_.lock()) {
      (void)eventLoop->remove(this);
    }
    dynamic_cast<cxl::CxlSocket *>(socket_.get())->close();
  }
}

Result<Void> Transport::getPeerCredentials() {
  if (!serverAddr_.isUNIX()) {
    // skip if it is not a UNIX domain socket.
    return Void{};
  }

  struct ucred credentials {};
  socklen_t len = sizeof(credentials);
  int ret = ::getsockopt(socket_->fd(), SOL_SOCKET, SO_PEERCRED, &credentials, &len);
  if (UNLIKELY(ret == -1)) {
    auto msg = fmt::format("::getsockopt({}) failed: errno {}", socket_->fd(), errno);
    XLOGF(ERR, msg);
    return makeError(RPCCode::kSocketError, std::move(msg));
  }
  credentials_ = credentials;
  return Void{};
}

template <uint32_t CheckAndRemove, uint32_t WantToRemove, MethodName Name>
Transport::Action Transport::tryToSuspend() {
  auto flags = flags_.load(std::memory_order_acquire);
  while (true) {
    if (UNLIKELY(flags & kInvalidatedFlag)) {
      return Action::Fail;
    } else if ((flags & CheckAndRemove)) {
      flags_ &= ~CheckAndRemove;
      static monitor::CountRecorder recorder(fmt::format("common_{}_retry", Name.str()));
      recorder.addSample(1);
      return Action::Retry;
    }
    auto newFlags = flags & ~WantToRemove;
    if (LIKELY(flags_.compare_exchange_strong(flags, newFlags))) {
      // wait next epoll.
      static monitor::CountRecorder recorder(fmt::format("common_{}_suspend", Name.str()));
      recorder.addSample(1);
      return Action::Suspend;
    }
  }
}

void Transport::doRead(bool error, bool logError /* = true */) {
  Socket::ExecutionScope executionScope(&ioWorker_);
  lastUsedTime_ = RelativeTime::now();

  auto guard = folly::makeGuard([startTime = std::chrono::steady_clock::now()] {
    auto elapsed = std::chrono::steady_clock::now() - startTime;
    readCPUTime.addSample(std::chrono::duration_cast<std::chrono::microseconds>(elapsed).count());
  });

  if (UNLIKELY(error)) {
    XLOGF_IF(WARNING, logError, "transport {} read error notified by epoll", describe());
    return tryToCleanUp(false);
  }

  flags_ &= ~kReadNewWakedFlag;
  while (true) {
    auto result = socket_->recv(folly::MutableByteRange{readBuff_->writableTail(), readBuff_->tailroom()});
    if (UNLIKELY(result.hasError())) {
      if (RpcTrace::enabled()) rpcTrace(0, 0, 0, "stream_receive_failed", result.error().code(), describe());
      XLOGF(WARNING, "transport {} receive failed: {}", describe(), result.error());
      return tryToCleanUp(false);
    }

    auto readSize = result.value();
    TransportEvidence::process().receive(kind_, servicePlane_, readSize);
    if (readSize == 0) {
      auto action = tryToSuspend<kReadNewWakedFlag, kReadAvailableFlag, "read_available">();
      if (action == Action::Suspend) {
        return;
      } else if (action == Action::Retry) {
        continue;
      } else if (action == Action::Fail) {
        XLOGF(WARNING, "transport {} ready to delete in read", describe());
        return tryToCleanUp(false);
      }
    }

    readBytes.addSample(readSize);
    batchReadSize.addSample(readSize);
    readBuff_->append(readSize);

    MessageWrapper msgWrapper(std::move(readBuff_));
    while (msgWrapper.headerComplete()) {
      // check message size.
      auto size = msgWrapper.header().size;
      if (UNLIKELY(size >= kMessageMaxSize)) {
        XLOGF(ERR, "transport {} receive a message with too large size: {}", describe(), size);
        return tryToCleanUp(false);
      }
      if (msgWrapper.messageComplete()) {
        // read a full message.
        msgWrapper.next();
      } else {
        break;
      }
    }

    if (msgWrapper.hasMessages()) {
      // allocate a new read buffer with remaining incomplete message.
      readBuff_ = msgWrapper.createFromRemain(kMessageReadBufferSize);
      // seek to beginning of the buffer and process this batch of messages.
      msgWrapper.seekBegin();
      ioWorker_.processMsg(std::move(msgWrapper), shared_from_this());
    } else if (msgWrapper.headerComplete() && msgWrapper.messageLength() > msgWrapper.capacity()) {
      readBuff_ = msgWrapper.createFromRemain(kMessageReadBufferSize);
    } else {
      // message is not complete, and the tail spase is enough to store the remaining part.
      readBuff_ = msgWrapper.detach();
    }
  }
}

void Transport::doWrite(bool error, bool logError /* = true */) {
  Socket::ExecutionScope executionScope(&ioWorker_);
  auto guard = folly::makeGuard([startTime = std::chrono::steady_clock::now()] {
    auto elapsed = std::chrono::steady_clock::now() - startTime;
    doWriteCPUTime.addSample(std::chrono::duration_cast<std::chrono::microseconds>(elapsed).count());
  });

  if (UNLIKELY(error)) {
    XLOGF_IF(WARNING, logError, "transport {} write error notified by epoll", describe());
    return tryToCleanUp(true);
  }

  flags_ &= ~(kWriteNewMsgFlag | kWriteNewWakedFlag);
  while (true) {
    // 1. collect items to write.
    auto newList = mpscWriteList_.takeOut();
    if (newList.empty() && inWritingList_.empty()) {
      auto flushed = socket_->flush();
      if (UNLIKELY(!flushed)) {
        XLOGF(WARNING, "transport {} flush failed: {}", describe(), flushed.error());
        return tryToCleanUp(true);
      }
      if (UNLIKELY(!observePublication())) {
        return tryToCleanUp(true);
      }
      auto action = tryToSuspend<kWriteNewMsgFlag, kWriteHasMsgFlag, "write_has_msg">();
      if (action == Action::Suspend) {
        return;
      } else if (action == Action::Retry) {
        continue;
      } else if (action == Action::Fail) {
        XLOGF(WARNING, "transport {} ready to delete in write", describe());
        return tryToCleanUp(true);
      }
    }

    // 2. concat write item list.
    if (!newList.empty()) {
      if (publicationLedger_) {
        auto assigned = newList.assignPublicationRanges(*publicationLedger_);
        if (UNLIKELY(!assigned)) {
          XLOGF(ERR, "transport {} failed to reserve publication ranges: {}", describe(), assigned.error());
          inWritingList_.concat(std::move(newList));
          return tryToCleanUp(true);
        }
      }
      inWritingList_.concat(std::move(newList));
    }

    // 3. write remain data.
    if (!inWritingList_.empty()) {
      auto action = writeAll();
      if (action == Action::Suspend) {
        return;
      } else if (action == Action::Fail) {
        XLOGF(WARNING, "transport {} ready to delete in write", describe());
        return tryToCleanUp(true);
      }
    }
  }
}

Transport::Action Transport::writeAll() {
  auto guard = folly::makeGuard([startTime = std::chrono::steady_clock::now()] {
    auto elapsed = std::chrono::steady_clock::now() - startTime;
    writeAllCPUTime.addSample(std::chrono::duration_cast<std::chrono::microseconds>(elapsed).count());
  });

  constexpr uint32_t kMaxBatchSize = 64;
  struct iovec iov[kMaxBatchSize];

  while (!inWritingList_.empty()) {
    size_t expectedWriteSize = 0;
    uint32_t len = inWritingList_.toIOVec(iov, kMaxBatchSize, expectedWriteSize);
    auto result = socket_->send(iov, len);
    if (result.hasError()) {
      if (RpcTrace::enabled()) rpcTrace(0, 0, 0, "stream_send_failed", result.error().code(), describe());
      return Action::Fail;
    }

    writeBytes.addSample(result.value());
    batchWriteSize.addSample(result.value());
    auto advanced = inWritingList_.advance(result.value(), publicationLedger_.get());
    if (UNLIKELY(!advanced)) {
      return Action::Fail;
    }
    if (UNLIKELY(!observePublication())) {
      return Action::Fail;
    }
    if (result.value() < expectedWriteSize) {
      return tryToSuspend<kWriteNewWakedFlag, kWriteAvailableFlag, "write_available">();
    }
  }
  return Action::OK;
}

void Transport::tryToCleanUp(bool isWrite) {
  // try to start both read and write task with error flag.
  invalidate();

  if (isWrite) {
    // clean up write status.
    auto retryList = retirePublication();
    if (!publicationLedger_ && kind_ != TransportKind::CXL) {
      retryList.concat(inWritingList_.extractForRetry());
    } else if (!publicationLedger_) {
      inWritingList_.clear();
    }
    retryList.concat(mpscWriteList_.takeOut().extractForRetry());
    if (!retryList.empty()) {
      ioWorker_.retryAsync(serverAddr_, std::move(retryList));
    }
    wakeUpAfterLastReadAndWriteFinished(flags_ |= kLastWriteFinished);
  } else {
    wakeUpAfterLastReadAndWriteFinished(flags_ |= kLastReadFinished);
  }
}

Result<Void> Transport::observePublication() {
  if (!publicationLedger_) {
    return Void{};
  }
  auto snapshot = socket_->publicationSnapshot();
  if (!snapshot) {
    return makeError(StatusCode::kDataCorruption, "CXL transport lost its publication snapshot");
  }
  auto observed = publicationLedger_->observe(*snapshot);
  if (UNLIKELY(!observed)) {
    XLOGF(WARNING,
          "transport {} publication observation failed: {} generation={} accepted={} published={} delivered={} trustworthy={} pending={}",
          describe(), observed.error(), snapshot->laneGeneration, snapshot->acceptedOffset,
          snapshot->publishedOffset, snapshot->peerDeliveredOffset, snapshot->trustworthy, snapshot->pending);
  }
  return observed;
}

void Transport::initializePublicationLedger() {
  if (!socket_ || kind_ != TransportKind::CXL) {
    return;
  }
  auto snapshot = socket_->publicationSnapshot();
  if (snapshot && snapshot->trustworthy) {
    publicationLedger_ = std::make_unique<PublicationLedger>(snapshot->laneGeneration, snapshot->acceptedOffset);
    if (RpcTrace::enabled()) rpcTrace(0, 0, 0, "publication_ledger_bound", 0,
        fmt::format("{} ledger={}", describe(), fmt::ptr(publicationLedger_.get())));
  }
}

WriteList Transport::retirePublication() {
  if (!publicationLedger_ || publicationRetired_.exchange(true, std::memory_order_acq_rel)) {
    return {};
  }
  auto retained = inWritingList_.retainRequests(*publicationLedger_);
  if (!retained) {
    XLOGF(ERR, "transport {} failed to retain in-flight CXL requests: {}", describe(), retained.error());
  }

  auto snapshot = socket_->publicationSnapshot().value_or(PublicationSnapshot{});
  // Until a peer retirement acknowledgement is implemented, a local close
  // does not prove that the peer has stopped consuming already-published
  // bytes.  Force conservative LaneRetired classification.
  snapshot.trustworthy = false;
  // close() invalidates the eventfd. Remove its kernel registration while the
  // descriptor is still valid; destructor-time removal would use fd == -1.
  if (auto eventLoop = eventLoop_.lock()) {
    (void)eventLoop->remove(this);
  }
  dynamic_cast<cxl::CxlSocket *>(socket_.get())->close();

  WriteList retry;
  for (auto &resolution : publicationLedger_->retire(snapshot)) {
    if (RpcTrace::enabled()) rpcTrace(0, 0, resolution.uuid, "publication_retired", 0,
        fmt::format("{} begin={} end={} disposition={} lifetime_refs={}", describe(),
                    resolution.range.beginOffset, resolution.range.endOffset,
                    static_cast<unsigned>(resolution.disposition), resolution.requestLifetime.use_count()));
    if (resolution.disposition == CompletionDisposition::RejectedBeforeExecute && resolution.retryable) {
      resolution.retryable->publication.reset();
      retry.concat(WriteList(std::move(resolution.retryable)));
    } else {
      TransportRuntime::quarantineCxlLifetime(std::move(resolution.requestLifetime));
      Waiter::instance().failWithDisposition(resolution.uuid, Status(RPCCode::kTimeout), resolution.disposition);
    }
  }
  return retry.extractForRetry();
}

void Transport::handleEvents(uint32_t epollEvents) {
  // 1. poll socket.
  auto results = socket_->poll(epollEvents);
  bool error = results.hasError();

  // 2. construct mask from events of poll.
  Socket::Events events = error ? (Socket::kEventReadableFlag | Socket::kEventWritableFlag) : results.value();
  bool doRead = events & Socket::kEventReadableFlag;
  bool doWrite = events & Socket::kEventWritableFlag;
  uint32_t mask = 0;
  if (doRead) {
    mask |= (kReadAvailableFlag | kReadNewWakedFlag);
  }
  if (doWrite) {
    mask |= (kWriteAvailableFlag | kWriteNewWakedFlag);
  }

  // 3. start read/write tasks based on flags.
  auto flags = flags_.fetch_or(mask);
  if (UNLIKELY(flags & kInvalidatedFlag)) {
    // broken connection. do nothing.
    return;
  }
  if (doRead && LIKELY((flags & kReadAvailableFlag) == 0)) {
    ioWorker_.startReadTask(this, error);
  }
  if (doWrite && LIKELY((flags & (kWriteAvailableFlag)) == 0 && (flags & kWriteHasMsgFlag) != 0)) {
    ioWorker_.startWriteTask(this, error);
  }
}

void Transport::wakeUpAfterLastReadAndWriteFinished(uint32_t flags) {
  constexpr auto kAllFinished = (kLastReadFinished | kLastWriteFinished);
  if ((flags & kAllFinished) == kAllFinished) {
    Waiter::instance().clearPendingRequestsOnTransportFailure(this);
    lastReadAndWriteFinished_.post();
  }
}

}  // namespace hf3fs::net
