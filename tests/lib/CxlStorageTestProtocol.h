#pragma once

#include "common/net/TransportRuntime.h"
#include "fbs/mgmtd/RoutingInfo.h"
#include "tests/lib/CxlTestProcess.h"

namespace hf3fs::test {

struct CxlStorageTestRequest {
  SERDE_STRUCT_FIELD(command, std::string{});
  SERDE_STRUCT_FIELD(config, std::string{});
  SERDE_STRUCT_FIELD(runtime, std::string{});
  SERDE_STRUCT_FIELD(appInfo, flat::AppInfo{});
  // AppInfo inherits FbsAppInfo's wire fields; clusterId is process-local.
  SERDE_STRUCT_FIELD(clusterId, std::string{});
  SERDE_STRUCT_FIELD(routing, flat::RoutingInfo{});
  SERDE_STRUCT_FIELD(targetState, flat::LocalTargetState::INVALID);
};

struct CxlStorageTestReply {
  SERDE_STRUCT_FIELD(error, std::string{});
  SERDE_STRUCT_FIELD(appInfo, flat::AppInfo{});
  SERDE_STRUCT_FIELD(value, false);
  SERDE_STRUCT_FIELD(generation, uint64_t{});
};

template <class T>
void requireCxlTestResult(const T &result) {
  if (!result) throw std::runtime_error(result.error().describe());
}

inline CxlStorageTestReply decodeCxlTestReply(std::string_view message) {
  CxlStorageTestReply reply;
  requireCxlTestResult(serde::deserialize(reply, message));
  if (!reply.error.empty()) throw std::runtime_error(reply.error);
  return reply;
}

inline CxlStorageTestReply cxlTestRequest(CxlTestProcess &process, const CxlStorageTestRequest &request) {
  return decodeCxlTestReply(process.request(serde::serialize(request)));
}

}  // namespace hf3fs::test
