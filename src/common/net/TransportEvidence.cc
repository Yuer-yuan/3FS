#include "common/net/TransportEvidence.h"

#include <fmt/format.h>

namespace hf3fs::net {

TransportEvidence &TransportEvidence::process() {
  static TransportEvidence evidence;
  return evidence;
}

void TransportEvidence::add(Counter counter, uint64_t bytes) noexcept {
  counters_[counter].fetch_add(bytes, std::memory_order_relaxed);
}

void TransportEvidence::receive(TransportKind kind, ServicePlane plane, uint64_t bytes) noexcept {
  if (kind == TransportKind::TCP) {
    add(TcpReceiveBytes, bytes);
    if (plane == ServicePlane::Data) {
      add(TcpDataPlaneBytes, bytes);
    }
  }
}

void TransportEvidence::rpc(TransportKind kind,
                            ServicePlane plane,
                            uint16_t service,
                            bool request,
                            uint64_t wireBytes) noexcept {
  if (kind == TransportKind::CXL) {
    add(request ? CxlRpcRequests : CxlRpcResponses);
    add(CxlRpcBytes, wireBytes);
  } else if (kind == TransportKind::TCP) {
    if (plane == ServicePlane::Control && service == 10001) {
      add(CoreTcpBytes, wireBytes);
    } else if (plane == ServicePlane::Control && service == 12) {
      add(BootstrapControlBytes, wireBytes);
    } else {
      // Also detect a serving service incorrectly assigned to TCP Control.
      add(BootstrapServingBytes, wireBytes);
    }
  }
}

TransportEvidence::Snapshot TransportEvidence::snapshot() const noexcept {
  Snapshot result{};
  for (size_t i = 0; i < Count; ++i) {
    result[i] = counters_[i].load(std::memory_order_relaxed);
  }
  return result;
}

std::string TransportEvidence::json() const {
  constexpr std::array names = {"rdma_open_attempts",
                                "tcp_receive_bytes",
                                "tcp_data_plane_bytes",
                                "core_tcp_bytes",
                                "bootstrap_control_bytes",
                                "bootstrap_serving_bytes",
                                "cxl_rpc_requests",
                                "cxl_rpc_responses",
                                "cxl_rpc_bytes",
                                "cxl_bulk_read_bytes",
                                "cxl_bulk_write_bytes"};
  static_assert(names.size() == Count);
  const auto values = snapshot();
  std::string result = "{";
  for (size_t i = 0; i < Count; ++i) {
    result += fmt::format("{}\"{}\":{}", i ? "," : "", names[i], values[i]);
  }
  return result + "}";
}

}  // namespace hf3fs::net
