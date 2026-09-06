#pragma once

#include <array>
#include <atomic>
#include <cstdint>
#include <string>

#include "common/net/TransportKind.h"

namespace hf3fs::net {

// Process-lifetime monotonic counters. Received RPC frames are counted once,
// after CRC/serde validation, using their compressed wire size including header.
// Bulk bytes count successful local copies, not remotely acknowledged requests.
class TransportEvidence {
 public:
  enum Counter : size_t {
    RdmaOpenAttempts,
    TcpReceiveBytes,
    TcpDataPlaneBytes,
    CoreTcpBytes,
    BootstrapControlBytes,
    BootstrapServingBytes,
    CxlRpcRequests,
    CxlRpcResponses,
    CxlRpcBytes,
    CxlBulkReadBytes,
    CxlBulkWriteBytes,
    Count,
  };
  using Snapshot = std::array<uint64_t, Count>;

  static TransportEvidence &process();
  void add(Counter counter, uint64_t bytes = 1) noexcept;
  void receive(TransportKind kind, ServicePlane plane, uint64_t bytes) noexcept;
  void rpc(TransportKind kind, ServicePlane plane, uint16_t service, bool request, uint64_t wireBytes) noexcept;
  Snapshot snapshot() const noexcept;
  std::string json() const;

 private:
  std::array<std::atomic<uint64_t>, Count> counters_{};
};

}  // namespace hf3fs::net
