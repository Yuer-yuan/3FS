#include <arpa/inet.h>
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <sys/socket.h>
#include <thread>
#include <unistd.h>

#include "CxlTestProcess.h"

int main(int argc, char **argv) {
  using namespace hf3fs::test;
  using namespace std::chrono_literals;
  if (argc != 3) return 2;
  const int fd = std::atoi(argv[1]);
  const std::string mode = argv[2];
  if (mode == "fail-start") return 42;
  if (mode != "echo" && mode != "fragmented" && mode != "exit-on-request" && mode != "bad-frame" && mode != "stall" &&
      mode != "callback")
    return 2;
  CxlTestChannel channel(fd);
  try {
    channel.send(CxlTestProcess::kReady, std::chrono::steady_clock::now() + 5s);
    while (true) {
      const auto message = channel.receive(std::chrono::steady_clock::now() + 30s);
      if (message == "stop") {
        channel.send("stopped", std::chrono::steady_clock::now() + 5s);
        return 0;
      }
      if (mode == "exit-on-request") return 23;
      if (mode == "stall") {
        std::this_thread::sleep_for(30s);
        return 3;
      }
      if (mode == "bad-frame") {
        const uint32_t size = htonl(CxlTestChannel::kMaxFrameBytes + 1U);
        return ::send(fd, &size, sizeof(size), MSG_NOSIGNAL) == static_cast<ssize_t>(sizeof(size)) ? 0 : 4;
      }
      if (mode == "callback") {
        const auto deadline = std::chrono::steady_clock::now() + 5s;
        channel.send("before", deadline);
        const auto updated = channel.receive(deadline);
        channel.send(updated == "after" ? "committed" : "rejected", deadline);
      } else if (mode == "fragmented") {
        const std::string reply = "fragmented-reply";
        const uint32_t size = htonl(reply.size());
        std::string frame(reinterpret_cast<const char *>(&size), sizeof(size));
        frame += reply;
        for (char byte : frame) {
          if (::send(fd, &byte, 1, MSG_NOSIGNAL) != 1) return 5;
          std::this_thread::sleep_for(1ms);
        }
      } else {
        channel.send(message, std::chrono::steady_clock::now() + 5s);
      }
    }
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
