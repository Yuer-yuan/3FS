#pragma once

#include <memory>
#include <string>
#include <vector>

#include "common/net/TransportKind.h"
#include "common/net/cxl/CxlBuffer.h"
#include "common/net/cxl/CxlFabric.h"
#include "common/serde/Serde.h"
#include "common/utils/ConfigBase.h"
#include "common/utils/Result.h"
#include "common/utils/Size.h"

namespace hf3fs::net {

struct CxlRoute {
  ServiceEndpoint endpoint;
  cxl::EndpointId target;

  bool operator==(const CxlRoute &) const = default;
};

class CxlRouteTable {
 public:
  static Result<CxlRouteTable> create(Address localAddress, std::vector<CxlRoute> routes, uint32_t endpointCount);

  Result<cxl::EndpointId> resolve(ServiceEndpoint endpoint) const;
  Result<ServiceEndpoint> resolve(Address address) const;
  Address localAddress() const noexcept { return localAddress_; }

 private:
  CxlRouteTable(Address localAddress, std::vector<CxlRoute> routes)
      : localAddress_(localAddress),
        routes_(std::move(routes)) {}

  Address localAddress_;
  std::vector<CxlRoute> routes_;
};

struct CxlConnectionContext {
  std::shared_ptr<cxl::CxlFabric> fabric;
  Address localAddress;
  cxl::EndpointId target;
};

struct CxlManifestRange {
  SERDE_STRUCT_FIELD(kind, cxl::CxlRangeKind{});
  SERDE_STRUCT_FIELD(offset, uint64_t{});
  SERDE_STRUCT_FIELD(length, uint64_t{});
};

struct CxlManifestParticipant {
  SERDE_STRUCT_FIELD(endpoint, uint32_t{});
  SERDE_STRUCT_FIELD(localAddress, std::string{});
};

struct CxlManifestRoute {
  SERDE_STRUCT_FIELD(address, std::string{});
  SERDE_STRUCT_FIELD(plane, ServicePlane{});
  SERDE_STRUCT_FIELD(targetEndpoint, uint32_t{});
};

struct CxlRuntimeManifest {
  SERDE_STRUCT_FIELD(schema, std::string{});
  SERDE_STRUCT_FIELD(totalRegionBytes, uint64_t{});
  SERDE_STRUCT_FIELD(sessionGeneration, uint64_t{});
  SERDE_STRUCT_FIELD(lifecycleRecordOffset, uint64_t{});
  SERDE_STRUCT_FIELD(manifestSha256, std::string{});
  SERDE_STRUCT_FIELD(endpointCount, uint32_t{});
  SERDE_STRUCT_FIELD(laneCount, uint32_t{});
  SERDE_STRUCT_FIELD(authorityEndpoint, uint32_t{});
  SERDE_STRUCT_FIELD(authorityGeneration, uint64_t{});
  SERDE_STRUCT_FIELD(ranges, std::vector<CxlManifestRange>{});
  SERDE_STRUCT_FIELD(participants, std::vector<CxlManifestParticipant>{});
  SERDE_STRUCT_FIELD(routes, std::vector<CxlManifestRoute>{});
};

class TransportRuntime {
 public:
  class Config : public ConfigBase<Config> {
    CONFIG_ITEM(enabled, false);
    CONFIG_ITEM(mode, cxl::CxlFabric::StartMode::Attach);
    CONFIG_ITEM(region_type, cxl::CxlFabric::RegionType::Dax);
    CONFIG_ITEM(region_path, std::string{});
    CONFIG_ITEM(region_offset, 0_B);
    CONFIG_ITEM(region_length, 0_B);
    CONFIG_ITEM(manifest_path, std::string{});
    CONFIG_ITEM(endpoint, uint32_t{});
    CONFIG_ITEM(endpoint_generation, uint64_t{});
    CONFIG_ITEM(capability_bits, uint64_t{});
    CONFIG_ITEM(attach_timeout, 1_s);
    CONFIG_ITEM(heartbeat_interval, 10_ms);
    CONFIG_ITEM(authority_stale_timeout, 1_s);
    CONFIG_ITEM(shutdown_timeout, 1_s);
    CONFIG_ITEM(poll_spin, 20_us);
    CONFIG_ITEM(poll_yields, 8u);
    CONFIG_ITEM(poll_sleep, 10_us);
    CONFIG_ITEM(poll_adaptive, true);
    CONFIG_ITEM(poll_idle_sleep_min, 1_us);
    CONFIG_ITEM(poll_idle_sleep_max, 50_us);
    CONFIG_ITEM(authority_owner_lock, std::string{});
    CONFIG_ITEM(authority_receipt, std::string{});
  };

  static Result<Void> startConfigured(const Config &config);
  static Result<Void> startCxl(cxl::CxlFabric::Config config);
  static Result<Void> startCxl(cxl::CxlFabric::Config config, std::vector<cxl::EndpointId> activeEndpoints);
  static Result<Void> startCxl(cxl::CxlFabric::Config config,
                               Address localAddress,
                               std::vector<CxlRoute> routes,
                               std::vector<cxl::EndpointId> activeEndpoints = {});
  static Result<Void> stopCxl();
  static std::shared_ptr<cxl::CxlFabric> cxlFabric();
  static std::shared_ptr<cxl::CxlBufferArena> cxlBufferArena();
  static Result<CxlConnectionContext> cxlConnection(ServiceEndpoint endpoint);
  static Result<ServiceEndpoint> cxlServiceEndpoint(Address address);
  static void quarantineCxlLifetime(std::shared_ptr<void> lifetime);
};

}  // namespace hf3fs::net
