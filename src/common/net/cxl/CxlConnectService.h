#pragma once

#include <array>
#include <atomic>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <vector>

#include "common/net/TransportKind.h"
#include "common/net/cxl/CxlFabric.h"
#include "common/net/cxl/CxlSocket.h"
#include "common/serde/CallContext.h"
#include "common/serde/Serde.h"
#include "common/serde/Service.h"

namespace hf3fs::net::cxl {

struct CxlConnectionNonce {
  SERDE_STRUCT_FIELD(low, uint64_t{});
  SERDE_STRUCT_FIELD(high, uint64_t{});

 public:
  bool operator==(const CxlConnectionNonce &) const = default;
};

struct CxlConnectDirection {
  SERDE_STRUCT_FIELD(producer_cursor_offset, uint64_t{});
  SERDE_STRUCT_FIELD(consumer_cursor_offset, uint64_t{});
  SERDE_STRUCT_FIELD(delivered_record_offset, uint64_t{});
  SERDE_STRUCT_FIELD(frame_ring_offset, uint64_t{});
  SERDE_STRUCT_FIELD(payload_cells_offset, uint64_t{});
};
struct CxlConnectReq {
  SERDE_STRUCT_FIELD(abi_major, uint16_t{kCxlAbiVersion});
  SERDE_STRUCT_FIELD(abi_minor, uint16_t{kCxlAbiMinorVersion});
  SERDE_STRUCT_FIELD(session_generation, uint64_t{});
  SERDE_STRUCT_FIELD(requester_endpoint, uint32_t{});
  SERDE_STRUCT_FIELD(requester_endpoint_generation, uint64_t{});
  SERDE_STRUCT_FIELD(requester_address, uint64_t{});
  SERDE_STRUCT_FIELD(target_endpoint, uint32_t{});
  SERDE_STRUCT_FIELD(service_plane, uint8_t{});
  SERDE_STRUCT_FIELD(queue_depth, uint32_t{64});
  SERDE_STRUCT_FIELD(cell_bytes, uint32_t{64U * 1024U});
  SERDE_STRUCT_FIELD(capability_bits, uint64_t{});
  SERDE_STRUCT_FIELD(connection_nonce, CxlConnectionNonce{});
};

struct CxlConnectRsp {
  SERDE_STRUCT_FIELD(session_generation, uint64_t{});
  SERDE_STRUCT_FIELD(lane_id, uint32_t{});
  SERDE_STRUCT_FIELD(lane_generation, uint64_t{});
  SERDE_STRUCT_FIELD(requester_endpoint, uint32_t{});
  SERDE_STRUCT_FIELD(requester_endpoint_generation, uint64_t{});
  SERDE_STRUCT_FIELD(target_endpoint, uint32_t{});
  SERDE_STRUCT_FIELD(target_endpoint_generation, uint64_t{});
  SERDE_STRUCT_FIELD(service_plane, uint8_t{});
  SERDE_STRUCT_FIELD(queue_depth, uint32_t{});
  SERDE_STRUCT_FIELD(cell_bytes, uint32_t{});
  SERDE_STRUCT_FIELD(capability_bits, uint64_t{});
  SERDE_STRUCT_FIELD(connection_nonce, CxlConnectionNonce{});
  SERDE_STRUCT_FIELD(requester_owner_record_offset, uint64_t{});
  SERDE_STRUCT_FIELD(acceptor_owner_record_offset, uint64_t{});
  SERDE_STRUCT_FIELD(submission, CxlConnectDirection{});
  SERDE_STRUCT_FIELD(completion, CxlConnectDirection{});

 public:
  CxlLaneConfig laneConfig() const;
};

struct CxlActivateReq {
  SERDE_STRUCT_FIELD(session_generation, uint64_t{});
  SERDE_STRUCT_FIELD(lane_id, uint32_t{});
  SERDE_STRUCT_FIELD(lane_generation, uint64_t{});
  SERDE_STRUCT_FIELD(requester_endpoint, uint32_t{});
  SERDE_STRUCT_FIELD(requester_endpoint_generation, uint64_t{});
  SERDE_STRUCT_FIELD(target_endpoint, uint32_t{});
  SERDE_STRUCT_FIELD(target_endpoint_generation, uint64_t{});
  SERDE_STRUCT_FIELD(service_plane, uint8_t{});
  SERDE_STRUCT_FIELD(connection_nonce, CxlConnectionNonce{});
};

struct CxlActivateRsp {
  SERDE_STRUCT_FIELD(activated, bool{});
};

static_assert(serde::SerializableToBytes<CxlConnectReq> && serde::SerializableToJson<CxlConnectReq>);
static_assert(serde::SerializableToBytes<CxlConnectRsp> && serde::SerializableToJson<CxlConnectRsp>);
static_assert(serde::SerializableToBytes<CxlActivateReq> && serde::SerializableToJson<CxlActivateReq>);
static_assert(serde::SerializableToBytes<CxlActivateRsp> && serde::SerializableToJson<CxlActivateRsp>);

SERDE_SERVICE(CxlConnect, 12) {
  SERDE_SERVICE_METHOD(connect, 1, CxlConnectReq, CxlConnectRsp);
  SERDE_SERVICE_METHOD(activate, 2, CxlActivateReq, CxlActivateRsp);
};

struct CxlConnectMetricsSnapshot {
  uint64_t metadataRequests{};
  uint64_t metadataBytes{};
  uint64_t bootstrapServingBytes{};
  uint64_t rejectedRequests{};
};

class CxlConnectMetrics {
 public:
  void accepted(size_t metadataBytes) noexcept;
  void rejected() noexcept;
  CxlConnectMetricsSnapshot snapshot() const noexcept;

 private:
  std::atomic<uint64_t> metadataRequests_{};
  std::atomic<uint64_t> metadataBytes_{};
  std::atomic<uint64_t> rejectedRequests_{};
};

class CxlConnectService : public serde::ServiceWrapper<CxlConnectService, CxlConnect> {
 public:
  using AcceptFn = std::function<Result<Void>(std::unique_ptr<CxlSocket>, ServicePlane)>;

  CxlConnectService(std::shared_ptr<CxlFabric> fabric,
                    AcceptFn accept,
                    std::shared_ptr<CxlConnectMetrics> metrics = nullptr);

  CoTryTask<CxlConnectRsp> connect(serde::CallContext &ctx, const CxlConnectReq &req);
  CoTryTask<CxlActivateRsp> activate(serde::CallContext &ctx, const CxlActivateReq &req);
  Result<CxlConnectRsp> connectForTest(const CxlConnectReq &req);
  Result<CxlActivateRsp> activateForTest(const CxlActivateReq &req);

  static CxlActivateReq activationRequest(const CxlConnectRsp &rsp);

  static Result<std::shared_ptr<CxlSocket>> finishRequester(std::shared_ptr<CxlFabric> fabric,
                                                            const CxlConnectReq &req,
                                                            const CxlConnectRsp &rsp,
                                                            Address peer);
  static Result<std::unique_ptr<CxlSocket>> finishRequesterUnique(std::shared_ptr<CxlFabric> fabric,
                                                                  const CxlConnectReq &req,
                                                                  const CxlConnectRsp &rsp,
                                                                  Address peer);
  static Result<Void> faultRequester(const std::shared_ptr<CxlFabric> &fabric, const CxlConnectRsp &rsp);

  const std::shared_ptr<CxlConnectMetrics> &metrics() const noexcept { return metrics_; }

 private:
  struct ConnectionKey {
    uint32_t endpoint{};
    uint64_t generation{};
    CxlConnectionNonce nonce{};
    bool operator==(const ConnectionKey &) const = default;
  };

  struct PendingConnection {
    CxlConnectRsp descriptor;
    std::unique_ptr<CxlSocket> socket;
  };

  struct LaneLeaseRegistry {
    explicit LaneLeaseRegistry(size_t laneCount)
        : activeLanes(laneCount) {}

    std::mutex mutex;
    std::vector<bool> activeLanes;
    std::vector<ConnectionKey> activeConnections;
  };

  Result<CxlConnectRsp> validateAndAllocate(const CxlConnectReq &req);
  Result<CxlConnectRsp> allocateLocked(const CxlConnectReq &req, uint64_t negotiatedCapabilities);
  CxlSocket::CloseHook acceptorCloseHook(const CxlConnectRsp &descriptor) const;
  void retireCanceledPending();

  std::shared_ptr<CxlFabric> fabric_;
  std::shared_ptr<BulkTransfer> bulkTransfer_;
  AcceptFn accept_;
  std::shared_ptr<CxlConnectMetrics> metrics_;
  std::shared_ptr<LaneLeaseRegistry> leases_;
  std::mutex pendingMutex_;
  std::vector<PendingConnection> pendingConnections_;
};

}  // namespace hf3fs::net::cxl
