#pragma once

#include <cstdint>
#include <string_view>

namespace hf3fs {
// NOTE: `StatusCode` is used as the namespace name so here we use `status_code_t` as the type name.
using status_code_t = uint16_t;

#define RAW_STATUS(name, value)                   \
  namespace StatusCode {                          \
  inline constexpr status_code_t k##name = value; \
  }

#define STATUS(ns, name, value)                     \
  namespace ns##Code {                              \
    inline constexpr status_code_t k##name = value; \
  }

#include "StatusCodeDetails.h"

#undef STATUS
#undef RAW_STATUS

// Transport-neutral source aliases. Their values and diagnostic strings stay
// identical to the legacy symbols so the RPC wire ABI remains unchanged.
namespace RPCCode {
inline constexpr auto kDataPlaneInitFailed = kIBInitFailed;
inline constexpr auto kDataPlaneInterfaceNotFound = kIBDeviceNotFound;
inline constexpr auto kBulkPostFailed = kRDMAPostFailed;
inline constexpr auto kBulkTransferError = kRDMAError;
inline constexpr auto kNoSharedBuffer = kRDMANoBuf;
inline constexpr auto kDataPlaneNotInitialized = kIBDeviceNotInitialized;
inline constexpr auto kDataPlaneOpenFailed = kIBOpenPortFailed;
}  // namespace RPCCode

namespace StorageClientCode {
inline constexpr auto kNoDataPlaneInterface = kNoRDMAInterface;
}  // namespace StorageClientCode

enum class StatusCodeType {
  Invalid = -1,
  Common = 0,
  Transaction,
  RPC,
  Meta,
  Storage,
  Mgmtd,
  MgmtdClient,
  StorageClient,
  ClientAgent,
  Cli,
  KvService,
};

namespace StatusCode {
std::string_view toString(status_code_t code);

StatusCodeType typeOf(status_code_t code);

int toErrno(status_code_t code);
}  // namespace StatusCode
}  // namespace hf3fs
