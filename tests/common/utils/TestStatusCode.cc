#include "common/utils/StatusCode.h"
#include "gtest/gtest.h"

namespace hf3fs {
namespace {

TEST(StatusCode, toString) {
  ASSERT_EQ(StatusCode::toString(StatusCode::kOK), "OK");
  ASSERT_EQ(StatusCode::toString(TransactionCode::kConflict), "Transaction::Conflict");
  ASSERT_EQ(StatusCode::toString(-1), "UnknownStatusCode");
}

TEST(StatusCode, TransportNeutralAliasesPreserveWireABI) {
  static_assert(RPCCode::kDataPlaneInitFailed == 2015);
  static_assert(RPCCode::kDataPlaneInterfaceNotFound == 2016);
  static_assert(RPCCode::kBulkPostFailed == 2017);
  static_assert(RPCCode::kBulkTransferError == 2018);
  static_assert(RPCCode::kNoSharedBuffer == 2019);
  static_assert(RPCCode::kDataPlaneNotInitialized == 2021);
  static_assert(RPCCode::kDataPlaneOpenFailed == 2027);
  static_assert(StorageClientCode::kNoDataPlaneInterface == 7016);
  static_assert(RPCCode::kDataPlaneHandshakeFailed == 2028);
  static_assert(RPCCode::kTransportCapabilityMissing == 2029);
  static_assert(RPCCode::kRemoteBufferAccessDenied == 2030);
  static_assert(RPCCode::kStaleGeneration == 2031);

  EXPECT_EQ(StatusCode::toString(RPCCode::kDataPlaneInitFailed), "RPC::IBInitFailed");
  EXPECT_EQ(StatusCode::toString(StorageClientCode::kNoDataPlaneInterface), "StorageClient::NoRDMAInterface");
}

}  // namespace
}  // namespace hf3fs
