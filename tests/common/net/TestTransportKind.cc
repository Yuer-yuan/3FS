#include <cstdint>
#include <gtest/gtest.h>
#include <vector>

#include "common/net/TransportKind.h"
#include "common/net/TransportRuntime.h"
#include "common/utils/Address.h"
#include "common/utils/StatusCode.h"

namespace hf3fs::net::test {
namespace {

Address tcpAddress() { return Address::fromString("TCP://127.0.0.1:8000"); }

TEST(TestTransportKind, AddressMapping) {
  EXPECT_EQ(transportKind(Address::TCP), TransportKind::TCP);
  EXPECT_EQ(transportKind(Address::RDMA), TransportKind::RDMA);
  EXPECT_EQ(transportKind(Address::CXL), TransportKind::CXL);
  EXPECT_NE((ServiceEndpoint{tcpAddress(), ServicePlane::Control}),
            (ServiceEndpoint{tcpAddress(), ServicePlane::Data}));
}

TEST(TestTransportKind, CxlAddressRoundTrip) {
  auto address = Address::fromString("CXL://10.0.0.7:8000");
  EXPECT_EQ(address.type, Address::CXL);
  EXPECT_EQ(Address::fromString(address.str()), address);
  static_assert(sizeof(Address) == sizeof(uint64_t));
}

TEST(TestTransportStatus, NeutralAliasesKeepWireValues) {
  static_assert(RPCCode::kNoSharedBuffer == RPCCode::kRDMANoBuf);
  static_assert(StorageClientCode::kNoDataPlaneInterface == StorageClientCode::kNoRDMAInterface);
  EXPECT_EQ(StatusCode::toString(RPCCode::kNoSharedBuffer), "RPC::RDMANoBuf");
}

TEST(TestCxlRouteTable, ResolvesAddressAndPlaneExactly) {
  const auto local = Address::fromString("CXL://127.0.0.1:9001");
  const auto peer = Address::fromString("CXL://127.0.0.1:9002");
  auto table = CxlRouteTable::create(local,
                                     {
                                         CxlRoute{ServiceEndpoint{peer, ServicePlane::Control}, cxl::EndpointId{2}},
                                         CxlRoute{ServiceEndpoint{peer, ServicePlane::Data}, cxl::EndpointId{3}},
                                     },
                                     4);
  ASSERT_TRUE(table.hasValue()) << (table ? "" : table.error().describe());
  EXPECT_EQ(table->localAddress(), local);
  ASSERT_TRUE(table->resolve(ServiceEndpoint{peer, ServicePlane::Control}).hasValue());
  EXPECT_EQ(table->resolve(ServiceEndpoint{peer, ServicePlane::Control})->value, 2);
  ASSERT_TRUE(table->resolve(ServiceEndpoint{peer, ServicePlane::Data}).hasValue());
  EXPECT_EQ(table->resolve(ServiceEndpoint{peer, ServicePlane::Data})->value, 3);
  auto ambiguous = table->resolve(peer);
  ASSERT_TRUE(ambiguous.hasError());
  EXPECT_EQ(ambiguous.error().code(), StatusCode::kInvalidConfig);
}

TEST(TestCxlRouteTable, RejectsMissingDuplicateNonCxlAndOutOfRangeRoutes) {
  const auto local = Address::fromString("CXL://127.0.0.1:9001");
  const auto peer = Address::fromString("CXL://127.0.0.1:9002");
  const auto data = ServiceEndpoint{peer, ServicePlane::Data};

  auto valid = CxlRouteTable::create(local, {CxlRoute{data, cxl::EndpointId{2}}}, 2);
  ASSERT_TRUE(valid.hasValue()) << (valid ? "" : valid.error().describe());
  auto resolved = valid->resolve(peer);
  ASSERT_TRUE(resolved.hasValue()) << (resolved ? "" : resolved.error().describe());
  EXPECT_EQ(resolved->plane, ServicePlane::Data);
  auto missing = valid->resolve(ServiceEndpoint{peer, ServicePlane::Control});
  ASSERT_TRUE(missing.hasError());
  EXPECT_EQ(missing.error().code(), StatusCode::kInvalidConfig);

  auto duplicate =
      CxlRouteTable::create(local, {CxlRoute{data, cxl::EndpointId{2}}, CxlRoute{data, cxl::EndpointId{2}}}, 2);
  ASSERT_TRUE(duplicate.hasError());
  EXPECT_EQ(duplicate.error().code(), StatusCode::kInvalidConfig);

  auto nonCxl =
      CxlRouteTable::create(local, {CxlRoute{ServiceEndpoint{peer.tcp(), ServicePlane::Data}, cxl::EndpointId{2}}}, 2);
  ASSERT_TRUE(nonCxl.hasError());
  EXPECT_EQ(nonCxl.error().code(), StatusCode::kInvalidConfig);

  auto outOfRange = CxlRouteTable::create(local, {CxlRoute{data, cxl::EndpointId{3}}}, 2);
  ASSERT_TRUE(outOfRange.hasError());
  EXPECT_EQ(outOfRange.error().code(), StatusCode::kInvalidConfig);

  auto badLocal = CxlRouteTable::create(local.tcp(), {CxlRoute{data, cxl::EndpointId{2}}}, 2);
  ASSERT_TRUE(badLocal.hasError());
  EXPECT_EQ(badLocal.error().code(), StatusCode::kInvalidConfig);
}

}  // namespace
}  // namespace hf3fs::net::test
