target_add_bin(storage_bench "StorageBench.cc" test-fabric-lib storage-client storage memory-common follybenchmark gmock fdb mgmtd)
add_dependencies(storage_bench cxl_storage_test_helper)
