#pragma once

#include <array>
#include <set>

#include "common/net/TransportKind.h"
#include "common/serde/CallContext.h"
#include "common/serde/Echo.h"
#include "common/serde/Service.h"
#include "common/utils/ConstructLog.h"
#include "common/utils/Coroutine.h"
#include "common/utils/Result.h"

namespace hf3fs::serde {

class Services {
 public:
  Services() {
    addService(std::make_unique<echo::ServiceImpl>(), {net::ServicePlane::Control, net::ServicePlane::Data});
  }

  template <class Service>
  Result<Void> addService(std::unique_ptr<Service> &&obj, std::set<net::ServicePlane> planes) {
    if (UNLIKELY(planes.empty())) {
      return makeError(StatusCode::kInvalidArg, "service must be registered in at least one plane");
    }
    for (auto plane : planes) {
      auto index = net::planeIndex(plane);
      if (UNLIKELY(index >= services_.size())) {
        return makeError(StatusCode::kInvalidArg, "invalid service plane");
      }
      if (UNLIKELY(services_[index][Service::kServiceID].object != nullptr)) {
        return makeError(StatusCode::kInvalidArg, fmt::format("redundant service id: {}", Service::kServiceID));
      }
    }

    std::shared_ptr<Service> shared = std::move(obj);
    for (auto plane : planes) {
      auto &service = services_[net::planeIndex(plane)][Service::kServiceID];
      service.getter = &MethodExtractor<Service, CallContext, &CallContext::invalidId>::get;
      service.object = shared.get();
      service.alive = std::shared_ptr<void *>(shared, nullptr);
      if constexpr (requires { Service{}.onError(Status::OK); }) {
        service.onError = &CallContext::customOnError<Service, &Service::onError>;
      }
    }
    return Void{};
  }

  CallContext::ServiceWrapper &getServiceById(uint16_t idx, net::ServicePlane plane) {
    return services_.at(net::planeIndex(plane)).at(idx);
  }

 private:
  ConstructLog<"serde::Services"> constructLog_;
  std::array<std::array<CallContext::ServiceWrapper, 65536>, 2> services_;
};

}  // namespace hf3fs::serde
