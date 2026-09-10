#pragma once

#include <atomic>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <thread>
#include <utility>
#include <vector>

#include "common/net/Socket.h"
#include "common/net/cxl/CxlLane.h"
#include "common/net/cxl/CxlMetrics.h"
#include "common/net/cxl/CxlProgressEngine.h"
#include "common/utils/ConfigBase.h"

namespace hf3fs::net::cxl {

class CxlSocket final : public Socket {
 public:
  class Config : public ConfigBase<Config> {
    CONFIG_HOT_UPDATED_ITEM(queue_depth, 64u, ConfigCheckers::checkPositive);
    CONFIG_HOT_UPDATED_ITEM(cell_bytes, 64U * 1024U, ConfigCheckers::checkPositive);
  };

  using ConnectedPair = std::pair<std::shared_ptr<CxlSocket>, std::shared_ptr<CxlSocket>>;
  using CloseHook = std::function<void(bool faulted)>;
  using PeerCheck = std::function<Result<Void>()>;

  static Result<std::shared_ptr<CxlSocket>> create(CxlLane lane,
                                                   Address peer,
                                                   std::shared_ptr<CxlProgressEngine> progressEngine,
                                                   std::shared_ptr<BulkTransfer> bulkTransfer = nullptr);
  static Result<std::unique_ptr<CxlSocket>> createUnique(CxlLane lane,
                                                         Address peer,
                                                         std::shared_ptr<CxlProgressEngine> progressEngine,
                                                         std::shared_ptr<BulkTransfer> bulkTransfer = nullptr);
  static Result<ConnectedPair> createConnectedPairForTest(CxlRegion &region,
                                                          const CxlLayout &layout,
                                                          const CxlLaneConfig &config,
                                                          std::shared_ptr<CxlProgressEngine> progressEngine,
                                                          Address requesterPeer,
                                                          Address acceptorPeer);
  static Result<ConnectedPair> createConnectedPairForTest(CxlRegion &region,
                                                          const CxlLayout &layout,
                                                          std::shared_ptr<CxlProgressEngine> progressEngine,
                                                          Address requesterPeer,
                                                          Address acceptorPeer);

  ~CxlSocket() override;

  std::string describe() override;
  folly::IPAddressV4 peerIP() override;
  TransportKind kind() const noexcept override { return TransportKind::CXL; }
  BulkTransfer *bulkTransfer() noexcept override { return bulkTransfer_.get(); }
  std::optional<PublicationSnapshot> publicationSnapshot() const noexcept override;
  Result<Void> bindExecutionOwner(const void *owner) override;
  int fd() const override { return eventFd_.load(std::memory_order_acquire); }
  Result<Events> poll(uint32_t events) override;
  Result<size_t> recv(folly::MutableByteRange buffer) override;
  Result<size_t> send(struct iovec iov[], uint32_t length) override;
  Result<Void> flush() override;
  Result<Void> check() override;

  void close() noexcept;
  void setCloseHook(CloseHook hook) noexcept { closeHook_ = std::move(hook); }
  void setPeerCheck(PeerCheck check);
  bool closed() const noexcept { return closed_.load(std::memory_order_acquire); }
  size_t stagedBytes() const noexcept { return stagedBytes_; }
  bool hasReservedSlot() const noexcept { return reservedSlot_; }
  const std::shared_ptr<CxlMetrics> &metrics() const noexcept { return metrics_; }

 private:
  CxlSocket(CxlLane lane,
            Address peer,
            int eventFd,
            std::shared_ptr<CxlProgressEngine> progressEngine,
            std::shared_ptr<CxlMetrics> metrics,
            std::shared_ptr<BulkTransfer> bulkTransfer);

  Result<Void> bindProducer();
  Result<Void> bindConsumer();
  Result<Void> publishStaging();
  Result<Void> checkPeer() const;
  Events computeReady() const noexcept;
  bool cacheLaneState() noexcept;
  void rearmAndRecheck(Events interest) noexcept;
  bool progress() noexcept;
  void signal() noexcept;
  void markFault() const noexcept;

  CxlLane lane_;
  Address peer_;
  Direction outbound_;
  Direction inbound_;
  std::atomic<int> eventFd_;
  std::shared_ptr<CxlProgressEngine> progressEngine_;
  std::shared_ptr<CxlMetrics> metrics_;
  std::shared_ptr<BulkTransfer> bulkTransfer_;
  std::vector<std::byte> staging_;
  size_t stagedBytes_{};
  bool reservedSlot_{};
  std::atomic<uint64_t> acceptedOffset_{};
  std::atomic<uint64_t> publishedOffset_{};
  mutable std::atomic<uint64_t> lastDeliveredOffset_{};
  std::atomic<uint32_t> armedMask_{kEventReadableFlag | kEventWritableFlag};
  std::atomic<uint32_t> lastReadyMask_{};
  std::atomic<uint64_t> lastSubmissionProducer_{};
  std::atomic<uint64_t> lastSubmissionConsumer_{};
  std::atomic<uint64_t> lastCompletionProducer_{};
  std::atomic<uint64_t> lastCompletionConsumer_{};
  std::atomic<uint64_t> firstPublishNs_{};
  mutable std::atomic<uint64_t> firstInboundVisibleNs_{};
  std::atomic<uint64_t> firstInboundConsumedNs_{};
  std::atomic<bool> closed_{};
  mutable std::atomic<bool> faulted_{};
  mutable std::mutex ownerMutex_;
  std::atomic<const void *> executionOwner_{};
  std::optional<std::thread::id> producerOwner_;
  std::optional<std::thread::id> consumerOwner_;
  CloseHook closeHook_;
  mutable std::mutex peerCheckMutex_;
  PeerCheck peerCheck_;
  mutable std::atomic<status_code_t> peerError_{StatusCode::kOK};

  friend class CxlProgressEngine;
};

}  // namespace hf3fs::net::cxl
