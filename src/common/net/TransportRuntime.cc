#include "common/net/RpcTrace.h"
#include "common/net/TransportRuntime.h"

#include <algorithm>
#include <array>
#include <mutex>
#include <optional>
#include <utility>
#include <cstdio>
#include <unistd.h>

#include "common/net/TransportEvidence.h"
#include "common/utils/FileUtils.h"

namespace hf3fs::net {
namespace {

constexpr std::string_view kRuntimeManifestSchema = "hf3fs.cxl-runtime-manifest.v1";

std::mutex runtimeMutex;
std::shared_ptr<cxl::CxlFabric> processFabric;
std::shared_ptr<cxl::CxlBufferArena> processBufferArena;
std::optional<CxlRouteTable> processRoutes;
std::vector<std::shared_ptr<void>> quarantinedLifetimes;

int hexNibble(char value) {
  if (value >= '0' && value <= '9') {
    return value - '0';
  }
  if (value >= 'a' && value <= 'f') {
    return value - 'a' + 10;
  }
  if (value >= 'A' && value <= 'F') {
    return value - 'A' + 10;
  }
  return -1;
}

Result<std::array<std::byte, 32>> parseManifestSha256(std::string_view text) {
  std::array<std::byte, 32> result{};
  if (text.size() != result.size() * 2U) {
    return makeError(StatusCode::kInvalidFormat, "CXL manifest SHA-256 must contain exactly 64 hex digits");
  }
  for (size_t index = 0; index < result.size(); ++index) {
    const int high = hexNibble(text[index * 2U]);
    const int low = hexNibble(text[index * 2U + 1U]);
    if (high < 0 || low < 0) {
      return makeError(StatusCode::kInvalidFormat, "CXL manifest SHA-256 contains a non-hex digit");
    }
    result[index] = static_cast<std::byte>((high << 4U) | low);
  }
  return result;
}

Result<CxlRuntimeManifest> loadRuntimeManifest(const std::string &path) {
  if (path.empty()) {
    return makeError(StatusCode::kInvalidConfig, "CXL runtime manifest path is empty");
  }
  auto contents = loadFile(Path(path));
  if (!contents) {
    return makeError(std::move(contents.error()));
  }
  CxlRuntimeManifest manifest;
  auto parsed = serde::fromJsonString(manifest, *contents);
  if (!parsed) {
    return makeError(StatusCode::kInvalidFormat, fmt::format("cannot parse CXL runtime manifest: {}", parsed.error()));
  }
  if (manifest.schema != kRuntimeManifestSchema) {
    return makeError(StatusCode::kInvalidFormat, "unsupported CXL runtime manifest schema");
  }
  return manifest;
}

Result<cxl::CxlLayoutManifest> makeLayoutManifest(const CxlRuntimeManifest &source) {
  if (source.ranges.size() != cxl::kCxlRangeCount || source.endpointCount == 0 || source.laneCount == 0 ||
      source.sessionGeneration == 0 || source.authorityEndpoint == 0 || source.authorityGeneration == 0) {
    return makeError(StatusCode::kInvalidConfig, "CXL runtime manifest has incomplete layout identity");
  }
  auto sha256 = parseManifestSha256(source.manifestSha256);
  if (!sha256) {
    return makeError(std::move(sha256.error()));
  }

  cxl::CxlLayoutManifest result{
      .totalRegionBytes = source.totalRegionBytes,
      .sessionGeneration = source.sessionGeneration,
      .lifecycleRecordOffset = source.lifecycleRecordOffset,
      .manifestSha256 = *sha256,
      .endpointCount = source.endpointCount,
      .laneCount = source.laneCount,
      .authorityEndpoint = cxl::EndpointId{source.authorityEndpoint},
      .authorityGeneration = source.authorityGeneration,
  };
  std::array<bool, cxl::kCxlRangeCount> seen{};
  for (const auto &range : source.ranges) {
    const auto kind = static_cast<size_t>(range.kind);
    if (kind == 0 || kind > cxl::kCxlRangeCount || seen[kind - 1U]) {
      return makeError(StatusCode::kInvalidConfig, "CXL runtime manifest has a missing or duplicate range kind");
    }
    seen[kind - 1U] = true;
    result.ranges[kind - 1U] = cxl::CxlRange{range.kind, range.offset, range.length};
  }
  return result;
}

}  // namespace

Result<Void> TransportRuntime::startConfigured(const Config &config) {
  if (!config.enabled()) {
    return Void{};
  }
  auto source = loadRuntimeManifest(config.manifest_path());
  if (!source) {
    return makeError(std::move(source.error()));
  }
  auto manifest = makeLayoutManifest(*source);
  if (!manifest) {
    return makeError(std::move(manifest.error()));
  }
  const uint64_t regionLength = config.region_length() ? config.region_length().toInt() : manifest->totalRegionBytes;
  cxl::CxlFabric::Config fabricConfig{
      .mode = config.mode(),
      .regionType = config.region_type(),
      .regionPath = config.region_path(),
      .regionOffset = config.region_offset(),
      .regionLength = regionLength,
      .manifest = *manifest,
      .endpoint = cxl::EndpointId{config.endpoint()},
      .endpointGeneration = config.endpoint_generation(),
      .capabilityBits = config.capability_bits(),
      .attachTimeout = config.attach_timeout().asMs(),
      .heartbeatInterval = config.heartbeat_interval(),
      .authorityStaleTimeout = config.authority_stale_timeout(),
      .shutdownTimeout = config.shutdown_timeout(),
      .poll =
          cxl::CxlPollConfig{
              .spin = config.poll_spin(),
              .yields = config.poll_yields(),
              .sleep = config.poll_sleep(),
              .adaptive = config.poll_adaptive(),
              .idleSleepMin = config.poll_idle_sleep_min(),
              .idleSleepMax = config.poll_idle_sleep_max(),
          },
      .authorityOwnerLock = config.authority_owner_lock(),
      .authorityReceipt = config.authority_receipt(),
  };

  std::optional<Address> localAddress;
  std::vector<cxl::EndpointId> activeEndpoints;
  activeEndpoints.reserve(source->participants.size());
  for (const auto &participant : source->participants) {
    auto parsed = Address::from(participant.localAddress);
    if (participant.endpoint == 0 || participant.endpoint > source->endpointCount || !parsed || !parsed->isCXL() ||
        std::find(activeEndpoints.begin(), activeEndpoints.end(), cxl::EndpointId{participant.endpoint}) !=
            activeEndpoints.end()) {
      return makeError(StatusCode::kInvalidConfig, "CXL manifest has an invalid or duplicate participant");
    }
    activeEndpoints.push_back(cxl::EndpointId{participant.endpoint});
    if (participant.endpoint == config.endpoint()) {
      if (localAddress) {
        return makeError(StatusCode::kInvalidConfig, "CXL endpoint has duplicate local addresses");
      }
      localAddress = *parsed;
    }
  }
  if (!localAddress) {
    return makeError(StatusCode::kInvalidConfig, "CXL endpoint has no local address in the runtime manifest");
  }
  if (config.mode() == cxl::CxlFabric::StartMode::InitializeAuthority) {
    return startCxl(std::move(fabricConfig), std::move(activeEndpoints));
  }

  std::vector<CxlRoute> routes;
  routes.reserve(source->routes.size());
  for (const auto &route : source->routes) {
    auto address = Address::from(route.address);
    if (!address || !address->isCXL()) {
      return makeError(StatusCode::kInvalidConfig, "CXL runtime manifest route has an invalid address");
    }
    routes.push_back(CxlRoute{ServiceEndpoint{*address, route.plane}, cxl::EndpointId{route.targetEndpoint}});
  }
  return startCxl(std::move(fabricConfig), *localAddress, std::move(routes), std::move(activeEndpoints));
}

Result<CxlRouteTable> CxlRouteTable::create(Address localAddress,
                                            std::vector<CxlRoute> routes,
                                            uint32_t endpointCount) {
  if (!localAddress.isCXL() || localAddress.ip == 0 || localAddress.port == 0 || endpointCount == 0) {
    return makeError(StatusCode::kInvalidConfig, "CXL route table requires a routable local CXL address");
  }
  for (size_t index = 0; index < routes.size(); ++index) {
    const auto &route = routes[index];
    if (!route.endpoint.address.isCXL() || route.endpoint.address.ip == 0 || route.endpoint.address.port == 0 ||
        route.target.value == 0 || route.target.value > endpointCount) {
      return makeError(StatusCode::kInvalidConfig, "CXL route contains an invalid address or endpoint identity");
    }
    if (std::find_if(routes.begin(), routes.begin() + index, [&](const CxlRoute &candidate) {
          return candidate.endpoint == route.endpoint;
        }) != routes.begin() + index) {
      return makeError(StatusCode::kInvalidConfig, "duplicate CXL address and service-plane route");
    }
  }
  return CxlRouteTable(localAddress, std::move(routes));
}

Result<cxl::EndpointId> CxlRouteTable::resolve(ServiceEndpoint endpoint) const {
  const auto route = std::find_if(routes_.begin(), routes_.end(), [&](const CxlRoute &candidate) {
    return candidate.endpoint == endpoint;
  });
  if (route == routes_.end()) {
    return makeError(
        StatusCode::kInvalidConfig,
        fmt::format("no exact CXL route for {} plane {}", endpoint.address, static_cast<uint8_t>(endpoint.plane)));
  }
  return route->target;
}

Result<ServiceEndpoint> CxlRouteTable::resolve(Address address) const {
  std::optional<ServiceEndpoint> resolved;
  for (const auto &route : routes_) {
    if (route.endpoint.address != address) {
      continue;
    }
    if (resolved && *resolved != route.endpoint) {
      return makeError(StatusCode::kInvalidConfig, "CXL address resolves to multiple service planes");
    }
    resolved = route.endpoint;
  }
  if (!resolved) {
    return makeError(StatusCode::kInvalidConfig, fmt::format("no CXL route for {}", address));
  }
  return *resolved;
}

Result<Void> TransportRuntime::startCxl(cxl::CxlFabric::Config config) {
  return startCxl(std::move(config), std::vector<cxl::EndpointId>{});
}

Result<Void> TransportRuntime::startCxl(cxl::CxlFabric::Config config, std::vector<cxl::EndpointId> activeEndpoints) {
  std::lock_guard lock(runtimeMutex);
  if (processFabric) {
    return makeError(StatusCode::kQueueConflict, "the process CXL transport runtime is already initialized");
  }
  auto fabric = cxl::CxlFabric::start(std::move(config), std::move(activeEndpoints));
  if (!fabric) {
    return makeError(std::move(fabric.error()));
  }
  auto arena = cxl::CxlBufferArena::create(*fabric);
  if (!arena) {
    (void)(*fabric)->stopAndJoin();
    return makeError(std::move(arena.error()));
  }
  processFabric = std::move(*fabric);
  processBufferArena = std::move(*arena);
  processRoutes.reset();
  return Void{};
}

Result<Void> TransportRuntime::startCxl(cxl::CxlFabric::Config config,
                                        Address localAddress,
                                        std::vector<CxlRoute> routes,
                                        std::vector<cxl::EndpointId> activeEndpoints) {
  auto routeTable = CxlRouteTable::create(localAddress, std::move(routes), config.manifest.endpointCount);
  if (!routeTable) {
    return makeError(std::move(routeTable.error()));
  }

  std::lock_guard lock(runtimeMutex);
  if (processFabric) {
    return makeError(StatusCode::kQueueConflict, "the process CXL transport runtime is already initialized");
  }
  auto fabric = cxl::CxlFabric::start(std::move(config), std::move(activeEndpoints));
  if (!fabric) {
    return makeError(std::move(fabric.error()));
  }
  auto arena = cxl::CxlBufferArena::create(*fabric);
  if (!arena) {
    (void)(*fabric)->stopAndJoin();
    return makeError(std::move(arena.error()));
  }
  processFabric = std::move(*fabric);
  processBufferArena = std::move(*arena);
  processRoutes.emplace(std::move(*routeTable));
  return Void{};
}

Result<Void> TransportRuntime::stopCxl() {
  std::shared_ptr<cxl::CxlFabric> fabric;
  {
    std::lock_guard lock(runtimeMutex);
    fabric = std::exchange(processFabric, nullptr);
    processBufferArena.reset();
    processRoutes.reset();
  }
  if (!fabric) {
    std::lock_guard lock(runtimeMutex);
    quarantinedLifetimes.clear();
    return Void{};
  }
  if (RpcTrace::enabled()) rpcTrace(0, 0, 0, "runtime_stop_begin", 0,
      fmt::format("endpoint={} generation={} references={}", fabric->config().endpoint.value,
                  fabric->config().endpointGeneration, fabric.use_count()));
  auto stopped = fabric->stopAndJoin();
  if (RpcTrace::enabled()) rpcTrace(0, 0, 0, "runtime_stop_end", stopped ? 0 : stopped.error().code());
  const auto &identity = fabric->config();
  std::string manifestHash;
  for (const auto byte : identity.manifest.manifestSha256) {
    manifestHash += fmt::format("{:02x}", std::to_integer<unsigned>(byte));
  }
  const auto evidence = fmt::format(
      "HF3FS_CXL_TRANSPORT_COUNTERS {{\"schema\":\"hf3fs.transport-counters.v1\",\"pid\":{},"
      "\"session\":{},\"endpoint\":{},\"generation\":{},\"scope\":\"process-lifetime-receive\","
      "\"manifest_sha256\":\"{}\",\"clean_shutdown\":{},\"counters\":{}}}\n",
      ::getpid(), identity.manifest.sessionGeneration, identity.endpoint.value,
      identity.endpointGeneration, manifestHash, stopped.hasValue(), TransportEvidence::process().json());
  std::fwrite(evidence.data(), 1, evidence.size(), stdout);
  std::fflush(stdout);
  {
    std::lock_guard lock(runtimeMutex);
    quarantinedLifetimes.clear();
  }
  return stopped;
}

std::shared_ptr<cxl::CxlFabric> TransportRuntime::cxlFabric() {
  std::lock_guard lock(runtimeMutex);
  return processFabric;
}

std::shared_ptr<cxl::CxlBufferArena> TransportRuntime::cxlBufferArena() {
  std::lock_guard lock(runtimeMutex);
  return processBufferArena;
}

Result<CxlConnectionContext> TransportRuntime::cxlConnection(ServiceEndpoint endpoint) {
  std::lock_guard lock(runtimeMutex);
  if (!processFabric || !processFabric->running()) {
    return makeError(RPCCode::kDataPlaneNotInitialized, "CXL transport runtime is not initialized");
  }
  if (!processRoutes) {
    return makeError(StatusCode::kInvalidConfig, "CXL transport runtime has no route table");
  }
  auto target = processRoutes->resolve(endpoint);
  if (!target) {
    return makeError(std::move(target.error()));
  }
  return CxlConnectionContext{processFabric, processRoutes->localAddress(), *target};
}

Result<ServiceEndpoint> TransportRuntime::cxlServiceEndpoint(Address address) {
  std::lock_guard lock(runtimeMutex);
  if (!processFabric || !processFabric->running()) {
    return makeError(RPCCode::kDataPlaneNotInitialized, "CXL transport runtime is not initialized");
  }
  if (!processRoutes) {
    return makeError(StatusCode::kInvalidConfig, "CXL transport runtime has no route table");
  }
  return processRoutes->resolve(address);
}

void TransportRuntime::quarantineCxlLifetime(std::shared_ptr<void> lifetime) {
  if (!lifetime) {
    return;
  }
  std::lock_guard lock(runtimeMutex);
  quarantinedLifetimes.push_back(std::move(lifetime));
}

}  // namespace hf3fs::net
