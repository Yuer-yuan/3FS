add_library(apache_arrow_static INTERFACE)
add_library(arrow_static STATIC IMPORTED)
add_library(parquet_static STATIC IMPORTED)
add_library(arrow_dependencies STATIC IMPORTED)

set(PREFIX "${CMAKE_CURRENT_BINARY_DIR}")
set(ARROW_RELEASE_BUILD_DIR "${CMAKE_CURRENT_BINARY_DIR}/src/apache-arrow-cpp/cpp/build/release")

# https://cmake.org/cmake/help/latest/policy/CMP0097.html
# Starting with CMake 3.16, explicitly setting GIT_SUBMODULES to an empty string
# means no submodules will be initialized or updated.
cmake_policy(SET CMP0097 NEW)

if(CMAKE_VERSION VERSION_GREATER_EQUAL 3.28)
    set(ARROW_BUILD_PARALLEL "")
else()
    set(ARROW_BUILD_PARALLEL "-j8")
endif()

include(ExternalProject)
if(CMAKE_CROSSCOMPILING)
  set(HF3FS_ARROW_SOURCE_DIR "" CACHE PATH "Pre-provisioned pinned Arrow tree with offline archives")
  if(NOT IS_ABSOLUTE "${HF3FS_ARROW_SOURCE_DIR}" OR
     NOT EXISTS "${HF3FS_ARROW_SOURCE_DIR}/cpp/thirdparty/export.sh")
    message(FATAL_ERROR "Cross Arrow requires HF3FS_ARROW_SOURCE_DIR and its offline archive manifest")
  endif()
  execute_process(COMMAND git -C "${HF3FS_ARROW_SOURCE_DIR}" rev-parse HEAD
    OUTPUT_VARIABLE HF3FS_ARROW_COMMIT OUTPUT_STRIP_TRAILING_WHITESPACE COMMAND_ERROR_IS_FATAL ANY)
  if(NOT HF3FS_ARROW_COMMIT STREQUAL "b7d2f7ffca66c868bd2fce5b3749c6caa002a7f0")
    message(FATAL_ERROR "Pre-provisioned Arrow commit differs from the dependency contract")
  endif()
  # Read assignments as data, without executing an export script or allowing
  # upstream dependency URLs to trigger a network fallback.
  file(STRINGS "${HF3FS_ARROW_SOURCE_DIR}/cpp/thirdparty/export.sh" HF3FS_ARROW_EXPORTS)
  set(HF3FS_ARROW_ENV)
  foreach(assignment IN LISTS HF3FS_ARROW_EXPORTS)
    if(assignment MATCHES "^export (ARROW_[A-Z0-9_]+_URL)=(.+)$")
      set(variable "${CMAKE_MATCH_1}")
      set(archive "${CMAKE_MATCH_2}")
      if(NOT IS_ABSOLUTE "${archive}" OR NOT EXISTS "${archive}")
        message(FATAL_ERROR "Missing pre-provisioned Arrow archive: ${variable}")
      endif()
      list(APPEND HF3FS_ARROW_ENV "${variable}=${archive}")
    endif()
  endforeach()
  if(NOT HF3FS_ARROW_ENV)
    message(FATAL_ERROR "Arrow offline archive manifest is empty")
  endif()
  set(HF3FS_ARROW_BINARY_DIR "${CMAKE_CURRENT_BINARY_DIR}/arrow-cross")
  set(ARROW_RELEASE_BUILD_DIR "${HF3FS_ARROW_BINARY_DIR}/release")
  ExternalProject_Add(apache-arrow-cpp
    PREFIX "${PREFIX}/arrow-cross-project"
    SOURCE_DIR "${HF3FS_ARROW_SOURCE_DIR}"
    BINARY_DIR "${HF3FS_ARROW_BINARY_DIR}"
    DOWNLOAD_COMMAND ""
    UPDATE_COMMAND ""
    CONFIGURE_COMMAND ${CMAKE_COMMAND} -E env ${HF3FS_ARROW_ENV}
      ${CMAKE_COMMAND} -S "${HF3FS_ARROW_SOURCE_DIR}/cpp" -B "${HF3FS_ARROW_BINARY_DIR}" -G Ninja
      -DCMAKE_TOOLCHAIN_FILE=${CMAKE_TOOLCHAIN_FILE}
      -DHF3FS_RISCV_SYSROOT=${CMAKE_SYSROOT}
      -DCMAKE_SYSROOT=${CMAKE_SYSROOT}
      -DCMAKE_C_COMPILER=${CMAKE_C_COMPILER}
      -DCMAKE_CXX_COMPILER=${CMAKE_CXX_COMPILER}
      -DCMAKE_BUILD_TYPE=Release -DARROW_USE_CCACHE=OFF -DARROW_USE_SCCACHE=OFF
      -DARROW_DEPENDENCY_SOURCE=BUNDLED -DARROW_BUILD_STATIC=ON -DARROW_JEMALLOC=ON
      -DARROW_SIMD_LEVEL=NONE -DARROW_RUNTIME_SIMD_LEVEL=NONE -DARROW_BUILD_EXAMPLES=OFF
      -DARROW_PARQUET=ON -DARROW_CSV=ON -DARROW_WITH_ZSTD=ON -DARROW_WITH_LZ4=ON -DARROW_WITH_ZLIB=ON
      -DZLIB_SOURCE=SYSTEM -DARROW_ZLIB_USE_SHARED=OFF
      -DZLIB_LIBRARY=${CMAKE_SYSROOT}/usr/lib/riscv64-linux-gnu/libz.a
      -DZLIB_INCLUDE_DIR=${CMAKE_SYSROOT}/usr/include
    BUILD_COMMAND ${CMAKE_COMMAND} -E env ${HF3FS_ARROW_ENV}
      ${CMAKE_COMMAND} --build "${HF3FS_ARROW_BINARY_DIR}" --parallel 8
    INSTALL_COMMAND ${CMAKE_COMMAND} --install "${HF3FS_ARROW_BINARY_DIR}" --prefix "${PREFIX}"
    BUILD_BYPRODUCTS "${ARROW_RELEASE_BUILD_DIR}/libarrow.a"
      "${ARROW_RELEASE_BUILD_DIR}/libparquet.a"
      "${ARROW_RELEASE_BUILD_DIR}/libarrow_bundled_dependencies.a")
else()
ExternalProject_Add(
    apache-arrow-cpp
    PREFIX ${PREFIX}
    GIT_REPOSITORY https://github.com/apache/arrow.git
    GIT_TAG b7d2f7ffca66c868bd2fce5b3749c6caa002a7f0
    GIT_SHALLOW ON
    GIT_PROGRESS ON
    GIT_SUBMODULES ""
    SOURCE_SUBDIR "cpp"
    BUILD_IN_SOURCE ON
    INSTALL_DIR ${PREFIX}
    CONFIGURE_COMMAND bash -x -c "\
    ( cd thirdparty && [[ -f export.sh ]] || ./download_dependencies.sh | tee export.sh ) && \
    source thirdparty/export.sh && cmake -S . -B . \
        -DCMAKE_BUILD_TYPE=Release \
        -DARROW_USE_CCACHE=OFF \
        -DARROW_USE_SCCACHE=OFF \
        -DARROW_DEPENDENCY_SOURCE=BUNDLED \
        -DARROW_BUILD_STATIC=ON \
        -DARROW_JEMALLOC=ON \
        -DARROW_SIMD_LEVEL=DEFAULT \
        -DARROW_BUILD_EXAMPLES=OFF \
        -DARROW_PARQUET=ON -DARROW_CSV=ON \
        -DARROW_WITH_ZSTD=ON -DARROW_WITH_LZ4=ON -DARROW_WITH_ZLIB=ON"
    BUILD_COMMAND bash -x -c "source thirdparty/export.sh && cmake --build . ${ARROW_BUILD_PARALLEL}"
    BUILD_JOB_SERVER_AWARE 1
    INSTALL_COMMAND cmake --install . --prefix "${PREFIX}"
    BUILD_BYPRODUCTS
        "${ARROW_RELEASE_BUILD_DIR}/libarrow.a"
        "${ARROW_RELEASE_BUILD_DIR}/libparquet.a"
        "${ARROW_RELEASE_BUILD_DIR}/libarrow_bundled_dependencies.a"
  )
endif()

add_dependencies(arrow_static apache-arrow-cpp)
add_dependencies(parquet_static apache-arrow-cpp)
add_dependencies(arrow_dependencies apache-arrow-cpp)
set_target_properties(arrow_static PROPERTIES IMPORTED_LOCATION "${ARROW_RELEASE_BUILD_DIR}/libarrow.a")
set_target_properties(parquet_static PROPERTIES IMPORTED_LOCATION "${ARROW_RELEASE_BUILD_DIR}/libparquet.a")
set_target_properties(arrow_dependencies PROPERTIES IMPORTED_LOCATION "${ARROW_RELEASE_BUILD_DIR}/libarrow_bundled_dependencies.a")
target_include_directories(apache_arrow_static SYSTEM INTERFACE "${PREFIX}/include")
target_link_libraries(apache_arrow_static INTERFACE parquet_static arrow_static arrow_dependencies)
if(CMAKE_CROSSCOMPILING)
  # The locked target zlib avoids the old bundled version's lld-incompatible
  # shared-library version script. It is external to Arrow's bundled archive.
  target_link_libraries(apache_arrow_static INTERFACE "${CMAKE_SYSROOT}/usr/lib/riscv64-linux-gnu/libz.a")
endif()
