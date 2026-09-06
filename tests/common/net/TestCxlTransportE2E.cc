#include <gtest/gtest.h>

#include "tests/common/net/RunCxlTransportFixture.h"

namespace hf3fs::net::test {
namespace {

TEST(TestCxlTransportE2E, TransportPoolBootstrapAndEchoUseCxlLane) {
  ASSERT_EQ(runCxlTransportFixture("orchestrate"), 0);
}

}  // namespace
}  // namespace hf3fs::net::test
