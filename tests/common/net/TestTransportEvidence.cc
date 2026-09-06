#include <gtest/gtest.h>

#include "common/net/TransportEvidence.h"

namespace hf3fs::net::test {

TEST(TestTransportEvidence, ServingOnControlIsNotMisclassifiedAsBootstrap) {
  TransportEvidence evidence;
  evidence.receive(TransportKind::TCP, ServicePlane::Control, 150);
  evidence.rpc(TransportKind::TCP, ServicePlane::Control, 10001, true, 30);
  evidence.rpc(TransportKind::TCP, ServicePlane::Control, 12, false, 40);
  evidence.rpc(TransportKind::TCP, ServicePlane::Control, 10003, true, 80);
  auto result = evidence.snapshot();
  EXPECT_EQ(result[TransportEvidence::TcpReceiveBytes], 150);
  EXPECT_EQ(result[TransportEvidence::CoreTcpBytes], 30);
  EXPECT_EQ(result[TransportEvidence::BootstrapControlBytes], 40);
  EXPECT_EQ(result[TransportEvidence::BootstrapServingBytes], 80);
  EXPECT_EQ(result[TransportEvidence::TcpDataPlaneBytes], 0);

  // Even a bootstrap service ID cannot disguise a TCP Data-plane request.
  evidence.receive(TransportKind::TCP, ServicePlane::Data, 20);
  evidence.rpc(TransportKind::TCP, ServicePlane::Data, 12, true, 20);
  result = evidence.snapshot();
  EXPECT_EQ(result[TransportEvidence::TcpDataPlaneBytes], 20);
  EXPECT_EQ(result[TransportEvidence::BootstrapServingBytes], 100);
}

TEST(TestTransportEvidence, CxlAndIncompleteTcpTrafficRemainSeparate) {
  TransportEvidence evidence;
  evidence.receive(TransportKind::CXL, ServicePlane::Data, 900);
  evidence.rpc(TransportKind::CXL, ServicePlane::Data, 10003, true, 400);
  evidence.rpc(TransportKind::CXL, ServicePlane::Data, 10003, false, 500);
  // No valid RPC was decoded from these bytes. They must not vanish.
  evidence.receive(TransportKind::TCP, ServicePlane::Control, 7);
  const auto result = evidence.snapshot();
  EXPECT_EQ(result[TransportEvidence::CxlRpcRequests], 1);
  EXPECT_EQ(result[TransportEvidence::CxlRpcResponses], 1);
  EXPECT_EQ(result[TransportEvidence::CxlRpcBytes], 900);
  EXPECT_EQ(result[TransportEvidence::TcpReceiveBytes], 7);
  EXPECT_EQ(result[TransportEvidence::BootstrapControlBytes], 0);
  EXPECT_EQ(result[TransportEvidence::CoreTcpBytes], 0);
}

}  // namespace hf3fs::net::test
