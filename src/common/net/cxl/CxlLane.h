#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <span>
#include <string_view>
#include <utility>

#include "common/net/cxl/CxlAbi.h"
#include "common/net/cxl/CxlLayout.h"
#include "common/utils/FriendTest.h"
#include "common/utils/Result.h"

namespace hf3fs::net::cxl {

enum class Direction : uint8_t {
  Submission = 0,
  Completion = 1,
};

enum class CxlLaneRole : uint8_t {
  Requester,
  Acceptor,
};

struct FrameView {
  std::span<const std::byte> payload;
  CxlFrameFlags flags{CxlFrameFlags::Data};
};

struct CxlLaneDirectionLayout {
  uint64_t producerCursorOffset{};
  uint64_t consumerCursorOffset{};
  uint64_t deliveredRecordOffset{};
  uint64_t frameRingOffset{};
  uint64_t payloadCellsOffset{};
};

struct CxlLaneConfig {
  uint64_t sessionGeneration{};
  uint64_t laneGeneration{};
  uint32_t depth{64};
  uint32_t cellBytes{64U * 1024U};
  std::array<CxlLaneDirectionLayout, 2> directions{};
};

class CxlLane {
 public:
  CxlLane(CxlLane &&other) noexcept;
  CxlLane &operator=(CxlLane &&other) noexcept;
  CxlLane(const CxlLane &) = delete;
  CxlLane &operator=(const CxlLane &) = delete;

  static Result<CxlLane> create(CxlRegion &region,
                                const CxlLayout &layout,
                                const CxlLaneConfig &config,
                                CxlLaneRole role);

  Result<size_t> tryPush(Direction direction, FrameView frame);
  Result<size_t> tryPop(Direction direction, std::span<std::byte> destination);

  Result<bool> readable(Direction direction);
  Result<bool> writable(Direction direction);
  Result<uint64_t> peerDeliveredOffset(Direction direction);
  Result<bool> observeReadable(Direction direction) const;
  Result<bool> observeWritable(Direction direction) const;
  Result<uint64_t> observePeerDeliveredOffset(Direction direction, uint64_t publishedOffset) const;

  void retire(Status status);
  bool isRetired() const noexcept { return retirementCode_.load(std::memory_order_acquire) != StatusCode::kOK; }
  std::optional<Status> retirementStatus() const;

  uint64_t producerCursor(Direction direction) const;
  uint64_t consumerCursor(Direction direction) const;
  uint64_t nextStreamOffset(Direction direction) const;

  const CxlLaneConfig &config() const noexcept { return config_; }
  CxlLaneRole role() const noexcept { return role_; }

 private:
  struct PendingRead {
    bool active{};
    uint64_t sequence{};
    uint64_t streamOffset{};
    uint32_t payloadLength{};
    uint32_t consumed{};
    std::span<const std::byte> payload;
  };

  struct DirectionState {
    uint64_t *producerCursor{};
    uint64_t *consumerCursor{};
    std::span<std::byte> deliveredRecord;
    std::span<std::byte> frameRing;
    std::span<std::byte> payloadCells;
    uint64_t nextPushStreamOffset{};
    uint64_t nextPopStreamOffset{};
    uint64_t deliveredSequence{};
    PendingRead pending;
  };

  CxlLane(CxlLaneConfig config, CxlLaneRole role, std::array<DirectionState, 2> directions)
      : config_(std::move(config)),
        role_(role),
        directions_(std::move(directions)) {}

  static constexpr size_t index(Direction direction) { return static_cast<size_t>(direction); }
  bool mayPush(Direction direction) const noexcept;
  bool mayPop(Direction direction) const noexcept;
  Result<Void> validateCursors(const DirectionState &state) const;
  Result<Void> publishDelivered(DirectionState &state, uint64_t offset);
  Result<size_t> fail(status_code_t code, std::string_view message);

  CxlLaneConfig config_;
  CxlLaneRole role_;
  std::array<DirectionState, 2> directions_;
  std::atomic<status_code_t> retirementCode_{StatusCode::kOK};

  FRIEND_TEST(MappedCxlLaneTest, RejectsStreamOffsetOverflow);
};

}  // namespace hf3fs::net::cxl
