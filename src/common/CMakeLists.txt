include(${PROJECT_SOURCE_DIR}/cmake/GitVersion.cmake)
CheckGitSetup(${PROJECT_SOURCE_DIR})

set(FLATBUFFERS_FLATC_SCHEMA_EXTRA_ARGS --cpp-std=c++17)

set(COMMON_TRANSPORT_LIBRARIES)
if(CMAKE_CROSSCOMPILING AND CMAKE_SYSTEM_PROCESSOR STREQUAL "riscv64")
  if(NOT EXISTS "${HF3FS_RISCV_COMPILER_RT}")
    message(FATAL_ERROR "RISC-V full build requires verified HF3FS_RISCV_COMPILER_RT")
  endif()
  # Clang lowers checked __int128 multiplication in ClickHouse decimal
  # parsing to __muloti4, which is not supplied by this target's libgcc.
  set_property(TARGET clickhouse-cpp-lib-static APPEND PROPERTY
    INTERFACE_LINK_LIBRARIES "${HF3FS_RISCV_COMPILER_RT}")
endif()
if(HF3FS_ENABLE_RDMA)
  list(APPEND COMMON_TRANSPORT_LIBRARIES ibverbs)
endif()

target_add_lib(common memory-common version-info folly ${COMMON_TRANSPORT_LIBRARIES} scn::scn clickhouse-cpp-lib-static toml11 libzstd_static 3fs_liburing)
add_dependencies(common MonitorCollectorService-fbs)
target_sources(common PRIVATE utils/Linenoise.c)

target_add_shared_lib(hf3fs_common_shared memory-common version-info folly ${COMMON_TRANSPORT_LIBRARIES} scn::scn clickhouse-cpp-lib-static toml11 libzstd_static 3fs_liburing)
add_dependencies(hf3fs_common_shared MonitorCollectorService-fbs)

if(NOT HF3FS_ENABLE_RDMA)
  foreach(COMMON_TARGET common hf3fs_common_shared)
    get_target_property(COMMON_SOURCES ${COMMON_TARGET} SOURCES)
    list(FILTER COMMON_SOURCES EXCLUDE REGEX "^net/ib/")
    set_property(TARGET ${COMMON_TARGET} PROPERTY SOURCES "${COMMON_SOURCES}")
  endforeach()
endif()
