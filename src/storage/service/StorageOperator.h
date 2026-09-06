#pragma once

#include <folly/concurrency/ConcurrentHashMap.h>
#include <folly/executors/CPUThreadPoolExecutor.h>

#include "analytics/StructuredTraceLog.h"
#include "client/mgmtd/IMgmtdClientForServer.h"
#include "client/mgmtd/RoutingInfo.h"
#include "client/storage/StorageMessenger.h"
#include "common/net/Buffer.h"
#include "common/net/Server.h"
#include "common/net/Transport.h"
#include "common/utils/Address.h"
#include "common/utils/ConfigBase.h"
#include "common/utils/Coroutine.h"
#include "common/utils/LockManager.h"
#include "common/utils/Semaphore.h"
#include "storage/aio/AioReadWorker.h"
#include "storage/service/BufferPool.h"
#include "storage/service/ReliableForwarding.h"
#include "storage/service/ReliableUpdate.h"
#include "storage/store/StorageTargets.h"
#include "storage/update/UpdateWorker.h"

namespace hf3fs::storage {

struct Components;

class StorageOperator {
 public:
  class Config : public ConfigBase<Config> {
    CONFIG_OBJ(write_worker, UpdateWorker::Config);
    CONFIG_OBJ(event_trace_log, analytics::StructuredTraceLog<StorageEventTrace>::Config);
    CONFIG_HOT_UPDATED_ITEM(max_num_results_per_query, uint32_t{100});
    CONFIG_HOT_UPDATED_ITEM(batch_read_job_split_size, uint32_t{1024});
    CONFIG_HOT_UPDATED_ITEM(post_buffer_per_bytes, 64_KB);
    CONFIG_HOT_UPDATED_ITEM(batch_read_ignore_chain_version, false);
    CONFIG_HOT_UPDATED_ITEM(max_concurrent_bulk_writes, 256U);
    CONFIG_HOT_UPDATED_ITEM(max_concurrent_bulk_reads, 256U);
    CONFIG_HOT_UPDATED_ITEM(max_concurrent_rdma_writes, 0U);  // deprecated config alias
    CONFIG_HOT_UPDATED_ITEM(max_concurrent_rdma_reads, 0U);   // deprecated config alias
    CONFIG_HOT_UPDATED_ITEM(read_only, false);
    CONFIG_HOT_UPDATED_ITEM(bulk_transmission_req_timeout, 0_ms);
    CONFIG_HOT_UPDATED_ITEM(apply_transmission_before_getting_semaphore, true);

   public:
    uint32_t effectiveMaxConcurrentBulkWrites() const {
      return max_concurrent_rdma_writes() ? max_concurrent_rdma_writes() : max_concurrent_bulk_writes();
    }
    uint32_t effectiveMaxConcurrentBulkReads() const {
      return max_concurrent_rdma_reads() ? max_concurrent_rdma_reads() : max_concurrent_bulk_reads();
    }
  };

  StorageOperator(const Config &config, Components &components)
      : config_(config),
        components_(components),
        updateWorker_(config_.write_worker()),
        storageEventTrace_(config.event_trace_log()),
        concurrentBulkWriteSemaphore_(config.effectiveMaxConcurrentBulkWrites()),
        concurrentBulkReadSemaphore_(config.effectiveMaxConcurrentBulkReads()) {
    onConfigUpdated_ = config_.addCallbackGuard([this]() {
      concurrentBulkWriteSemaphore_.changeUsableTokens(config_.effectiveMaxConcurrentBulkWrites());
      concurrentBulkReadSemaphore_.changeUsableTokens(config_.effectiveMaxConcurrentBulkReads());
    });
  }

  Result<Void> init(uint32_t numberOfDisks);

  Result<Void> stopAndJoin();

  CoTryTask<BatchReadRsp> batchRead(ServiceRequestContext &requestCtx,
                                    const BatchReadReq &req,
                                    serde::CallContext &ctx);

  CoTryTask<WriteRsp> write(ServiceRequestContext &requestCtx, const WriteReq &req, serde::CallContext *ctx);

  CoTryTask<UpdateRsp> update(ServiceRequestContext &requestCtx, const UpdateReq &req, serde::CallContext *ctx);

  CoTryTask<QueryLastChunkRsp> queryLastChunk(ServiceRequestContext &requestCtx, const QueryLastChunkReq &req);

  CoTryTask<TruncateChunksRsp> truncateChunks(ServiceRequestContext &requestCtx, const TruncateChunksReq &req);

  CoTryTask<RemoveChunksRsp> removeChunks(ServiceRequestContext &requestCtx, const RemoveChunksReq &req);

  CoTryTask<TargetSyncInfo> syncStart(const SyncStartReq &req);

  CoTryTask<SyncDoneRsp> syncDone(const SyncDoneReq &req);

  CoTryTask<SpaceInfoRsp> spaceInfo(const SpaceInfoReq &req);

  CoTryTask<CreateTargetRsp> createTarget(const CreateTargetReq &req);

  CoTryTask<OfflineTargetRsp> offlineTarget(const OfflineTargetReq &req);

  CoTryTask<RemoveTargetRsp> removeTarget(const RemoveTargetReq &req);

  CoTryTask<QueryChunkRsp> queryChunk(const QueryChunkReq &req);

  CoTryTask<GetAllChunkMetadataRsp> getAllChunkMetadata(const GetAllChunkMetadataReq &req);

 protected:
  using ChunkMetadataProcessor = std::function<CoTryTask<void>(const ChunkId &, const ChunkMetadata &)>;

  CoTask<IOResult> handleUpdate(ServiceRequestContext &requestCtx,
                                UpdateReq &req,
                                serde::CallContext *ctx,
                                TargetPtr &target);

  CoTask<IOResult> doUpdate(ServiceRequestContext &requestCtx,
                            const UpdateIO &updateIO,
                            const UpdateOptions &updateOptions,
                            uint32_t featureFlags,
                            const std::shared_ptr<StorageTarget> &target,
                            serde::CallContext *ctx,
                            BufferPool::Buffer &buffer,
                            const uint8_t *&forwardingData,
                            ChunkEngineUpdateJob &chunkEngineJob,
                            bool allowToAllocate);

  CoTask<IOResult> doCommit(ServiceRequestContext &requestCtx,
                            const CommitIO &commitIO,
                            const UpdateOptions &updateOptions,
                            ChunkEngineUpdateJob &chunkEngineJob,
                            uint32_t featureFlags,
                            const std::shared_ptr<StorageTarget> &target);

  Result<std::vector<std::pair<ChunkId, ChunkMetadata>>> doQuery(ServiceRequestContext &requestCtx,
                                                                 const VersionedChainId &vChainId,
                                                                 const ChunkIdRange &chunkIdRange);

  CoTryTask<uint32_t> processQueryResults(ServiceRequestContext &requestCtx,
                                          const VersionedChainId &vChainId,
                                          const ChunkIdRange &chunkIdRanges,
                                          ChunkMetadataProcessor processor,
                                          bool &moreChunksInRange);

  CoTask<IOResult> doTruncate(ServiceRequestContext &requestCtx,
                              const TruncateChunkOp &op,
                              flat::UserInfo userInfo,
                              uint32_t featureFlags);

  CoTask<IOResult> doRemove(ServiceRequestContext &requestCtx,
                            const RemoveChunksOp &op,
                            flat::UserInfo userInfo,
                            uint32_t featureFlags);

 private:
  friend class ReliableUpdate;

  ConstructLog<"storage::StorageOperator"> constructLog_;
  const Config &config_;
  Components &components_;
  UpdateWorker updateWorker_;
  analytics::StructuredTraceLog<StorageEventTrace> storageEventTrace_;
  std::unique_ptr<ConfigCallbackGuard> onConfigUpdated_;
  hf3fs::Semaphore concurrentBulkWriteSemaphore_;
  hf3fs::Semaphore concurrentBulkReadSemaphore_;
  std::atomic<uint64_t> totalReadBytes_{};
  std::atomic<uint64_t> totalReadIOs_{};
};

}  // namespace hf3fs::storage
