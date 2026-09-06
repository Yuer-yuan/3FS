#include <gtest/gtest.h>

#include "common/net/PublicationLedger.h"
#include "tests/GtestHelpers.h"

namespace hf3fs::net::test {
namespace {

WriteItemPtr request(size_t uuid, PublicationRange range) {
  auto item = WriteItem::Pool::get();
  item->uuid = uuid;
  item->publication = range;
  return item;
}

WriteItemPtr request(size_t uuid, PublicationRange range, std::shared_ptr<void> lifetime) {
  auto item = request(uuid, range);
  item->requestLifetime = std::move(lifetime);
  return item;
}

PublicationSnapshot snapshot(uint64_t generation,
                             uint64_t accepted,
                             uint64_t published,
                             uint64_t delivered,
                             bool trustworthy = true) {
  return PublicationSnapshot{generation, accepted, published, delivered, trustworthy};
}

TEST(TestPublicationLedger, SeparatesRequestsCoalescedIntoOneCellByByteRange) {
  PublicationLedger ledger(7);
  auto first = ledger.reserve(100);
  auto second = ledger.reserve(100);
  ASSERT_OK(first);
  ASSERT_OK(second);
  ASSERT_OK(ledger.retain(request(11, *first)));
  ASSERT_OK(ledger.retain(request(12, *second)));

  auto retired = ledger.retire(snapshot(7, 200, 200, 50));
  ASSERT_EQ(retired.size(), 2);
  EXPECT_EQ(retired[0].uuid, 11);
  EXPECT_EQ(retired[0].disposition, CompletionDisposition::OutcomeUnknown);
  EXPECT_FALSE(retired[0].retryable);
  EXPECT_EQ(retired[1].uuid, 12);
  EXPECT_EQ(retired[1].disposition, CompletionDisposition::RejectedBeforeExecute);
  EXPECT_TRUE(retired[1].retryable);
}

TEST(TestPublicationLedger, RequestCrossingCellsIsUnknownAfterPartialDelivery) {
  PublicationLedger ledger(8);
  auto range = ledger.reserve(130_KB);
  ASSERT_OK(range);
  ASSERT_OK(ledger.retain(request(21, *range)));
  auto retired = ledger.retire(snapshot(8, 130_KB, 128_KB, 64_KB));
  ASSERT_EQ(retired.size(), 1);
  EXPECT_EQ(retired[0].disposition, CompletionDisposition::OutcomeUnknown);
  EXPECT_FALSE(retired[0].retryable);
}

TEST(TestPublicationLedger, AcceptedButUnflushedRequestRemainsRetryable) {
  PublicationLedger ledger(9);
  auto range = ledger.reserve(512);
  ASSERT_OK(range);
  ASSERT_OK(ledger.retain(request(31, *range)));
  auto retired = ledger.retire(snapshot(9, 512, 0, 0));
  ASSERT_EQ(retired.size(), 1);
  EXPECT_EQ(retired[0].disposition, CompletionDisposition::RejectedBeforeExecute);
  EXPECT_TRUE(retired[0].retryable);
}

TEST(TestPublicationLedger, DeliveryReleasesBufferButKeepsUnknownMetadataUntilResponse) {
  PublicationLedger ledger(10);
  auto range = ledger.reserve(256);
  ASSERT_OK(range);
  ASSERT_OK(ledger.retain(request(41, *range)));
  EXPECT_EQ(ledger.retainedBufferCount(), 1);
  ASSERT_OK(ledger.observe(snapshot(10, 256, 256, 256)));
  EXPECT_EQ(ledger.retainedBufferCount(), 0);
  auto retired = ledger.retire(snapshot(10, 256, 256, 256));
  ASSERT_EQ(retired.size(), 1);
  EXPECT_EQ(retired[0].disposition, CompletionDisposition::OutcomeUnknown);
  EXPECT_FALSE(retired[0].retryable);
}

TEST(TestPublicationLedger, DeliveryReleasesMessageButPinsExportUntilCompletion) {
  PublicationLedger ledger(10);
  auto range = ledger.reserve(256);
  ASSERT_OK(range);
  auto lifetime = std::make_shared<int>(42);
  std::weak_ptr<int> observer = lifetime;
  ASSERT_OK(ledger.retain(request(42, *range, std::move(lifetime))));
  ASSERT_OK(ledger.observe(snapshot(10, 256, 256, 256)));
  EXPECT_EQ(ledger.retainedBufferCount(), 0);
  EXPECT_FALSE(observer.expired());
  ASSERT_OK(ledger.complete(42));
  EXPECT_TRUE(observer.expired());
}

TEST(TestPublicationLedger, UnknownOutcomeReturnsExportLifetimeForSessionQuarantine) {
  PublicationLedger ledger(10);
  auto range = ledger.reserve(256);
  ASSERT_OK(range);
  auto lifetime = std::make_shared<int>(42);
  std::weak_ptr<int> observer = lifetime;
  ASSERT_OK(ledger.retain(request(43, *range, std::move(lifetime))));
  auto retired = ledger.retire(snapshot(10, 256, 256, 1));
  ASSERT_EQ(retired.size(), 1);
  EXPECT_EQ(retired.front().disposition, CompletionDisposition::OutcomeUnknown);
  EXPECT_TRUE(retired.front().requestLifetime);
  EXPECT_FALSE(observer.expired());
  retired.clear();
  EXPECT_TRUE(observer.expired());
}

TEST(TestPublicationLedger, FinalResponseWinsRaceWithDeliveredWatermark) {
  PublicationLedger ledger(11);
  auto range = ledger.reserve(128);
  ASSERT_OK(range);
  ASSERT_OK(ledger.retain(request(51, *range)));
  auto completed = ledger.complete(51);
  ASSERT_OK(completed);
  EXPECT_EQ(completed->disposition, CompletionDisposition::Completed);
  EXPECT_TRUE(ledger.retire(snapshot(11, 128, 128, 0)).empty());
}

TEST(TestPublicationLedger, CorruptOrWrongGenerationSnapshotNeverAutoRetries) {
  for (const auto invalid : {snapshot(12, 64, 64, 0, false), snapshot(13, 64, 64, 0)}) {
    PublicationLedger ledger(12);
    auto range = ledger.reserve(64);
    ASSERT_OK(range);
    ASSERT_OK(ledger.retain(request(61, *range)));
    auto retired = ledger.retire(invalid);
    ASSERT_EQ(retired.size(), 1);
    EXPECT_EQ(retired[0].disposition, CompletionDisposition::LaneRetired);
    EXPECT_FALSE(retired[0].retryable);
  }
}

}  // namespace
}  // namespace hf3fs::net::test
