#include "common/net/BulkControl.h"
#include "common/net/Client.h"
#include "common/net/RequestOptions.h"
#include "common/net/Server.h"
#include "common/serde/ClientContext.h"
#include "tests/GtestHelpers.h"
#if HF3FS_ENABLE_RDMA
#include "tests/common/net/ib/SetupIB.h"
#else
#include "tests/common/net/RunCxlTransportFixture.h"
#endif

namespace hf3fs::net::test {
namespace {

struct BulkControlService : serde::ServiceWrapper<BulkControlService, BulkControl> {
  CoTryTask<BulkTransmissionRsp> apply(serde::CallContext &ctx, const BulkTransmissionReq &req) {
    auto &tr = ctx.transport();
    if (tr->kind() != TransportKind::RDMA && tr->kind() != TransportKind::CXL) {
      co_return makeError(StatusCode::kInvalidArg);
    }
    serde::ClientContext clientCtx(tr);
    co_return co_await BulkControl<>::apply(clientCtx, req);
  }
};

TEST(TestBulkControl, Normal) {
#if HF3FS_ENABLE_RDMA
  SetupIB::SetUpTestSuite();

  net::Server::Config serverConfig;
  net::Server server{serverConfig};
  ASSERT_OK(server.setup());
  ASSERT_OK(server.start());
  ASSERT_OK(server.addSerdeService(std::make_unique<BulkControlService>()));

  net::Client::Config clientConfig;
  net::Client client{clientConfig};
  ASSERT_OK(client.start());
  auto ctx = client.serdeCtx(server.groups().front()->addressList().front());

  folly::coro::blockingWait(folly::coro::co_invoke([&]() -> CoTask<void> {
    BulkTransmissionReq req;
    auto result = co_await BulkControl<>::apply(ctx, req);
    if (result.hasError()) {
      std::cout << result.error().describe() << std::endl;
    }
    CO_ASSERT_OK(result);
  }));
#else
  ASSERT_EQ(runCxlTransportFixture("orchestrate-bulk-control"), 0);
#endif
}

}  // namespace
}  // namespace hf3fs::net::test
