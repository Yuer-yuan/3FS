#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#define DEFAULT_HEARTBEAT_MS 30000
#define PROC_BUFFER_BYTES 4096

struct process_sample {
  char state;
  char wchan[128];
  uint64_t utime_ticks;
  uint64_t stime_ticks;
  uint64_t voluntary_ctxt;
  uint64_t nonvoluntary_ctxt;
  uint64_t rchar;
  uint64_t wchar;
  uint64_t syscr;
  uint64_t syscw;
  uint64_t read_bytes;
  uint64_t write_bytes;
};

static int write_all(int fd, const char *buffer, size_t length) {
  while (length != 0) {
    ssize_t written = write(fd, buffer, length);
    if (written < 0) {
      if (errno == EINTR) {
        continue;
      }
      return -1;
    }
    buffer += written;
    length -= (size_t)written;
  }
  return 0;
}

static uint64_t monotonic_ms(void) {
  struct timespec now;
  if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
    return 0;
  }
  return (uint64_t)now.tv_sec * 1000 + (uint64_t)now.tv_nsec / 1000000;
}

static ssize_t read_text_file(const char *path, char *buffer, size_t capacity) {
  int fd = open(path, O_RDONLY | O_CLOEXEC);
  if (fd < 0) {
    return -1;
  }
  size_t total = 0;
  while (total + 1 < capacity) {
    ssize_t count = read(fd, buffer + total, capacity - total - 1);
    if (count > 0) {
      total += (size_t)count;
      continue;
    }
    if (count < 0 && errno == EINTR) {
      continue;
    }
    if (count < 0) {
      total = 0;
    }
    break;
  }
  close(fd);
  buffer[total] = '\0';
  return total == 0 ? -1 : (ssize_t)total;
}

static uint64_t named_value(const char *text, const char *name) {
  size_t length = strlen(name);
  const char *line = text;
  while (line != NULL && *line != '\0') {
    if (strncmp(line, name, length) == 0 && line[length] == ':') {
      char *end = NULL;
      unsigned long long value = strtoull(line + length + 1, &end, 10);
      if (end != line + length + 1) {
        return (uint64_t)value;
      }
    }
    line = strchr(line, '\n');
    if (line != NULL) {
      ++line;
    }
  }
  return 0;
}

static void sample_process(pid_t pid, struct process_sample *sample) {
  memset(sample, 0, sizeof(*sample));
  sample->state = '?';
  strcpy(sample->wchan, "unavailable");

  char path[64];
  char text[PROC_BUFFER_BYTES];
  snprintf(path, sizeof(path), "/proc/%ld/stat", (long)pid);
  if (read_text_file(path, text, sizeof(text)) > 0) {
    char *fields = strrchr(text, ')');
    if (fields != NULL && fields[1] == ' ') {
      fields += 2;
      char *save = NULL;
      char *token = strtok_r(fields, " ", &save);
      int field = 3;
      while (token != NULL) {
        if (field == 3 && token[0] != '\0') {
          sample->state = token[0];
        } else if (field == 14) {
          sample->utime_ticks = strtoull(token, NULL, 10);
        } else if (field == 15) {
          sample->stime_ticks = strtoull(token, NULL, 10);
          break;
        }
        token = strtok_r(NULL, " ", &save);
        ++field;
      }
    }
  }

  snprintf(path, sizeof(path), "/proc/%ld/wchan", (long)pid);
  if (read_text_file(path, text, sizeof(text)) > 0) {
    text[strcspn(text, "\r\n\t ")] = '\0';
    if (text[0] != '\0') {
      strncpy(sample->wchan, text, sizeof(sample->wchan) - 1);
      sample->wchan[sizeof(sample->wchan) - 1] = '\0';
    }
  }

  snprintf(path, sizeof(path), "/proc/%ld/status", (long)pid);
  if (read_text_file(path, text, sizeof(text)) > 0) {
    sample->voluntary_ctxt = named_value(text, "voluntary_ctxt_switches");
    sample->nonvoluntary_ctxt = named_value(text, "nonvoluntary_ctxt_switches");
  }

  snprintf(path, sizeof(path), "/proc/%ld/io", (long)pid);
  if (read_text_file(path, text, sizeof(text)) > 0) {
    sample->rchar = named_value(text, "rchar");
    sample->wchar = named_value(text, "wchar");
    sample->syscr = named_value(text, "syscr");
    sample->syscw = named_value(text, "syscw");
    sample->read_bytes = named_value(text, "read_bytes");
    sample->write_bytes = named_value(text, "write_bytes");
  }
}

static int heartbeat_interval_ms(void) {
  const char *configured = getenv("HF3FS_IO500_HEARTBEAT_MS");
  if (configured == NULL || *configured == '\0') {
    return DEFAULT_HEARTBEAT_MS;
  }
  char *end = NULL;
  unsigned long value = strtoul(configured, &end, 10);
  if (*end != '\0' || value < 10 || value > 600000) {
    return DEFAULT_HEARTBEAT_MS;
  }
  return (int)value;
}

static int emit_heartbeat(pid_t child, uint64_t started_ms,
                          uint64_t output_bytes) {
  const char *rank = getenv("HF3FS_IO500_RANK");
  if (rank == NULL || *rank == '\0') {
    rank = "unknown";
  }
  struct process_sample sample;
  sample_process(child, &sample);
  uint64_t now_ms = monotonic_ms();
  char line[1024];
  int length = snprintf(
      line, sizeof(line),
      "HF3FS_IO500_HEARTBEAT rank=%s pid=%ld elapsed_ms=%llu "
      "output_bytes=%llu state=%c wchan=%s utime_ticks=%llu "
      "stime_ticks=%llu voluntary_ctxt=%llu nonvoluntary_ctxt=%llu "
      "rchar=%llu wchar=%llu syscr=%llu syscw=%llu read_bytes=%llu "
      "write_bytes=%llu\n",
      rank, (long)child, (unsigned long long)(now_ms - started_ms),
      (unsigned long long)output_bytes, sample.state, sample.wchan,
      (unsigned long long)sample.utime_ticks,
      (unsigned long long)sample.stime_ticks,
      (unsigned long long)sample.voluntary_ctxt,
      (unsigned long long)sample.nonvoluntary_ctxt,
      (unsigned long long)sample.rchar, (unsigned long long)sample.wchar,
      (unsigned long long)sample.syscr, (unsigned long long)sample.syscw,
      (unsigned long long)sample.read_bytes,
      (unsigned long long)sample.write_bytes);
  if (length < 0 || (size_t)length >= sizeof(line)) {
    errno = EOVERFLOW;
    return -1;
  }
  return write_all(STDOUT_FILENO, line, (size_t)length);
}

int main(int argc, char **argv) {
  if (argc < 2) {
    fprintf(stderr, "usage: %s PROGRAM [ARG ...]\n", argv[0]);
    return 64;
  }

  int master = posix_openpt(O_RDWR | O_NOCTTY | O_CLOEXEC);
  if (master < 0 || grantpt(master) != 0 || unlockpt(master) != 0) {
    perror("io500-linebuf: allocate pty");
    return 70;
  }
  char *slave_name = ptsname(master);
  if (slave_name == NULL) {
    perror("io500-linebuf: resolve pty");
    close(master);
    return 70;
  }
  int slave = open(slave_name, O_RDWR | O_NOCTTY);
  if (slave < 0) {
    perror("io500-linebuf: open pty");
    close(master);
    return 70;
  }

  struct termios attributes;
  if (tcgetattr(slave, &attributes) == 0) {
    cfmakeraw(&attributes);
    if (tcsetattr(slave, TCSANOW, &attributes) != 0) {
      perror("io500-linebuf: configure pty");
      close(slave);
      close(master);
      return 70;
    }
  }

  pid_t child = fork();
  if (child < 0) {
    perror("io500-linebuf: fork");
    close(slave);
    close(master);
    return 70;
  }
  if (child == 0) {
    close(master);
    if (dup2(slave, STDOUT_FILENO) < 0 || dup2(slave, STDERR_FILENO) < 0) {
      perror("io500-linebuf: connect pty");
      _exit(126);
    }
    if (slave != STDOUT_FILENO && slave != STDERR_FILENO) {
      close(slave);
    }
    execvp(argv[1], &argv[1]);
    int exec_error = errno;
    perror("io500-linebuf: exec");
    _exit(exec_error == ENOENT ? 127 : 126);
  }

  close(slave);
  int relay_error = 0;
  uint64_t output_bytes = 0;
  uint64_t started_ms = monotonic_ms();
  uint64_t next_heartbeat_ms =
      started_ms + (uint64_t)heartbeat_interval_ms();
  for (;;) {
    uint64_t before_poll_ms = monotonic_ms();
    int timeout_ms =
        before_poll_ms >= next_heartbeat_ms
            ? 0
            : (int)(next_heartbeat_ms - before_poll_ms);
    struct pollfd descriptor = {
        .fd = master,
        .events = POLLIN | POLLHUP,
        .revents = 0,
    };
    int ready = poll(&descriptor, 1, timeout_ms);
    if (ready < 0) {
      if (errno == EINTR) {
        continue;
      }
      perror("io500-linebuf: poll output");
      relay_error = 1;
      break;
    }
    uint64_t after_poll_ms = monotonic_ms();
    if (after_poll_ms >= next_heartbeat_ms) {
      if (emit_heartbeat(child, started_ms, output_bytes) != 0) {
        perror("io500-linebuf: emit heartbeat");
        relay_error = 1;
        break;
      }
      next_heartbeat_ms =
          after_poll_ms + (uint64_t)heartbeat_interval_ms();
    }
    if (ready == 0) {
      continue;
    }
    char buffer[16384];
    ssize_t received = read(master, buffer, sizeof(buffer));
    if (received > 0) {
      if (write_all(STDOUT_FILENO, buffer, (size_t)received) != 0) {
        perror("io500-linebuf: relay output");
        relay_error = 1;
        break;
      }
      output_bytes += (uint64_t)received;
      continue;
    }
    if (received == 0 || errno == EIO) {
      break;
    }
    if (errno != EINTR) {
      perror("io500-linebuf: read output");
      relay_error = 1;
      break;
    }
  }
  close(master);

  int status = 0;
  while (waitpid(child, &status, 0) < 0) {
    if (errno != EINTR) {
      perror("io500-linebuf: wait");
      return 70;
    }
  }
  if (relay_error) {
    return 74;
  }
  if (WIFEXITED(status)) {
    return WEXITSTATUS(status);
  }
  if (WIFSIGNALED(status)) {
    return 128 + WTERMSIG(status);
  }
  return 70;
}
