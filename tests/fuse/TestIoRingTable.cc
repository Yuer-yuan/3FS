#include <atomic>
#include <cerrno>
#include <cstdarg>
#include <gtest/gtest.h>
#include <semaphore.h>
#include <sys/stat.h>

#include "fuse/IoRing.h"

namespace {
std::atomic<int> failAfter{-1};
}

extern "C" sem_t *__real_sem_open(const char *name, int flags, ...);
extern "C" sem_t *__wrap_sem_open(const char *name, int flags, ...) {
  if (failAfter.load() >= 0 && failAfter.fetch_sub(1) == 0) {
    errno = ENOENT;
    return SEM_FAILED;
  }
  if (!(flags & O_CREAT)) {
    return __real_sem_open(name, flags);
  }
  va_list arguments;
  va_start(arguments, flags);
  const auto mode = va_arg(arguments, mode_t);
  const auto value = va_arg(arguments, unsigned int);
  va_end(arguments);
  return __real_sem_open(name, flags, mode, value);
}

namespace hf3fs::fuse::test {

TEST(IoRingTable, SemaphoreFailureLeavesNoPartialTable) {
  for (int successful = 0; successful < 3; ++successful) {
    IoRingTable table;
    failAfter = successful;
    const auto result = table.init(8);
    failAfter = -1;
    ASSERT_FALSE(result);
    EXPECT_NE(result.error().describe().find("sem_open"), std::string::npos);
    EXPECT_TRUE(table.sems.empty());
    EXPECT_EQ(table.ioRings, nullptr);
    for (int prio = 0; prio < successful; ++prio) {
      struct stat info {};
      EXPECT_EQ(stat(IoRingTable::semPath(prio).c_str(), &info), -1);
      EXPECT_EQ(errno, ENOENT);
    }
  }
}

TEST(IoRingTable, SuccessfulInitializationCreatesUsableSemaphores) {
  IoRingTable table;
  ASSERT_TRUE(table.init(8));
  ASSERT_EQ(table.sems.size(), 3);
  ASSERT_NE(table.ioRings, nullptr);
  for (const auto &semaphore : table.sems) {
    ASSERT_NE(semaphore.get(), SEM_FAILED);
    EXPECT_EQ(sem_post(semaphore.get()), 0);
    EXPECT_EQ(sem_trywait(semaphore.get()), 0);
  }
  EXPECT_FALSE(table.init(8));
}

}  // namespace hf3fs::fuse::test
