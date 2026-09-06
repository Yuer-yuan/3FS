#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <termios.h>
#include <unistd.h>

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
  for (;;) {
    char buffer[16384];
    ssize_t received = read(master, buffer, sizeof(buffer));
    if (received > 0) {
      if (write_all(STDOUT_FILENO, buffer, (size_t)received) != 0) {
        perror("io500-linebuf: relay output");
        relay_error = 1;
        break;
      }
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
