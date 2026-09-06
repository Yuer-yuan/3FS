#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <inttypes.h>
#include <mpi.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static uint64_t ns(clockid_t id) {
  struct timespec t;
  if (clock_gettime(id, &t)) { perror("clock_gettime"); exit(1); }
  return (uint64_t)t.tv_sec * 1000000000ULL + t.tv_nsec;
}

int main(int argc, char **argv) {
  if (argc == 3 && !strcmp(argv[1], "set-clock")) {
    char *end;
    errno = 0;
    long long seconds = strtoll(argv[2], &end, 10);
    if (errno || *end || seconds < 1700000000LL) return 64;
    struct timespec t = {.tv_sec = seconds, .tv_nsec = 0};
    if (clock_settime(CLOCK_REALTIME, &t)) { perror("clock_settime"); return 1; }
  } else if (argc != 2 || (strcmp(argv[1], "clock") && strcmp(argv[1], "ranks"))) {
    return 64;
  }
  char host[128];
  if (gethostname(host, sizeof(host))) return 1;
  host[sizeof(host) - 1] = 0;
  if (!strcmp(argv[1], "ranks")) {
    int rank, size;
    if (MPI_Init(&argc, &argv) != MPI_SUCCESS) return 1;
    MPI_Comm_rank(MPI_COMM_WORLD, &rank);
    MPI_Comm_size(MPI_COMM_WORLD, &size);
    MPI_Barrier(MPI_COMM_WORLD);
    uint64_t begin = ns(CLOCK_REALTIME), mono = ns(CLOCK_MONOTONIC);
    struct timespec delay = {.tv_sec = 2, .tv_nsec = 0};
    while (nanosleep(&delay, &delay) && errno == EINTR) {}
    MPI_Barrier(MPI_COMM_WORLD);
    printf("HF3FS_IO500_PROBE rank=%d size=%d host=%s pid=%ld begin_ns=%" PRIu64
           " end_ns=%" PRIu64 " monotonic_begin_ns=%" PRIu64 " monotonic_end_ns=%" PRIu64 "\n",
           rank, size, host, (long)getpid(), begin, ns(CLOCK_REALTIME), mono, ns(CLOCK_MONOTONIC));
    fflush(stdout);
    return MPI_Finalize() == MPI_SUCCESS ? 0 : 1;
  }
  uint64_t before = ns(CLOCK_MONOTONIC);
  uint64_t realtime = ns(CLOCK_REALTIME);
  uint64_t after = ns(CLOCK_MONOTONIC);
  printf("HF3FS_IO500_CLOCK host=%s realtime_ns=%" PRIu64 " monotonic_before_ns=%" PRIu64
         " monotonic_after_ns=%" PRIu64 "\n", host, realtime, before, after);
  return 0;
}
