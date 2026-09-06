add_library(jemalloc INTERFACE)
add_library(hf3fs_jemalloc_shared SHARED IMPORTED)

include(ExternalProject)
set(JEMALLOC_DIR "${CMAKE_BINARY_DIR}/third_party/jemalloc")

if(CMAKE_CROSSCOMPILING)
  if(NOT EXISTS "${PROJECT_SOURCE_DIR}/third_party/jemalloc/configure")
    message(FATAL_ERROR "Prepare the pinned jemalloc configure script before the offline cross build")
  endif()
  ExternalProject_add(Hf3fsJemalloc_project
    SOURCE_DIR "${PROJECT_SOURCE_DIR}/third_party/jemalloc"
    BINARY_DIR "${CMAKE_BINARY_DIR}/third_party/jemalloc-build"
    DOWNLOAD_COMMAND ""
    UPDATE_COMMAND ""
    BUILD_BYPRODUCTS "${JEMALLOC_DIR}/include/jemalloc/jemalloc.h"
      "${JEMALLOC_DIR}/lib/libjemalloc.so.2"
    CONFIGURE_COMMAND ${CMAKE_COMMAND} -E env
      "CC=${CMAKE_C_COMPILER}" "AR=${CMAKE_AR}" "RANLIB=${CMAKE_RANLIB}"
      "CFLAGS=--sysroot=${CMAKE_SYSROOT} -O2 -fPIC"
      "${PROJECT_SOURCE_DIR}/third_party/jemalloc/configure"
      --host=riscv64-linux-gnu --prefix=${JEMALLOC_DIR}
      --disable-cxx --enable-prof --disable-initial-exec-tls
    BUILD_COMMAND make -j 6
    INSTALL_COMMAND make install)
else()
ExternalProject_add(Hf3fsJemalloc_project
  SOURCE_DIR "${PROJECT_SOURCE_DIR}/third_party/jemalloc"
  BUILD_BYPRODUCTS "${JEMALLOC_DIR}/include/jemalloc/jemalloc.h"
  "${JEMALLOC_DIR}/lib/libjemalloc.so.2"
  CONFIGURE_COMMAND ./autogen.sh && ./configure --prefix=${JEMALLOC_DIR} --disable-cxx --enable-prof --disable-initial-exec-tls
  BUILD_IN_SOURCE ON
  BUILD_COMMAND make -j 6
  INSTALL_DIR "${JEMALLOC_DIR}"
  INSTALL_COMMAND make install)
endif()

add_dependencies(hf3fs_jemalloc_shared Hf3fsJemalloc_project)
set_target_properties(hf3fs_jemalloc_shared PROPERTIES IMPORTED_LOCATION "${JEMALLOC_DIR}/lib/libjemalloc.so.2")
target_include_directories(hf3fs_jemalloc_shared INTERFACE "${JEMALLOC_DIR}/include")
target_link_libraries(jemalloc INTERFACE hf3fs_jemalloc_shared)
