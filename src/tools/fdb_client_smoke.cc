#define FDB_API_VERSION 710
#include <atomic>
#include <bit>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <folly/experimental/coro/BlockingWait.h>
#include <foundationdb/fdb_c.h>
#include <iostream>
#include <limits>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <thread>
#include <utility>

#include "fdb/FDB.h"

namespace {

static_assert(std::endian::native == std::endian::little);

constexpr std::string_view kKeyPrefix{"\xff\x02/hf3fs-cxl-g0/"};
constexpr std::int64_t kTransactionTimeoutMs = 30'000;

struct Arguments {
  std::string clusterFile;
  std::string key;
  std::string value;
};

struct Result {
  bool setOk = false;
  bool getOk = false;
  bool valueMatch = false;
  bool callbackSetOk = false;
  bool callbackCalled = false;
  bool callbackGetOk = false;
  bool callbackValueMatch = false;
  std::int64_t callbackElapsedMs = 0;
  bool wrapperDatabaseOk = false;
  bool wrapperTransactionOk = false;
  bool wrapperGetOk = false;
  bool wrapperValueMatch = false;
  std::int64_t wrapperElapsedMs = 0;
  fdb_error_t error = 0;
  std::string stage = "none";
};

struct CallbackState {
  std::mutex mutex;
  std::condition_variable condition;
  bool called = false;
};

// The probe registers one callback. Process-lifetime storage keeps its context
// valid even when a broken callback path forces the bounded wait to time out.
CallbackState callbackState;

void futureReady(FDBFuture *, void *parameter) {
  auto *state = static_cast<CallbackState *>(parameter);
  {
    std::lock_guard lock(state->mutex);
    state->called = true;
  }
  state->condition.notify_one();
}

struct FutureDeleter {
  void operator()(FDBFuture *future) const {
    if (future != nullptr) {
      fdb_future_destroy(future);
    }
  }
};

struct DatabaseDeleter {
  void operator()(FDBDatabase *database) const {
    if (database != nullptr) {
      fdb_database_destroy(database);
    }
  }
};

struct TransactionDeleter {
  void operator()(FDBTransaction *transaction) const {
    if (transaction != nullptr) {
      fdb_transaction_destroy(transaction);
    }
  }
};

using Future = std::unique_ptr<FDBFuture, FutureDeleter>;
using Database = std::unique_ptr<FDBDatabase, DatabaseDeleter>;
using Transaction = std::unique_ptr<FDBTransaction, TransactionDeleter>;

class NetworkRuntime {
 public:
  NetworkRuntime() = default;
  NetworkRuntime(const NetworkRuntime &) = delete;
  NetworkRuntime &operator=(const NetworkRuntime &) = delete;

  ~NetworkRuntime() { (void)stop(); }

  fdb_error_t start() {
    fdb_error_t error = fdb_setup_network();
    if (error != 0) {
      return error;
    }
    running_ = true;
    thread_ = std::thread([this] { networkError_.store(fdb_run_network(), std::memory_order_release); });
    return 0;
  }

  fdb_error_t stop() {
    if (!running_) {
      return 0;
    }
    fdb_error_t stopError = fdb_stop_network();
    if (thread_.joinable()) {
      thread_.join();
    }
    running_ = false;
    fdb_error_t runError = networkError_.load(std::memory_order_acquire);
    return stopError != 0 ? stopError : runError;
  }

 private:
  std::thread thread_;
  std::atomic<fdb_error_t> networkError_{0};
  bool running_ = false;
};

fdb_error_t waitFuture(FDBFuture *future) {
  if (future == nullptr) {
    return -1;
  }
  fdb_error_t error = fdb_future_block_until_ready(future);
  return error != 0 ? error : fdb_future_get_error(future);
}

fdb_error_t configureTransaction(FDBTransaction *transaction) {
  fdb_error_t error = fdb_transaction_set_option(transaction, FDB_TR_OPTION_ACCESS_SYSTEM_KEYS, nullptr, 0);
  if (error != 0) {
    return error;
  }
  return fdb_transaction_set_option(transaction,
                                    FDB_TR_OPTION_TIMEOUT,
                                    reinterpret_cast<const std::uint8_t *>(&kTransactionTimeoutMs),
                                    sizeof(kTransactionTimeoutMs));
}

Result failure(std::string stage, fdb_error_t error) {
  Result result;
  result.stage = std::move(stage);
  result.error = error;
  return result;
}

Result runTransaction(const Arguments &arguments) {
  fdb_error_t error = fdb_select_api_version(FDB_API_VERSION);
  if (error != 0) {
    return failure("select_api", error);
  }

  NetworkRuntime network;
  error = network.start();
  if (error != 0) {
    return failure("setup_network", error);
  }

  FDBDatabase *databaseRaw = nullptr;
  error = fdb_create_database(arguments.clusterFile.c_str(), &databaseRaw);
  Database database(databaseRaw);
  if (error != 0 || database == nullptr) {
    return failure("create_database", error != 0 ? error : -1);
  }

  std::string key(kKeyPrefix);
  key.append(arguments.key);
  if (key.size() > static_cast<std::size_t>(std::numeric_limits<int>::max()) ||
      arguments.value.size() > static_cast<std::size_t>(std::numeric_limits<int>::max())) {
    return failure("input_too_large", -1);
  }

  FDBTransaction *writeRaw = nullptr;
  error = fdb_database_create_transaction(database.get(), &writeRaw);
  Transaction writeTransaction(writeRaw);
  if (error != 0 || writeTransaction == nullptr) {
    return failure("create_write_transaction", error != 0 ? error : -1);
  }
  error = configureTransaction(writeTransaction.get());
  if (error != 0) {
    return failure("configure_write_transaction", error);
  }
  fdb_transaction_set(writeTransaction.get(),
                      reinterpret_cast<const std::uint8_t *>(key.data()),
                      static_cast<int>(key.size()),
                      reinterpret_cast<const std::uint8_t *>(arguments.value.data()),
                      static_cast<int>(arguments.value.size()));
  Future commit(fdb_transaction_commit(writeTransaction.get()));
  error = waitFuture(commit.get());
  if (error != 0) {
    return failure("commit", error);
  }

  Result result;
  result.setOk = true;
  FDBTransaction *readRaw = nullptr;
  error = fdb_database_create_transaction(database.get(), &readRaw);
  Transaction readTransaction(readRaw);
  if (error != 0 || readTransaction == nullptr) {
    result.stage = "create_read_transaction";
    result.error = error != 0 ? error : -1;
    return result;
  }
  error = configureTransaction(readTransaction.get());
  if (error != 0) {
    result.stage = "configure_read_transaction";
    result.error = error;
    return result;
  }
  Future read(fdb_transaction_get(readTransaction.get(),
                                  reinterpret_cast<const std::uint8_t *>(key.data()),
                                  static_cast<int>(key.size()),
                                  false));
  error = waitFuture(read.get());
  if (error != 0) {
    result.stage = "read";
    result.error = error;
    return result;
  }
  fdb_bool_t present = false;
  const std::uint8_t *value = nullptr;
  int valueLength = 0;
  error = fdb_future_get_value(read.get(), &present, &value, &valueLength);
  if (error != 0) {
    result.stage = "read_value";
    result.error = error;
    return result;
  }
  result.getOk = present;
  result.valueMatch = present && valueLength == static_cast<int>(arguments.value.size()) &&
                      std::string_view(reinterpret_cast<const char *>(value), valueLength) == arguments.value;
  if (!result.valueMatch) {
    result.stage = present ? "value_mismatch" : "value_absent";
    return result;
  }

  FDBTransaction *callbackRaw = nullptr;
  error = fdb_database_create_transaction(database.get(), &callbackRaw);
  Transaction callbackTransaction(callbackRaw);
  if (error != 0 || callbackTransaction == nullptr) {
    result.stage = "create_callback_transaction";
    result.error = error != 0 ? error : -1;
    return result;
  }
  error = configureTransaction(callbackTransaction.get());
  if (error != 0) {
    result.stage = "configure_callback_transaction";
    result.error = error;
    return result;
  }
  Future callbackRead(fdb_transaction_get(callbackTransaction.get(),
                                          reinterpret_cast<const std::uint8_t *>(key.data()),
                                          static_cast<int>(key.size()),
                                          true));
  auto callbackBegin = std::chrono::steady_clock::now();
  error = fdb_future_set_callback(callbackRead.get(), futureReady, &callbackState);
  if (error != 0) {
    result.stage = "set_callback";
    result.error = error;
    return result;
  }
  result.callbackSetOk = true;
  {
    std::unique_lock lock(callbackState.mutex);
    result.callbackCalled =
        callbackState.condition.wait_for(lock, std::chrono::seconds(35), [] { return callbackState.called; });
  }
  result.callbackElapsedMs =
      std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - callbackBegin).count();
  if (!result.callbackCalled) {
    fdb_future_cancel(callbackRead.get());
    result.stage = "callback_timeout";
    result.error = -2;
    return result;
  }
  error = fdb_future_get_error(callbackRead.get());
  if (error != 0) {
    result.stage = "callback_read";
    result.error = error;
    return result;
  }
  present = false;
  value = nullptr;
  valueLength = 0;
  error = fdb_future_get_value(callbackRead.get(), &present, &value, &valueLength);
  if (error != 0) {
    result.stage = "callback_read_value";
    result.error = error;
    return result;
  }
  result.callbackGetOk = present;
  result.callbackValueMatch = present && valueLength == static_cast<int>(arguments.value.size()) &&
                              std::string_view(reinterpret_cast<const char *>(value), valueLength) == arguments.value;
  if (!result.callbackValueMatch) {
    result.stage = present ? "callback_value_mismatch" : "callback_value_absent";
    return result;
  }

  {
    auto wrapperBegin = std::chrono::steady_clock::now();
    hf3fs::kv::fdb::DB wrapperDatabase(arguments.clusterFile, true);
    result.wrapperDatabaseOk = wrapperDatabase.error() == 0;
    if (!result.wrapperDatabaseOk) {
      result.stage = "wrapper_create_database";
      result.error = wrapperDatabase.error();
      return result;
    }
    hf3fs::kv::fdb::Transaction wrapperTransaction(wrapperDatabase);
    result.wrapperTransactionOk = wrapperTransaction.error() == 0;
    if (!result.wrapperTransactionOk) {
      result.stage = "wrapper_create_transaction";
      result.error = wrapperTransaction.error();
      return result;
    }
    error = wrapperTransaction.setOption(FDB_TR_OPTION_ACCESS_SYSTEM_KEYS);
    if (error != 0) {
      result.stage = "wrapper_configure_transaction";
      result.error = error;
      return result;
    }
    auto wrapperRead = folly::coro::blockingWait(wrapperTransaction.get(key, true));
    result.wrapperElapsedMs =
        std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - wrapperBegin).count();
    result.wrapperGetOk = wrapperRead.error() == 0 && wrapperRead.value().has_value();
    result.wrapperValueMatch = result.wrapperGetOk && *wrapperRead.value() == arguments.value;
    if (wrapperRead.error() != 0) {
      result.stage = "wrapper_read";
      result.error = wrapperRead.error();
      return result;
    }
    if (!result.wrapperValueMatch) {
      result.stage = result.wrapperGetOk ? "wrapper_value_mismatch" : "wrapper_value_absent";
      return result;
    }
  }

  callbackRead.reset();
  callbackTransaction.reset();
  read.reset();
  readTransaction.reset();
  commit.reset();
  writeTransaction.reset();
  database.reset();
  error = network.stop();
  if (error != 0) {
    result.stage = "stop_network";
    result.error = error;
    return result;
  }
  result.stage = "complete";
  return result;
}

std::string jsonEscape(std::string_view input) {
  static constexpr char kHex[] = "0123456789abcdef";
  std::string output;
  for (unsigned char value : input) {
    switch (value) {
      case '\\':
        output += "\\\\";
        break;
      case '"':
        output += "\\\"";
        break;
      case '\n':
        output += "\\n";
        break;
      case '\r':
        output += "\\r";
        break;
      case '\t':
        output += "\\t";
        break;
      default:
        if (value < 0x20) {
          output += "\\u00";
          output += kHex[value >> 4];
          output += kHex[value & 0x0f];
        } else {
          output += static_cast<char>(value);
        }
    }
  }
  return output;
}

void printResult(const Result &result) {
  const char *message =
      result.error == 0 ? "success" : (result.error > 0 ? fdb_get_error(result.error) : "internal smoke error");
  bool passed = result.setOk && result.getOk && result.valueMatch && result.callbackSetOk && result.callbackCalled &&
                result.callbackGetOk && result.callbackValueMatch && result.wrapperDatabaseOk &&
                result.wrapperTransactionOk && result.wrapperGetOk && result.wrapperValueMatch && result.error == 0;
  std::cout << "{\"api_version\":" << FDB_API_VERSION << ",\"set_ok\":" << (result.setOk ? "true" : "false")
            << ",\"get_ok\":" << (result.getOk ? "true" : "false")
            << ",\"value_match\":" << (result.valueMatch ? "true" : "false")
            << ",\"callback_set_ok\":" << (result.callbackSetOk ? "true" : "false")
            << ",\"callback_called\":" << (result.callbackCalled ? "true" : "false")
            << ",\"callback_get_ok\":" << (result.callbackGetOk ? "true" : "false")
            << ",\"callback_value_match\":" << (result.callbackValueMatch ? "true" : "false")
            << ",\"callback_elapsed_ms\":" << result.callbackElapsedMs
            << ",\"wrapper_database_ok\":" << (result.wrapperDatabaseOk ? "true" : "false")
            << ",\"wrapper_transaction_ok\":" << (result.wrapperTransactionOk ? "true" : "false")
            << ",\"wrapper_get_ok\":" << (result.wrapperGetOk ? "true" : "false")
            << ",\"wrapper_value_match\":" << (result.wrapperValueMatch ? "true" : "false")
            << ",\"wrapper_elapsed_ms\":" << result.wrapperElapsedMs << ",\"status\":\""
            << (passed ? "passed" : "failed") << "\",\"stage\":\"" << jsonEscape(result.stage)
            << "\",\"error_code\":" << result.error << ",\"error\":\""
            << jsonEscape(message != nullptr ? message : "unknown") << "\"}\n";
}

bool parseArguments(int argc, char **argv, Arguments &arguments) {
  bool clusterSeen = false;
  bool keySeen = false;
  bool valueSeen = false;
  for (int index = 1; index < argc; index += 2) {
    if (index + 1 >= argc) {
      return false;
    }
    std::string_view option(argv[index]);
    std::string value(argv[index + 1]);
    if (option == "--cluster-file" && !clusterSeen) {
      arguments.clusterFile = std::move(value);
      clusterSeen = true;
    } else if (option == "--key" && !keySeen) {
      arguments.key = std::move(value);
      keySeen = true;
    } else if (option == "--value" && !valueSeen) {
      arguments.value = std::move(value);
      valueSeen = true;
    } else {
      return false;
    }
  }
  return clusterSeen && keySeen && valueSeen && !arguments.clusterFile.empty() && !arguments.key.empty();
}

}  // namespace

int main(int argc, char **argv) {
  Arguments arguments;
  if (!parseArguments(argc, argv, arguments)) {
    std::cerr << "usage: fdb_client_smoke --cluster-file PATH --key KEY "
                 "--value VALUE\n";
    return 2;
  }
  Result result = runTransaction(arguments);
  printResult(result);
  return result.setOk && result.getOk && result.valueMatch && result.callbackSetOk && result.callbackCalled &&
                 result.callbackGetOk && result.callbackValueMatch && result.wrapperDatabaseOk &&
                 result.wrapperTransactionOk && result.wrapperGetOk && result.wrapperValueMatch && result.error == 0
             ? EXIT_SUCCESS
             : EXIT_FAILURE;
}
