#include "common/net/cxl/CxlMetrics.h"

namespace hf3fs::net::cxl {

void CxlMetrics::addProgressSpinNanoseconds(uint64_t value) noexcept {
  progressSpinNanoseconds_.fetch_add(value, std::memory_order_relaxed);
}
void CxlMetrics::addProgressYield() noexcept { progressYields_.fetch_add(1, std::memory_order_relaxed); }
void CxlMetrics::addProgressSleep() noexcept { progressSleeps_.fetch_add(1, std::memory_order_relaxed); }
void CxlMetrics::addEventfdWakeup() noexcept { eventfdWakeups_.fetch_add(1, std::memory_order_relaxed); }
void CxlMetrics::addAcceptedBytes(uint64_t value) noexcept {
  acceptedBytes_.fetch_add(value, std::memory_order_relaxed);
}
void CxlMetrics::addPublishedBytes(uint64_t value) noexcept {
  publishedBytes_.fetch_add(value, std::memory_order_relaxed);
}
void CxlMetrics::addDeliveredBytes(uint64_t value) noexcept {
  deliveredBytes_.fetch_add(value, std::memory_order_relaxed);
}
void CxlMetrics::addCorruptPublication() noexcept { corruptPublications_.fetch_add(1, std::memory_order_relaxed); }
void CxlMetrics::addPendingPublication() noexcept { pendingPublications_.fetch_add(1, std::memory_order_relaxed); }
void CxlMetrics::addRetiredLane() noexcept { retiredLanes_.fetch_add(1, std::memory_order_relaxed); }

CxlMetricsSnapshot CxlMetrics::snapshot() const noexcept {
  return CxlMetricsSnapshot{
      .progressSpinNanoseconds = progressSpinNanoseconds_.load(std::memory_order_relaxed),
      .progressYields = progressYields_.load(std::memory_order_relaxed),
      .progressSleeps = progressSleeps_.load(std::memory_order_relaxed),
      .eventfdWakeups = eventfdWakeups_.load(std::memory_order_relaxed),
      .acceptedBytes = acceptedBytes_.load(std::memory_order_relaxed),
      .publishedBytes = publishedBytes_.load(std::memory_order_relaxed),
      .deliveredBytes = deliveredBytes_.load(std::memory_order_relaxed),
      .corruptPublications = corruptPublications_.load(std::memory_order_relaxed),
      .pendingPublications = pendingPublications_.load(std::memory_order_relaxed),
      .retiredLanes = retiredLanes_.load(std::memory_order_relaxed),
  };
}

}  // namespace hf3fs::net::cxl
