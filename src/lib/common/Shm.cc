#include "Shm.h"

#include <fcntl.h>
#include <folly/experimental/coro/BlockingWait.h>
#include <numa.h>
#include <sys/mman.h>
#include <unistd.h>

namespace hf3fs::lib {
ShmBuf::ShmBuf(const Path &p, size_t sz, size_t bsz, int numa, meta::Uid u, int pr, int pp)
    : path(p),
      size(sz),
      blockSize(bsz ? bsz : sz),
      off(0),
      user(u),
      pid(pr),
      ppid(pp),
      id(Uuid::random()),
      owner_(true),
      numaNode_(numa),
      memhs_((size + blockSize - 1) / blockSize) {
  if (bsz < 0 || bsz > sz) {
    throw std::invalid_argument(fmt::format("invalid block size {} total bytes {}", bsz, sz));
  }

  mapBuf();

  if (numaNode_ >= 0) {
    numa_tonode_memory(bufStart, size, numaNode_);
  }
}

ShmBuf::ShmBuf(const Path &p, off_t o, size_t sz, size_t bsz, Uuid u)
    : path(p),
      size(sz),
      blockSize(bsz ? bsz : sz),
      off(o),
      //      user(0),
      id(u),
      owner_(false),
      numaNode_(-1),
      memhs_((size + blockSize - 1) / blockSize) {
  if (bsz < 0 || bsz > sz) {
    throw std::invalid_argument(fmt::format("invalid block size {} total bytes {}", bsz, sz));
  }

  XLOGF(DBG, "this {} buf start {} off {}", (void *)this, (void *)bufStart, off);

  mapBuf();

  XLOGF(DBG, "this {} buf start {} off {}", (void *)this, (void *)bufStart, off);
}

ShmBuf::ShmBuf(std::shared_ptr<storage::client::IOBuffer> storage, size_t bsz, Uuid u)
    : bufStart(storage->data()),
      size(storage->size()),
      blockSize(bsz ? bsz : size),
      off(0),
      id(u),
      owner_(false),
      numaNode_(-1),
      memhs_((size - 1) / blockSize + 1),
      cxlStorage_(std::move(storage)) {
  for (size_t i = 0; i < memhs_.size(); ++i) {
    auto range = cxlStorage_->subrange(i * blockSize, std::min(size - i * blockSize, blockSize));
    if (!range) {
      throw std::runtime_error("invalid CXL IOV block range");
    }
    memhs_[i].store(std::make_shared<storage::client::IOBuffer>(std::move(*range)));
  }
}

ShmBuf::ShmBuf(uint8_t *mapping, size_t sz, size_t bsz, Uuid u, int leaseFd, bool creator)
    : bufStart(mapping),
      size(sz),
      blockSize(bsz ? bsz : sz),
      off(0),
      id(u),
      owner_(false),
      numaNode_(-1),
      cxlLeaseFd_(leaseFd),
      cxlCreator_(creator) {}

ShmBuf::~ShmBuf() {
  XLOGF(DBG, "calling dtor of shm {}", (void *)this);

  folly::coro::blockingWait(deregisterForIO());

  // if (owner_) {
  //   auto res = ftruncate(fd_, 0);
  //   XLOGF_IF(ERR, res < 0, "ftruncate shm file to 0 failed {}", res);
  // }

  unmapBuf();
  // The FUSE directory handle pins the arena allocation until after munmap.
  if (cxlLeaseFd_ >= 0) {
    close(cxlLeaseFd_);
  }
}

CoTask<void> ShmBuf::deregisterForIO() {
  XLOGF(DBG, "regging {} memhs {}", regging_.load(), memhs_.size());

  if (!regging_) {
    co_return;
  }

  if (!memhs_[0].load()) {
    co_await memhBaton_;
  }

  regging_ = false;

  for (auto &memh : memhs_) {
    memh.store(std::shared_ptr<storage::client::IOBuffer>());
  }

  co_return;
}

CoTask<void> ShmBuf::registerForIO(folly::Executor::KeepAlive<> exec,
                                   storage::client::StorageClient &sc,
                                   std::function<void()> &&recordMetrics) {
  if (regging_) {
    co_await memhBaton_;
  }

  regging_ = true;
  memhBaton_.reset();

  for (auto &memh : memhs_) {
    memh.store(std::shared_ptr<storage::client::IOBuffer>());
  }

  auto f = [this, &sc, recordMetrics = std::move(recordMetrics)]() {
    for (size_t i = 0; i < memhs_.size(); ++i) {
      auto res = sc.registerIOBuffer(bufStart + blockSize * i, std::min(size - blockSize * i, blockSize));
      if (!res) {
        XLOGF(ERR,
              "failed to register buffer @{} seg #{} with bytes {} block size {} code {} msg {}",
              (void *)bufStart,
              i,
              size,
              blockSize,
              res.error().code(),
              res.error().message());
      } else {
        memhs_[i].store(std::make_shared<storage::client::IOBuffer>(std::move(*res)));
        recordMetrics();
      }
    }

    memhBaton_.post();
  };
  if (exec) {
    exec.get()->add(std::move(f));
  } else {
    f();
  }
}

void ShmBuf::unmapBuf() {
  if (bufStart) {
    if (!cxlStorage_) {
      munmap(bufStart, size);
    }

    if (owner_) {
      shm_unlink(path.c_str());
    }

    bufStart = nullptr;
  }
}

CoTask<std::shared_ptr<storage::client::IOBuffer>> ShmBuf::memh(size_t off) {
  auto idx = off / blockSize;
  if (regging_ && !memhs_[idx].load()) {
    co_await memhBaton_;
  }

  co_return memhs_[idx].load();
}

void ShmBuf::mapBuf() {
  auto fd = shm_open(path.c_str(), O_RDWR | (owner_ ? O_CREAT | O_EXCL : 0), 0666);
  if (fd < 0) {
    auto err = errno;
    throw std::runtime_error(fmt::format("failed to shm_open {} is owner {} errno {} euid {} uid {}",
                                         path.native(),
                                         owner_,
                                         err,
                                         geteuid(),
                                         getuid()));
  }

  SCOPE_EXIT { close(fd); };

  if (owner_) {
    auto res = ftruncate(fd, size);
    if (res < 0) {
      throw std::runtime_error("failed to ftruncate");
    }
  }
  bufStart = (uint8_t *)mmap(nullptr, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, off);
  if (bufStart == MAP_FAILED) {
    throw std::runtime_error("failed to mmap");
  }

  XLOGF(DBG, "this {} owner {} buf start {} size {} off {}", (void *)this, owner_, (void *)bufStart, size, off);
}
}  // namespace hf3fs::lib
