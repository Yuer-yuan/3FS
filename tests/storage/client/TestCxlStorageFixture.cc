#include "tests/lib/UnitTestFabric.h"

#if HF3FS_ENABLE_CXL && !HF3FS_ENABLE_RDMA
namespace hf3fs::test {
namespace {

class TestCxlStorageFixture : public UnitTestFabric, public ::testing::Test {
 public:
  TestCxlStorageFixture()
      : UnitTestFabric(SystemSetupConfig{128_KB, 1, 1, 1, {folly::fs::temp_directory_path()}}) {}
  void SetUp() override { ASSERT_TRUE(setUpStorageSystem()); }
  void TearDown() override { tearDownStorageSystem(); }
};

TEST_F(TestCxlStorageFixture, WriteReadAndRestartPreservesData) {
  ASSERT_TRUE(storageServers_.front()->address().isCXL());
  std::vector<uint8_t> input(setupConfig_.chunk_size());
  for (size_t i = 0; i < input.size(); ++i) input[i] = (i * 17 + i / 251) & 0xff;
  const storage::ChunkId chunk(1, 1);
  auto written = writeToChunk(firstChainId_, chunk, input);
  ASSERT_OK(written.lengthInfo);
  ASSERT_EQ(*written.lengthInfo, input.size());
  std::vector<uint8_t> output(input.size());
  auto read = readFromChunk(firstChainId_, chunk, output);
  ASSERT_OK(read.lengthInfo);
  ASSERT_EQ(*read.lengthInfo, input.size());
  ASSERT_EQ(input, output);

  ASSERT_TRUE(stopAndRemoveStorageServer(uint32_t{0}));
  auto restarted = createStorageServer(0);
  ASSERT_NE(restarted, nullptr);
  restarted->setFakeRoutingInfo(rawRoutingInfo_);
  restarted->refreshRoutingInfo();
  storageServers_.push_back(std::move(restarted));
  std::fill(output.begin(), output.end(), 0);
  read = readFromChunk(firstChainId_, chunk, output);
  ASSERT_OK(read.lengthInfo);
  ASSERT_EQ(*read.lengthInfo, input.size());
  ASSERT_EQ(input, output);
  // Teardown independently checks endpoint CRC, RETIRED, and generation 2.
}

TEST_F(TestCxlStorageFixture, ShortReadAndEofLeaveDestinationSuffixUntouched) {
  std::vector<uint8_t> input(64_KB + 3);
  for (size_t i = 0; i < input.size(); ++i) input[i] = (i * 13 + i / 127) & 0xff;
  const storage::ChunkId chunk(1, 1);
  auto written = writeToChunk(firstChainId_, chunk, input);
  ASSERT_RESULT_EQ(input.size(), written.lengthInfo);
  std::vector<uint8_t> output(128_KB, 0xa5);
  auto read = readFromChunk(firstChainId_, chunk, output);
  ASSERT_RESULT_EQ(input.size(), read.lengthInfo);
  ASSERT_TRUE(std::equal(input.begin(), input.end(), output.begin()));
  ASSERT_TRUE(std::all_of(output.begin() + input.size(), output.end(), [](uint8_t c) { return c == 0xa5; }));

  output.assign(setupConfig_.chunk_size() - input.size(), 0xa5);
  read = readFromChunk(firstChainId_, chunk, output, input.size());
  ASSERT_RESULT_EQ(0, read.lengthInfo);
  ASSERT_TRUE(std::all_of(output.begin(), output.end(), [](uint8_t c) { return c == 0xa5; }));
}

}  // namespace
}  // namespace hf3fs::test
#endif
