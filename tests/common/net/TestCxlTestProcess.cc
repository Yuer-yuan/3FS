#include <array>
#include <chrono>
#include <filesystem>
#include <gtest/gtest.h>
#include <iostream>
#include <stdexcept>
#include <unistd.h>

#include "tests/lib/CxlTestProcess.h"

namespace hf3fs::test {
namespace {

using namespace std::chrono_literals;

class TestCxlTestProcess : public ::testing::Test {
 protected:
  void SetUp() override {
    std::string pattern = (std::filesystem::temp_directory_path() / "hf3fs-cxl-process-test-XXXXXX").string();
    ASSERT_NE(::mkdtemp(pattern.data()), nullptr);
    directory_ = pattern;
    std::array<char, 4096> executable{};
    ASSERT_GT(::readlink("/proc/self/exe", executable.data(), executable.size() - 1U), 0);
    helper_ = std::filesystem::path(executable.data()).parent_path() / "cxl_test_process_helper";
  }

  void TearDown() override {
    // Keep even expected failure logs for inspection of timeout/cleanup paths.
    std::cout << "CXL process fixture evidence: " << directory_ << '\n';
  }

  auto launch(const std::string &mode, std::chrono::milliseconds timeout = 5s) {
    return CxlTestProcess::launch(helper_, {mode}, directory_ / (mode + ".log"), timeout);
  }

  std::filesystem::path directory_;
  std::filesystem::path helper_;
};

TEST_F(TestCxlTestProcess, LargeFrameAndCleanShutdown) {
  auto child = launch("echo");
  std::string payload(512 * 1024, '\0');
  for (size_t i = 0; i < payload.size(); ++i) payload[i] = static_cast<char>(i % 251);
  ASSERT_EQ(child->request(payload), payload);
  ASSERT_EQ(child->request("stop"), "stopped");
  ASSERT_EQ(child->wait(), 0);
  ASSERT_FALSE(child->forcedCleanup());
}

TEST_F(TestCxlTestProcess, FragmentedHeaderAndPayload) {
  auto child = launch("fragmented");
  ASSERT_EQ(child->request("request"), "fragmented-reply");
  ASSERT_EQ(child->request("stop"), "stopped");
  ASSERT_EQ(child->wait(), 0);
  ASSERT_FALSE(child->forcedCleanup());
}

TEST_F(TestCxlTestProcess, SnapshotCallbackCompletesBeforeFinalReply) {
  auto child = launch("callback");
  bool called = false;
  const auto result = child->requestWithCallback("edit", [&](std::string_view snapshot) {
    EXPECT_EQ(snapshot, "before");
    called = true;
    return std::string("after");
  });
  ASSERT_TRUE(called);
  ASSERT_EQ(result, "committed");
  ASSERT_EQ(child->request("stop"), "stopped");
  ASSERT_EQ(child->wait(), 0);
}

TEST_F(TestCxlTestProcess, FailedExecAndFailedStartup) {
  EXPECT_THROW(CxlTestProcess::launch(directory_ / "missing", {}, directory_ / "missing.log"), std::runtime_error);
  EXPECT_THROW(launch("fail-start"), std::runtime_error);
}

TEST_F(TestCxlTestProcess, UnexpectedExitReportsStatus) {
  auto child = launch("exit-on-request");
  EXPECT_THROW(child->request("request"), std::runtime_error);
  EXPECT_EQ(child->wait(), 23);
  EXPECT_FALSE(child->forcedCleanup());
  EXPECT_THROW(child->request("stop"), std::runtime_error);
}

TEST_F(TestCxlTestProcess, OversizedReplyPoisonsChannel) {
  auto child = launch("bad-frame");
  EXPECT_THROW(child->request("request"), std::runtime_error);
  EXPECT_THROW(child->request("request"), std::runtime_error);
  EXPECT_EQ(child->wait(), 0);
}

TEST_F(TestCxlTestProcess, TimeoutForbidsQueuedCommandsAndMarksForcedCleanup) {
  auto child = launch("stall", 200ms);
  const auto begin = std::chrono::steady_clock::now();
  EXPECT_THROW(child->request("request"), std::runtime_error);
  EXPECT_LT(std::chrono::steady_clock::now() - begin, 2s);
  const auto poisoned = std::chrono::steady_clock::now();
  EXPECT_THROW(child->request("stop"), std::runtime_error);
  EXPECT_LT(std::chrono::steady_clock::now() - poisoned, 100ms);
  EXPECT_THROW(child->wait(10ms), std::runtime_error);
  EXPECT_TRUE(child->forcedCleanup());
  EXPECT_LT(child->pid(), 0);
}

}  // namespace
}  // namespace hf3fs::test
