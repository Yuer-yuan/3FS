#include <folly/FBString.h>
#include <folly/memory/Malloc.h>
#include <gtest/gtest.h>

namespace hf3fs::tests {
namespace {

struct AllocatorUnavailable {
  bool operator()() const { return false; }
};

TEST(FollyAllocator, CachedUnavailableAllocatorRemainsUnavailable) {
  // Folly caches false as -1. A consumer compiled with unsigned char used to
  // reinterpret that cache as true and call a null allocator function on RV64.
  using Available = folly::detail::FastStaticBool<AllocatorUnavailable>;
  EXPECT_FALSE(Available::get());
  for (int attempt = 0; attempt < 8; ++attempt) {
    EXPECT_FALSE(Available::get());
  }
}

TEST(FollyAllocator, StringGrowthPreservesBytes) {
  const auto jemalloc = folly::usingJEMalloc();
  for (int attempt = 0; attempt < 8; ++attempt) {
    EXPECT_EQ(folly::usingJEMalloc(), jemalloc);
    folly::fbstring value;
    for (int byte = 0; byte < 4096; ++byte) {
      value.push_back(static_cast<char>(byte % 127));
    }
    ASSERT_EQ(value.size(), 4096);
    for (int byte = 0; byte < 4096; ++byte) {
      EXPECT_EQ(value[byte], static_cast<char>(byte % 127));
    }
  }
}

}  // namespace
}  // namespace hf3fs::tests
