add_crate(chunk_engine)

target_add_lib(storage core-app core-service memory-common storage-fbs mgmtd-client storage-client kv analytics
  hf3fs_aio chunk_engine hf3fs_profiler)
target_include_directories(storage PUBLIC ${CMAKE_SOURCE_DIR}/third_party/leveldb)
target_add_bin(storage_main "storage.cpp" storage jemalloc)
