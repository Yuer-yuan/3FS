#pragma once

#include <folly/logging/xlog.h>
#include <gtest/gtest.h>

#if HF3FS_ENABLE_RDMA
#include "common/net/ib/IBDevice.h"
#endif
#include "common/utils/ConfigBase.h"
#include "tests/GtestHelpers.h"

namespace hf3fs::net::test {

class SetupIB : public ::testing::Test {
 public:
  static void SetUpTestSuite() {
#if HF3FS_ENABLE_RDMA
    static IBConfig config;
    auto ib = IBManager::start(config);
    XLOGF_IF(FATAL, ib.hasError(), "IBManager start failed, result {}", ib.error());
    ASSERT_FALSE(IBDevice::all().empty());
#endif
  }
};

}  // namespace hf3fs::net::test
