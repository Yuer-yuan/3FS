#include <boost/core/ignore_unused.hpp>
#include <cstring>
#include <limits>

#include "StorageClientImpl.h"
#include "StorageClientInMem.h"
#include "common/monitor/ScopedMetricsWriter.h"
#include "common/net/TransportRuntime.h"

namespace hf3fs::storage::client {

static monitor::CountRecorder iobuf_reg_success_ops{"storage_client.iobuf_reg.success_ops"};
static monitor::CountRecorder iobuf_reg_failed_ops{"storage_client.iobuf_reg.failed_ops"};
static monitor::LatencyRecorder iobuf_reg_latency{"storage_client.iobuf_reg.latency"};
static monitor::DistributionRecorder iobuf_reg_size{"storage_client.iobuf_reg.size"};
static monitor::CountRecorder shadowCopyInBytes{"storage_client.shadow_copy_in_bytes"};
static monitor::CountRecorder shadowCopyOutBytes{"storage_client.shadow_copy_out_bytes"};

bool IOBuffer::contains(const uint8_t *data, uint32_t len) const {
  const auto begin = reinterpret_cast<uintptr_t>(data_);
  const auto candidate = reinterpret_cast<uintptr_t>(data);
  return data_ != nullptr && candidate >= begin && candidate - begin <= length_ && len <= length_ - (candidate - begin);
}

Result<net::RemoteExport> IOBuffer::exportRemote(size_t offset, size_t length, net::RemoteAccess access) const {
  auto range = shared_.subrange(offset, length);
  if (!range) {
    return makeError(std::move(range.error()));
  }
  if (shadowed() && (net::remoteAccessBits(access) & net::remoteAccessBits(net::RemoteAccess::Read)) != 0) {
    std::memcpy(range->data(), data_ + offset, length);
    shadowCopyInBytes.addSample(length);
  }
  return range->exportRemote(access);
}

Result<Void> IOBuffer::copyOut(size_t offset, size_t length) const {
  auto range = shared_.subrange(offset, length);
  if (!range) {
    return makeError(std::move(range.error()));
  }
  if (shadowed()) {
    std::memcpy(data_ + offset, range->data(), length);
    shadowCopyOutBytes.addSample(length);
  }
  return Void{};
}

const StorageClient::Config StorageClient::kDefaultConfig;

std::shared_ptr<StorageClient> StorageClient::create(ClientId clientId,
                                                     const Config &config,
                                                     hf3fs::client::ICommonMgmtdClient &mgmtdClient) {
  const auto &trafficControl = config.traffic_control();

  if (trafficControl.max_concurrent_updates() > UpdateChannelAllocator::kMaxNumChannels) {
    XLOGF(CRITICAL,
          "Bad config: trafficControl.max_concurrent_updates {} > UpdateChannelAllocator::kMaxNumChannels {}",
          trafficControl.max_concurrent_updates(),
          UpdateChannelAllocator::kMaxNumChannels);
    return nullptr;
  }

  std::shared_ptr<StorageClient> client;

  if (config.implementation_type() == ImplementationType::RPC) {
    client = std::make_shared<StorageClientImpl>(clientId, config, mgmtdClient);
  } else if (config.implementation_type() == ImplementationType::InMem) {
    client = std::make_shared<StorageClientInMem>(clientId, config, mgmtdClient);
  }

  if (!client || !client->start()) {
    XLOGF(CRITICAL,
          "Failed to create and start storage client of type {}",
          magic_enum::enum_name(config.implementation_type()));
    client.reset();
  }

  return client;
}

ReadIO StorageClient::createReadIO(ChainId chainId,
                                   const ChunkId &chunkId,
                                   uint32_t offset,
                                   uint32_t length,
                                   uint8_t *data,
                                   IOBuffer *buffer,
                                   void *userCtx) {
  return ReadIO{chainId, chunkId, offset, length, data, buffer, userCtx};
}

WriteIO StorageClient::createWriteIO(ChainId chainId,
                                     const ChunkId &chunkId,
                                     uint32_t offset,
                                     uint32_t length,
                                     uint32_t chunkSize,
                                     uint8_t *data,
                                     IOBuffer *buffer,
                                     void *userCtx) {
  RequestId requestId(nextRequestId_.fetch_add(1));
  return WriteIO{requestId, chainId, chunkId, offset, length, chunkSize, data, buffer, userCtx};
}

QueryLastChunkOp StorageClient::createQueryOp(ChainId chainId,
                                              ChunkId chunkIdBegin,
                                              ChunkId chunkIdEnd,
                                              uint32_t maxNumChunkIdsToProcess,
                                              void *userCtx) {
  return QueryLastChunkOp{chainId, {chunkIdBegin, chunkIdEnd, maxNumChunkIdsToProcess}, userCtx};
}

RemoveChunksOp StorageClient::createRemoveOp(ChainId chainId,
                                             ChunkId chunkIdBegin,
                                             ChunkId chunkIdEnd,
                                             uint32_t maxNumChunkIdsToProcess,
                                             void *userCtx) {
  RequestId requestId(nextRequestId_.fetch_add(1));
  return RemoveChunksOp{requestId, chainId, {chunkIdBegin, chunkIdEnd, maxNumChunkIdsToProcess}, userCtx};
}

TruncateChunkOp StorageClient::createTruncateOp(ChainId chainId,
                                                const ChunkId &chunkId,
                                                uint32_t chunkLen,
                                                uint32_t chunkSize,
                                                bool onlyExtendChunk,
                                                void *userCtx) {
  RequestId requestId(nextRequestId_.fetch_add(1));
  return TruncateChunkOp(requestId, chainId, chunkId, chunkLen, chunkSize, onlyExtendChunk, userCtx);
}

Result<IOBuffer> StorageClient::registerIOBuffer(uint8_t *buf, size_t len) {
  monitor::ScopedLatencyWriter latencyWriter(iobuf_reg_latency);
  iobuf_reg_size.addSample(len);

  if (buf == nullptr || len == 0) {
    iobuf_reg_failed_ops.addSample(1);
    return makeError(StatusCode::kInvalidArg, "cannot register an empty IO buffer");
  }

  auto arena = net::TransportRuntime::cxlBufferArena();
  auto shared = arena ? arena->tryAllocate(len)
                : config_.implementation_type() == ImplementationType::InMem
                    ? net::SharedBuffer::allocateHeap(len)
                    : Result<net::SharedBuffer>{makeError(RPCCode::kDataPlaneNotInitialized)};
  if (!shared) {
    iobuf_reg_failed_ops.addSample(1);
    return makeError(std::move(shared.error()));
  }
  iobuf_reg_success_ops.addSample(1);
  return IOBuffer{std::move(*shared), buf, len};
}

Result<IOBuffer> StorageClient::allocateIOBuffer(size_t len) {
  auto arena = net::TransportRuntime::cxlBufferArena();
  auto shared = arena ? arena->tryAllocate(len)
                : config_.implementation_type() == ImplementationType::InMem
                    ? net::SharedBuffer::allocateHeap(len)
                    : Result<net::SharedBuffer>{makeError(RPCCode::kDataPlaneNotInitialized)};
  if (!shared) {
    return makeError(std::move(shared.error()));
  }
  return IOBuffer{std::move(*shared)};
}

}  // namespace hf3fs::storage::client
