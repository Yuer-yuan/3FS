#include "common/net/cxl/CxlLane.h"

#include <algorithm>
#include <atomic>
#include <cstring>
#include <fmt/format.h>
#include <limits>
#include <set>

namespace hf3fs::net::cxl {
namespace {

constexpr uint64_t kCursorPageBytes = 4096;

Result<uint64_t> checkedMultiply(uint64_t left, uint64_t right, std::string_view label) {
  if (left != 0 && right > std::numeric_limits<uint64_t>::max() / left) {
    return makeError(StatusCode::kInvalidArg, fmt::format("{} size overflows uint64_t", label));
  }
  return left * right;
}

bool rangeContains(const CxlLayout &layout, CxlRangeKind kind, uint64_t offset, uint64_t length) {
  const auto rangeIndex = static_cast<size_t>(kind) - 1U;
  if (rangeIndex >= layout.ranges().size() || length > std::numeric_limits<uint64_t>::max() - offset) {
    return false;
  }
  const auto &range = layout.ranges()[rangeIndex];
  return range.kind == kind && range.length <= std::numeric_limits<uint64_t>::max() - range.offset &&
         offset >= range.offset && offset + length <= range.offset + range.length;
}

bool overlaps(uint64_t firstOffset, uint64_t firstLength, uint64_t secondOffset, uint64_t secondLength) {
  return firstOffset < secondOffset + secondLength && secondOffset < firstOffset + firstLength;
}

uint32_t deliveredCrc(const CxlDeliveredRecord &record) {
  return cxlCrc32cWithZeroedU32(record, offsetof(CxlDeliveredRecord, crc32c));
}

bool validFrameFlags(uint32_t flags) {
  return flags == static_cast<uint32_t>(CxlFrameFlags::Data) || flags == static_cast<uint32_t>(CxlFrameFlags::Error) ||
         flags == static_cast<uint32_t>(CxlFrameFlags::Retire);
}

}  // namespace

CxlLane::CxlLane(CxlLane &&other) noexcept
    : config_(std::move(other.config_)),
      role_(other.role_),
      directions_(std::move(other.directions_)),
      retirementCode_(other.retirementCode_.load(std::memory_order_acquire)) {}

CxlLane &CxlLane::operator=(CxlLane &&other) noexcept {
  if (this != &other) {
    config_ = std::move(other.config_);
    role_ = other.role_;
    directions_ = std::move(other.directions_);
    retirementCode_.store(other.retirementCode_.load(std::memory_order_acquire), std::memory_order_release);
  }
  return *this;
}

Result<CxlLane> CxlLane::create(CxlRegion &region,
                                const CxlLayout &layout,
                                const CxlLaneConfig &config,
                                CxlLaneRole role) {
  if (config.sessionGeneration == 0 || config.sessionGeneration != layout.sessionGeneration() ||
      config.laneGeneration == 0 || config.depth == 0 || config.cellBytes == 0) {
    return makeError(StatusCode::kInvalidArg, "invalid CXL lane identity or geometry");
  }

  auto encodedSuperblock = region.checkedRange(0, sizeof(CxlSuperblock), alignof(CxlSuperblock));
  if (!encodedSuperblock || std::memcmp(encodedSuperblock->data(), &layout.superblock(), sizeof(CxlSuperblock)) != 0) {
    return makeError(RPCCode::kStaleGeneration, "CXL lane region does not match the validated layout");
  }

  auto frameBytes = checkedMultiply(config.depth, sizeof(CxlFrameEntry), "CXL frame ring");
  auto payloadBytes = checkedMultiply(config.depth, config.cellBytes, "CXL payload cells");
  if (!frameBytes || !payloadBytes) {
    return makeError(StatusCode::kInvalidArg, "CXL lane geometry overflows");
  }

  std::set<uint64_t> cursorPages;
  std::array<DirectionState, 2> states;
  for (size_t directionIndex = 0; directionIndex < states.size(); ++directionIndex) {
    const auto &direction = config.directions[directionIndex];
    if (direction.producerCursorOffset % kCursorPageBytes != 0 ||
        direction.consumerCursorOffset % kCursorPageBytes != 0 ||
        direction.deliveredRecordOffset != direction.consumerCursorOffset + kCxlCacheLineBytes ||
        direction.frameRingOffset % kCxlCacheLineBytes != 0 || direction.payloadCellsOffset % kCxlCacheLineBytes != 0) {
      return makeError(StatusCode::kInvalidArg, "CXL lane offsets violate cursor-page or cache-line alignment");
    }
    cursorPages.insert(direction.producerCursorOffset);
    cursorPages.insert(direction.consumerCursorOffset);
    if (!rangeContains(layout, CxlRangeKind::CursorPages, direction.producerCursorOffset, sizeof(uint64_t)) ||
        !rangeContains(layout, CxlRangeKind::CursorPages, direction.consumerCursorOffset, sizeof(uint64_t)) ||
        !rangeContains(layout,
                       CxlRangeKind::CursorPages,
                       direction.deliveredRecordOffset,
                       sizeof(CxlDeliveredRecord)) ||
        !rangeContains(layout, CxlRangeKind::FrameRings, direction.frameRingOffset, *frameBytes) ||
        !rangeContains(layout, CxlRangeKind::RpcPayloadCells, direction.payloadCellsOffset, *payloadBytes)) {
      return makeError(StatusCode::kInvalidArg, "CXL lane offset is outside its validated layout range");
    }

    auto producer = region.checkedRange(direction.producerCursorOffset, sizeof(uint64_t), alignof(uint64_t));
    auto consumer = region.checkedRange(direction.consumerCursorOffset, sizeof(uint64_t), alignof(uint64_t));
    auto delivered =
        region.checkedRange(direction.deliveredRecordOffset, sizeof(CxlDeliveredRecord), alignof(CxlDeliveredRecord));
    auto frames = region.checkedRange(direction.frameRingOffset, *frameBytes, alignof(CxlFrameEntry));
    auto payloads = region.checkedRange(direction.payloadCellsOffset, *payloadBytes, kCxlCacheLineBytes);
    if (!producer || !consumer || !delivered || !frames || !payloads) {
      return makeError(StatusCode::kInvalidArg, "CXL lane mapping is incomplete or unaligned");
    }
    states[directionIndex] = DirectionState{
        .producerCursor = reinterpret_cast<uint64_t *>(producer->data()),
        .consumerCursor = reinterpret_cast<uint64_t *>(consumer->data()),
        .deliveredRecord = *delivered,
        .frameRing = *frames,
        .payloadCells = *payloads,
        .nextPushStreamOffset = 0,
        .nextPopStreamOffset = 0,
        .deliveredSequence = 0,
        .pending = {},
    };
  }
  if (cursorPages.size() != 4) {
    return makeError(StatusCode::kInvalidArg, "each CXL lane cursor writer requires a distinct page");
  }
  if (overlaps(config.directions[0].frameRingOffset, *frameBytes, config.directions[1].frameRingOffset, *frameBytes) ||
      overlaps(config.directions[0].payloadCellsOffset,
               *payloadBytes,
               config.directions[1].payloadCellsOffset,
               *payloadBytes)) {
    return makeError(StatusCode::kInvalidArg, "CXL lane directions overlap in the shared region");
  }

  CxlLane lane(config, role, states);
  const Direction outbound = role == CxlLaneRole::Requester ? Direction::Submission : Direction::Completion;
  const Direction inbound = role == CxlLaneRole::Requester ? Direction::Completion : Direction::Submission;
  std::atomic_ref<uint64_t>(*lane.directions_[index(outbound)].producerCursor).store(0, std::memory_order_release);
  std::atomic_ref<uint64_t>(*lane.directions_[index(inbound)].consumerCursor).store(0, std::memory_order_release);
  auto initialDelivered = lane.publishDelivered(lane.directions_[index(inbound)], 0);
  if (!initialDelivered) {
    return makeError(std::move(initialDelivered.error()));
  }
  return lane;
}

Result<size_t> CxlLane::tryPush(Direction direction, FrameView frame) {
  if (auto retired = retirementStatus()) {
    return makeError(*retired);
  }
  if (!mayPush(direction)) {
    return makeError(StatusCode::kInvalidArg, "CXL lane role does not own this producer");
  }

  const uint32_t flags = static_cast<uint32_t>(frame.flags);
  if (!validFrameFlags(flags) ||
      (frame.flags == CxlFrameFlags::Data && (frame.payload.empty() || frame.payload.size() > config_.cellBytes)) ||
      (frame.flags != CxlFrameFlags::Data && !frame.payload.empty())) {
    return makeError(StatusCode::kInvalidArg, "invalid CXL frame flags or payload length");
  }

  auto &state = directions_[index(direction)];
  auto cursors = validateCursors(state);
  if (!cursors) {
    Status error = cursors.error();
    retire(error);
    return makeError(std::move(error));
  }
  const uint64_t producer = std::atomic_ref<uint64_t>(*state.producerCursor).load(std::memory_order_relaxed);
  const uint64_t consumer = std::atomic_ref<uint64_t>(*state.consumerCursor).load(std::memory_order_acquire);
  if (producer - consumer == config_.depth) {
    return makeError(StatusCode::kQueueFull, "CXL lane is full");
  }
  if (producer >= std::numeric_limits<uint64_t>::max() - config_.depth) {
    return fail(RPCCode::kStaleGeneration, "CXL absolute cursor is exhausted; lane generation must advance");
  }
  if (frame.payload.size() > std::numeric_limits<uint64_t>::max() - state.nextPushStreamOffset) {
    return fail(RPCCode::kStaleGeneration, "CXL stream offset would overflow");
  }

  const uint64_t slot = producer % config_.depth;
  const uint64_t payloadIndex = slot * config_.cellBytes;
  auto payloadCell = state.payloadCells.subspan(payloadIndex, config_.cellBytes);
  if (!frame.payload.empty()) {
    std::memcpy(payloadCell.data(), frame.payload.data(), frame.payload.size());
  }

  CxlFrameEntry entry{};
  storeLe64(&entry.absoluteSequence, producer);
  storeLe64(&entry.sessionGeneration, config_.sessionGeneration);
  storeLe64(&entry.laneGeneration, config_.laneGeneration);
  storeLe64(&entry.streamOffset, state.nextPushStreamOffset);
  storeLe64(
      &entry.payloadOffset,
      frame.flags == CxlFrameFlags::Data ? config_.directions[index(direction)].payloadCellsOffset + payloadIndex : 0);
  storeLe32(&entry.payloadLength, frame.payload.size());
  storeLe32(&entry.flags, flags);
  storeLe32(&entry.crc32c, cxlCrc32cWithZeroedU32(entry, offsetof(CxlFrameEntry, crc32c), frame.payload));
  std::memcpy(state.frameRing.data() + slot * sizeof(CxlFrameEntry), &entry, sizeof(entry));
  std::atomic_thread_fence(std::memory_order_release);
  std::atomic_ref<uint64_t>(*state.producerCursor).store(producer + 1U, std::memory_order_release);
  state.nextPushStreamOffset += frame.payload.size();
  return frame.payload.size();
}

Result<size_t> CxlLane::tryPop(Direction direction, std::span<std::byte> destination) {
  if (auto retired = retirementStatus()) {
    return makeError(*retired);
  }
  if (!mayPop(direction)) {
    return makeError(StatusCode::kInvalidArg, "CXL lane role does not own this consumer");
  }

  auto &state = directions_[index(direction)];
  auto cursors = validateCursors(state);
  if (!cursors) {
    Status error = cursors.error();
    retire(error);
    return makeError(std::move(error));
  }

  if (!state.pending.active) {
    const uint64_t consumer = std::atomic_ref<uint64_t>(*state.consumerCursor).load(std::memory_order_relaxed);
    const uint64_t producer = std::atomic_ref<uint64_t>(*state.producerCursor).load(std::memory_order_acquire);
    if (producer == consumer) {
      return makeError(StatusCode::kQueueEmpty, "CXL lane is empty");
    }

    const uint64_t slot = consumer % config_.depth;
    CxlFrameEntry entry{};
    std::memcpy(&entry, state.frameRing.data() + slot * sizeof(CxlFrameEntry), sizeof(entry));
    const uint64_t streamOffset = loadLe64(&entry.streamOffset);
    const uint32_t payloadLength = loadLe32(&entry.payloadLength);
    const uint32_t flags = loadLe32(&entry.flags);
    const uint64_t expectedPayloadOffset =
        config_.directions[index(direction)].payloadCellsOffset + slot * config_.cellBytes;
    if (loadLe64(&entry.absoluteSequence) != consumer ||
        loadLe64(&entry.sessionGeneration) != config_.sessionGeneration ||
        loadLe64(&entry.laneGeneration) != config_.laneGeneration || streamOffset != state.nextPopStreamOffset ||
        !validFrameFlags(flags) || loadLe32(&entry.reserved0) != 0 || loadLe64(&entry.reserved1) != 0 ||
        payloadLength > config_.cellBytes ||
        (flags == static_cast<uint32_t>(CxlFrameFlags::Data) &&
         (payloadLength == 0 || loadLe64(&entry.payloadOffset) != expectedPayloadOffset)) ||
        (flags != static_cast<uint32_t>(CxlFrameFlags::Data) &&
         (payloadLength != 0 || loadLe64(&entry.payloadOffset) != 0))) {
      return fail(StatusCode::kDataCorruption, "invalid, stale or out-of-order CXL frame entry");
    }
    auto payload = state.payloadCells.subspan(slot * config_.cellBytes, payloadLength);
    const uint32_t storedCrc = loadLe32(&entry.crc32c);
    if (storedCrc != cxlCrc32cWithZeroedU32(entry, offsetof(CxlFrameEntry, crc32c), payload)) {
      return fail(StatusCode::kDataCorruption, "CXL frame or payload CRC32C mismatch");
    }
    if (flags != static_cast<uint32_t>(CxlFrameFlags::Data)) {
      std::atomic_ref<uint64_t>(*state.consumerCursor).store(consumer + 1U, std::memory_order_release);
      return fail(flags == static_cast<uint32_t>(CxlFrameFlags::Retire) ? RPCCode::kStaleGeneration
                                                                        : RPCCode::kBulkTransferError,
                  "CXL peer retired the lane or published an error frame");
    }
    state.pending = PendingRead{
        .active = true,
        .sequence = consumer,
        .streamOffset = streamOffset,
        .payloadLength = payloadLength,
        .consumed = 0,
        .payload = payload,
    };
  }

  if (destination.empty()) {
    return 0;
  }
  auto &pending = state.pending;
  const size_t copied = std::min<size_t>(destination.size(), pending.payloadLength - pending.consumed);
  std::memcpy(destination.data(), pending.payload.data() + pending.consumed, copied);
  pending.consumed += copied;
  auto delivered = publishDelivered(state, pending.streamOffset + pending.consumed);
  if (!delivered) {
    Status error = delivered.error();
    retire(error);
    return makeError(std::move(error));
  }
  if (pending.consumed == pending.payloadLength) {
    state.nextPopStreamOffset += pending.payloadLength;
    std::atomic_ref<uint64_t>(*state.consumerCursor).store(pending.sequence + 1U, std::memory_order_release);
    pending = PendingRead{};
  }
  return copied;
}

Result<bool> CxlLane::observeReadable(Direction direction) const {
  if (!mayPop(direction)) {
    return makeError(StatusCode::kInvalidArg, "CXL lane role does not own this consumer");
  }
  const auto &state = directions_[index(direction)];
  auto cursors = validateCursors(state);
  if (!cursors) {
    return makeError(cursors.error());
  }
  return std::atomic_ref<uint64_t>(*state.producerCursor).load(std::memory_order_acquire) !=
         std::atomic_ref<uint64_t>(*state.consumerCursor).load(std::memory_order_acquire);
}

Result<bool> CxlLane::observeWritable(Direction direction) const {
  if (!mayPush(direction)) {
    return makeError(StatusCode::kInvalidArg, "CXL lane role does not own this producer");
  }
  const auto &state = directions_[index(direction)];
  auto cursors = validateCursors(state);
  if (!cursors) {
    return makeError(cursors.error());
  }
  const uint64_t producer = std::atomic_ref<uint64_t>(*state.producerCursor).load(std::memory_order_acquire);
  const uint64_t consumer = std::atomic_ref<uint64_t>(*state.consumerCursor).load(std::memory_order_acquire);
  if (producer >= std::numeric_limits<uint64_t>::max() - config_.depth) {
    return makeError(RPCCode::kStaleGeneration, "CXL absolute cursor is exhausted");
  }
  return producer - consumer < config_.depth;
}

Result<std::optional<uint64_t>> CxlLane::observePeerDeliveredOffset(Direction direction, uint64_t publishedOffset) const {
  if (!mayPush(direction)) {
    return makeError(StatusCode::kInvalidArg, "CXL lane role does not own this producer");
  }
  const auto &state = directions_[index(direction)];
  // A seqlock writer may be descheduled with an odd sequence. Take one
  // nonblocking observation and let ordinary I/O progress observe it again;
  // exhausting a spin budget never proves that a valid publication is bad.
  auto snapshot = loadCxlOwnerRecord<CxlDeliveredRecord>(state.deliveredRecord, 1);
  if (!snapshot) {
    auto *words = reinterpret_cast<uint64_t *>(state.deliveredRecord.data());
    if (std::atomic_ref<uint64_t>(words[0]).load(std::memory_order_acquire) == 0) {
      return makeError(StatusCode::kDataCorruption, "uninitialized CXL peer-delivered record");
    }
    return std::optional<uint64_t>{};
  }
  std::string_view invalidReason;
  if (loadLe64(&snapshot->sessionGeneration) != config_.sessionGeneration) {
    invalidReason = "session generation mismatch";
  } else if (loadLe64(&snapshot->laneGeneration) != config_.laneGeneration) {
    invalidReason = "lane generation mismatch";
  } else if (loadLe64(&snapshot->deliveredOffsetComplement) != ~loadLe64(&snapshot->deliveredOffset)) {
    invalidReason = "delivered complement mismatch";
  } else if (loadLe32(&snapshot->reserved0) != 0 || loadLe64(&snapshot->reserved1) != 0 ||
             loadLe64(&snapshot->reserved2) != 0) {
    invalidReason = "nonzero reserved field";
  } else if (loadLe32(&snapshot->crc32c) != deliveredCrc(*snapshot)) {
    invalidReason = "delivered CRC mismatch";
  } else if (loadLe64(&snapshot->deliveredOffset) > publishedOffset) {
    invalidReason = "delivered offset exceeds published offset";
  }
  if (!invalidReason.empty()) {
    return makeError(StatusCode::kDataCorruption,
                     fmt::format("CXL peer-delivered record {}: sequence={} session={}/{} lane={}/{} delivered={} published={}",
                                 invalidReason, loadLe64(&snapshot->recordSequence),
                                 loadLe64(&snapshot->sessionGeneration), config_.sessionGeneration,
                                 loadLe64(&snapshot->laneGeneration), config_.laneGeneration,
                                 loadLe64(&snapshot->deliveredOffset), publishedOffset));
  }
  return std::optional<uint64_t>{loadLe64(&snapshot->deliveredOffset)};
}

Result<bool> CxlLane::readable(Direction direction) {
  if (auto retired = retirementStatus()) {
    return makeError(*retired);
  }
  if (!mayPop(direction)) {
    return makeError(StatusCode::kInvalidArg, "CXL lane role does not own this consumer");
  }
  auto &state = directions_[index(direction)];
  if (state.pending.active) {
    return true;
  }
  auto cursors = validateCursors(state);
  if (!cursors) {
    Status error = cursors.error();
    retire(error);
    return makeError(std::move(error));
  }
  return std::atomic_ref<uint64_t>(*state.producerCursor).load(std::memory_order_acquire) !=
         std::atomic_ref<uint64_t>(*state.consumerCursor).load(std::memory_order_relaxed);
}

Result<bool> CxlLane::writable(Direction direction) {
  if (auto retired = retirementStatus()) {
    return makeError(*retired);
  }
  if (!mayPush(direction)) {
    return makeError(StatusCode::kInvalidArg, "CXL lane role does not own this producer");
  }
  auto &state = directions_[index(direction)];
  auto cursors = validateCursors(state);
  if (!cursors) {
    Status error = cursors.error();
    retire(error);
    return makeError(std::move(error));
  }
  const uint64_t producer = std::atomic_ref<uint64_t>(*state.producerCursor).load(std::memory_order_relaxed);
  const uint64_t consumer = std::atomic_ref<uint64_t>(*state.consumerCursor).load(std::memory_order_acquire);
  if (producer >= std::numeric_limits<uint64_t>::max() - config_.depth) {
    Status error(RPCCode::kStaleGeneration, "CXL absolute cursor is exhausted; lane generation must advance");
    retire(error);
    return makeError(std::move(error));
  }
  return producer - consumer < config_.depth;
}

Result<std::optional<uint64_t>> CxlLane::peerDeliveredOffset(Direction direction) {
  if (auto retired = retirementStatus()) {
    return makeError(*retired);
  }
  if (!mayPush(direction)) {
    return makeError(StatusCode::kInvalidArg, "CXL lane role does not own this producer");
  }
  auto observed = observePeerDeliveredOffset(direction, directions_[index(direction)].nextPushStreamOffset);
  if (!observed) {
    Status error = observed.error();
    retire(error);
    return makeError(std::move(error));
  }
  return observed;
}

void CxlLane::retire(Status status) {
  auto expected = StatusCode::kOK;
  retirementCode_.compare_exchange_strong(expected, status.code(), std::memory_order_acq_rel);
}

std::optional<Status> CxlLane::retirementStatus() const {
  const auto code = retirementCode_.load(std::memory_order_acquire);
  return code == StatusCode::kOK ? std::nullopt : std::optional<Status>{std::in_place, code};
}

uint64_t CxlLane::producerCursor(Direction direction) const {
  return std::atomic_ref<uint64_t>(*directions_[index(direction)].producerCursor).load(std::memory_order_acquire);
}

uint64_t CxlLane::consumerCursor(Direction direction) const {
  return std::atomic_ref<uint64_t>(*directions_[index(direction)].consumerCursor).load(std::memory_order_acquire);
}

uint64_t CxlLane::nextStreamOffset(Direction direction) const {
  const auto &state = directions_[index(direction)];
  return mayPush(direction) ? state.nextPushStreamOffset : state.nextPopStreamOffset;
}

bool CxlLane::mayPush(Direction direction) const noexcept {
  return (role_ == CxlLaneRole::Requester && direction == Direction::Submission) ||
         (role_ == CxlLaneRole::Acceptor && direction == Direction::Completion);
}

bool CxlLane::mayPop(Direction direction) const noexcept {
  return (role_ == CxlLaneRole::Requester && direction == Direction::Completion) ||
         (role_ == CxlLaneRole::Acceptor && direction == Direction::Submission);
}

Result<Void> CxlLane::validateCursors(const DirectionState &state) const {
  const uint64_t producer = std::atomic_ref<uint64_t>(*state.producerCursor).load(std::memory_order_acquire);
  const uint64_t consumer = std::atomic_ref<uint64_t>(*state.consumerCursor).load(std::memory_order_acquire);
  if (consumer > producer || producer - consumer > config_.depth) {
    return makeError(StatusCode::kDataCorruption, "invalid CXL absolute cursor relationship");
  }
  return Void{};
}

Result<Void> CxlLane::publishDelivered(DirectionState &state, uint64_t offset) {
  if (state.deliveredSequence > std::numeric_limits<uint64_t>::max() - 2U) {
    return makeError(RPCCode::kStaleGeneration, "CXL delivered-record sequence would overflow");
  }
  state.deliveredSequence += 2U;
  CxlDeliveredRecord record{};
  storeLe64(&record.recordSequence, state.deliveredSequence);
  storeLe64(&record.sessionGeneration, config_.sessionGeneration);
  storeLe64(&record.laneGeneration, config_.laneGeneration);
  storeLe64(&record.deliveredOffset, offset);
  storeLe64(&record.deliveredOffsetComplement, ~offset);
  storeLe32(&record.crc32c, deliveredCrc(record));
  if (!publishCxlOwnerRecord(state.deliveredRecord, record)) {
    return makeError(StatusCode::kDataCorruption, "failed to publish CXL delivered record");
  }
  return Void{};
}

Result<size_t> CxlLane::fail(status_code_t code, std::string_view message) {
  Status error(code, message);
  retire(error);
  return makeError(std::move(error));
}

}  // namespace hf3fs::net::cxl
