cmake_minimum_required(VERSION 3.24)
project(hf3fs_cxl_riscv_smoke LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

get_filename_component(
  HF3FS_DEFAULT_SMOKE_SOURCE_DIR "${CMAKE_CURRENT_LIST_DIR}/../../../src/tools"
  ABSOLUTE)
set(HF3FS_SMOKE_SOURCE_DIR "${HF3FS_DEFAULT_SMOKE_SOURCE_DIR}" CACHE PATH
    "Absolute path to the 3FS smoke sources")
option(HF3FS_BUILD_DAX_SMOKE "Build the CXL DAX publication probe" ON)
option(HF3FS_BUILD_FUSE_SMOKE "Build the FUSE mount probe" ON)
option(HF3FS_BUILD_FDB_SMOKE "Build the FoundationDB transaction probe" ON)

find_package(Threads REQUIRED)

if(HF3FS_BUILD_DAX_SMOKE)
  add_executable(cxl_dax_smoke "${HF3FS_SMOKE_SOURCE_DIR}/cxl_dax_smoke.cc")
  target_link_libraries(cxl_dax_smoke PRIVATE Threads::Threads)
endif()

if(HF3FS_BUILD_FUSE_SMOKE)
  foreach(variable IN ITEMS HF3FS_RISCV_FUSE_INCLUDE HF3FS_RISCV_FUSE_LIBRARY)
    if(NOT DEFINED ${variable} OR NOT IS_ABSOLUTE "${${variable}}")
      message(FATAL_ERROR "${variable} must be absolute")
    endif()
  endforeach()
  add_library(hf3fs_fuse3 SHARED IMPORTED GLOBAL)
  set_target_properties(
    hf3fs_fuse3 PROPERTIES IMPORTED_LOCATION "${HF3FS_RISCV_FUSE_LIBRARY}"
                           INTERFACE_INCLUDE_DIRECTORIES "${HF3FS_RISCV_FUSE_INCLUDE}")
  add_executable(fuse_mount_smoke
                 "${HF3FS_SMOKE_SOURCE_DIR}/fuse_mount_smoke.cc")
  target_link_libraries(fuse_mount_smoke PRIVATE hf3fs_fuse3 Threads::Threads)
endif()

if(HF3FS_BUILD_FDB_SMOKE)
  foreach(variable IN ITEMS HF3FS_RISCV_FDB_INCLUDE HF3FS_RISCV_FDB_CLIENT)
    if(NOT DEFINED ${variable} OR NOT IS_ABSOLUTE "${${variable}}")
      message(FATAL_ERROR "${variable} must be absolute")
    endif()
  endforeach()
  add_library(hf3fs_fdb_c SHARED IMPORTED GLOBAL)
  set_target_properties(
    hf3fs_fdb_c PROPERTIES IMPORTED_LOCATION "${HF3FS_RISCV_FDB_CLIENT}"
                           INTERFACE_INCLUDE_DIRECTORIES "${HF3FS_RISCV_FDB_INCLUDE}")
  add_executable(fdb_client_smoke
                 "${HF3FS_SMOKE_SOURCE_DIR}/fdb_client_smoke.cc")
  target_link_libraries(fdb_client_smoke PRIVATE hf3fs_fdb_c Threads::Threads)
endif()
