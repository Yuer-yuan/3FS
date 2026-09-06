#include <atomic>
#include <gtest/gtest.h>
#include <memory>
#include <thread>
#include <vector>

#include "common/utils/AtomicSharedPtr.h"

namespace hf3fs::tests {
namespace {

TEST(AtomicSharedPtr, ConcurrentUpdatesPublishCompleteObjects) {
  struct Value {
    int generation;
    int checksum;
    explicit Value(int value)
        : generation(value),
          checksum(value ^ 0x5555) {}
  };
  AtomicSharedPtr<const Value> current(std::make_shared<const Value>(0));
  std::atomic<bool> corrupt = false;
  std::vector<std::thread> workers;
  for (int thread = 0; thread < 4; ++thread) {
    workers.emplace_back([&] {
      for (int update = 0; update < 1000; ++update) {
        auto previous = current.load();
        for (;;) {
          if (previous->checksum != (previous->generation ^ 0x5555)) {
            corrupt.store(true);
          }
          auto next = std::make_shared<const Value>(previous->generation + 1);
          if (current.compare_exchange_weak(previous, std::move(next))) {
            break;
          }
        }
      }
    });
  }
  for (auto &worker : workers) {
    worker.join();
  }
  EXPECT_FALSE(corrupt.load());
  EXPECT_EQ(current.load()->generation, 4000);
}

TEST(AtomicSharedPtr, ReadersRetainReplacedObject) {
  AtomicSharedPtr<const int> current(std::make_shared<const int>(7));
  auto reader = current.load();
  std::weak_ptr<const int> retired = reader;
  auto replaced = current.exchange(std::make_shared<const int>(8));
  EXPECT_EQ(replaced, reader);
  replaced.reset();
  EXPECT_FALSE(retired.expired());
  EXPECT_EQ(*reader, 7);
  reader.reset();
  EXPECT_TRUE(retired.expired());
  EXPECT_EQ(*current.load(), 8);
}

}  // namespace
}  // namespace hf3fs::tests
