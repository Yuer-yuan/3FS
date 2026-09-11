#pragma once

#include <atomic>
#include <cstdint>

namespace hf3fs::net::cxl {

struct CxlMetricsSnapshot {
  uint64_t progressSpinNanoseconds{};
  uint64_t progressYields{};
  uint64_t progressSleeps{};
  uint64_t eventfdWakeups{};
  uint64_t acceptedBytes{};
  uint64_t publishedBytes{};
  uint64_t deliveredBytes{};
  uint64_t corruptPublications{};
  uint64_t pendingPublications{};
  uint64_t retiredLanes{};
};

class CxlMetrics {
 public:
  void addProgressSpinNanoseconds(uint64_t value) noexcept;
  void addProgressYield() noexcept;
  void addProgressSleep() noexcept;
  void addEventfdWakeup() noexcept;
  void addAcceptedBytes(uint64_t value) noexcept;
  void addPublishedBytes(uint64_t value) noexcept;
  void addDeliveredBytes(uint64_t value) noexcept;
  void addCorruptPublication() noexcept;
  void addPendingPublication() noexcept;
  void addRetiredLane() noexcept;

  CxlMetricsSnapshot snapshot() const noexcept;

 private:
  std::atomic<uint64_t> progressSpinNanoseconds_{};
  std::atomic<uint64_t> progressYields_{};
  std::atomic<uint64_t> progressSleeps_{};
  std::atomic<uint64_t> eventfdWakeups_{};
  std::atomic<uint64_t> acceptedBytes_{};
  std::atomic<uint64_t> publishedBytes_{};
  std::atomic<uint64_t> deliveredBytes_{};
  std::atomic<uint64_t> corruptPublications_{};
  std::atomic<uint64_t> pendingPublications_{};
  std::atomic<uint64_t> retiredLanes_{};
};

}  // namespace hf3fs::net::cxl
