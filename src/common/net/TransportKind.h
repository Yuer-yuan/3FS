#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <optional>

#include "common/utils/Address.h"

namespace hf3fs::net {

enum class TransportKind : uint8_t { TCP, RDMA, CXL };

enum class ServicePlane : uint8_t { Control, Data };

struct ServiceEndpoint {
  Address address;
  ServicePlane plane;

  bool operator==(const ServiceEndpoint &) const = default;
};

constexpr TransportKind transportKind(Address::Type type) {
  if (type == Address::RDMA) {
    return TransportKind::RDMA;
  }
  if (type == Address::CXL) {
    return TransportKind::CXL;
  }
  return TransportKind::TCP;
}

// Compatibility inference for legacy address-only routes. CXL is deliberately
// excluded because one CXL address can carry either plane and must be explicit.
constexpr std::optional<ServicePlane> legacyServicePlane(Address::Type type) {
  switch (transportKind(type)) {
    case TransportKind::TCP:
      return ServicePlane::Control;
    case TransportKind::RDMA:
      return ServicePlane::Data;
    case TransportKind::CXL:
      return std::nullopt;
  }
  return std::nullopt;
}

constexpr size_t planeIndex(ServicePlane plane) { return static_cast<size_t>(plane); }

}  // namespace hf3fs::net

template <>
struct std::hash<hf3fs::net::ServiceEndpoint> {
  size_t operator()(const hf3fs::net::ServiceEndpoint &endpoint) const {
    auto addressHash = std::hash<hf3fs::net::Address>{}(endpoint.address);
    auto planeHash = static_cast<size_t>(endpoint.plane);
    return addressHash ^ (planeHash + 0x9e3779b9U + (addressHash << 6U) + (addressHash >> 2U));
  }
};
