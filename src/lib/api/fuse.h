#pragma once

#include <asm-generic/ioctl.h>
#include <cstdint>
#include <linux/limits.h>
#include <sys/ioctl.h>
#include <sys/types.h>

namespace hf3fs::lib::fuse {

#define HF3FS_IOCTYPE_ID 'h'
#define HF3FS_IOCTL_PUNCH_HOLE_MAX 1000

struct Hf3fsIoctlGetMountNameArg {
  char str[32];
};

struct Hf3fsIoctlHardlinkArg {
  ino_t ino;
  char str[NAME_MAX];
};

struct Hf3fsIoctlPunchHoleArg {
  int n;
  size_t flags;
  size_t start[HF3FS_IOCTL_PUNCH_HOLE_MAX];
  size_t end[HF3FS_IOCTL_PUNCH_HOLE_MAX];
};

struct Hf3fsIoctlMove {
  uint64_t srcParent;
  char srcName[NAME_MAX + 1];
  uint64_t dstParent;
  char dstName[NAME_MAX + 1];
  bool moveToTrash;
};

struct Hf3fsIoctlRemove {
  uint64_t parent;
  char name[NAME_MAX + 1];
  bool recursive;
};

// Trusted-process, file-backed CXL experiment interface. This is not a
// range-restricted fd capability: callers must already have fabric access.
struct Hf3fsIoctlCxlIov {
  uint64_t size;
  uint64_t blockSize;
  uint8_t id[16];
  uint64_t offset;
  uint64_t device;
  uint64_t inode;
  char path[PATH_MAX];
};

enum {
  HF3FS_IOC_GET_MOUNT_NAME = _IOR(HF3FS_IOCTYPE_ID, 0, Hf3fsIoctlGetMountNameArg),
  HF3FS_IOC_GET_PATH_OFFSET = _IOR(HF3FS_IOCTYPE_ID, 1, uint32_t),
  HF3FS_IOC_GET_MAGIC_NUM = _IOR(HF3FS_IOCTYPE_ID, 2, uint32_t),
  HF3FS_IOC_GET_IOCTL_VERSION = _IOR(HF3FS_IOCTYPE_ID, 3, uint32_t),

  HF3FS_IOC_RECURSIVE_RM = _IOR(HF3FS_IOCTYPE_ID, 10, uint32_t),
  HF3FS_IOC_FSYNC = _IOR(HF3FS_IOCTYPE_ID, 11, uint32_t),
  HF3FS_IOC_HARDLINK = _IOW(HF3FS_IOCTYPE_ID, 12, Hf3fsIoctlHardlinkArg),
  HF3FS_IOC_PUNCH_HOLE = _IOW(HF3FS_IOCTYPE_ID, 13, Hf3fsIoctlPunchHoleArg),
  HF3FS_IOC_MOVE = _IOW(HF3FS_IOCTYPE_ID, 14, Hf3fsIoctlMove),
  HF3FS_IOC_REMOVE = _IOW(HF3FS_IOCTYPE_ID, 15, Hf3fsIoctlRemove),
  HF3FS_IOC_CXL_IOV_CREATE = _IOWR(HF3FS_IOCTYPE_ID, 20, Hf3fsIoctlCxlIov),
  HF3FS_IOC_CXL_IOV_OPEN = _IOWR(HF3FS_IOCTYPE_ID, 21, Hf3fsIoctlCxlIov),
  HF3FS_IOC_IOBUFFER_COPY_STATS = _IOR(HF3FS_IOCTYPE_ID, 22, uint64_t[2]),
};

}  // namespace hf3fs::lib::fuse
