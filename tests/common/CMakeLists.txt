target_add_test(test_common common fdb mgmtd-fbs cxl-test-process)
add_dependencies(test_common cxl_test_process_helper)
get_target_property(TEST_COMMON_SOURCES test_common SOURCES)
list(FILTER TEST_COMMON_SOURCES EXCLUDE REGEX "net/CxlTransportE2EHelper.cc$")
set_property(TARGET test_common PROPERTY SOURCES "${TEST_COMMON_SOURCES}")

# Focused transport/service-plane gate. Keep this independent of FoundationDB
# so transport refactors can be exercised on machines without the host FDB SDK.
add_executable(test_transport_planes
  net/TestTransportKind.cc
  net/TestTransportEvidence.cc
  net/TestPublicationLedger.cc
  net/TestCxlTransportRetirement.cc
  serde/TestService.cc
  utils/TestStatusCode.cc
)
target_link_libraries(test_transport_planes gmock test_main common mgmtd-fbs)
target_include_directories(test_transport_planes
  PUBLIC
    $<BUILD_INTERFACE:${PROJECT_SOURCE_DIR}/src>
    ${PROJECT_SOURCE_DIR}
    ${PROJECT_BINARY_DIR}/src
    ${PROJECT_BINARY_DIR}
)
set_target_properties(test_transport_planes PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests"
)
add_test(NAME test_transport_planes COMMAND test_transport_planes)

# Exercise RV64's process-local representations on the build host as well.
# Do not link common: its Status representation may differ on this host.
add_executable(test_riscv_portability
  utils/TestStatus.cc
  utils/TestAtomicSharedPtr.cc
  ${PROJECT_SOURCE_DIR}/src/common/utils/StatusCode.cc
)
target_compile_definitions(test_riscv_portability PRIVATE
  HF3FS_PORTABLE_STATUS=1 HF3FS_USE_STD_ATOMIC_SHARED_PTR=1)
target_include_directories(test_riscv_portability PRIVATE ${PROJECT_SOURCE_DIR}/src)
target_link_libraries(test_riscv_portability gmock_main fmt::fmt Threads::Threads)
set_target_properties(test_riscv_portability PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests")
add_test(NAME test_riscv_portability COMMAND test_riscv_portability)

# Simulate an unsigned-char platform on the host. Folly's usage requirements
# must override that default for every consumer of its inline implementation.
add_executable(test_folly_allocator utils/TestFollyAllocator.cc)
target_compile_options(test_folly_allocator PRIVATE -funsigned-char)
target_link_libraries(test_folly_allocator gmock_main folly)
set_target_properties(test_folly_allocator PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests")
add_test(NAME test_folly_allocator COMMAND test_folly_allocator)

# Fixed CXL ABI and mapping tests are deliberately independent of the host
# FoundationDB SDK and require neither a DAX device nor RDMA hardware.
add_executable(test_cxl_layout
  net/cxl/TestCxlAbi.cc
  net/cxl/TestCxlBuffer.cc
  net/cxl/TestCxlConnect.cc
  net/cxl/TestCxlEndpointState.cc
  net/cxl/TestCxlFabric.cc
  net/cxl/TestCxlLane.cc
  net/cxl/TestCxlRegion.cc
  net/cxl/TestCxlSocket.cc
)
target_link_libraries(test_cxl_layout gmock test_main common)
target_include_directories(test_cxl_layout
  PUBLIC
    $<BUILD_INTERFACE:${PROJECT_SOURCE_DIR}/src>
    ${PROJECT_SOURCE_DIR}
    ${PROJECT_BINARY_DIR}/src
    ${PROJECT_BINARY_DIR}
)
set_target_properties(test_cxl_layout PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests"
)
add_test(NAME test_cxl_layout COMMAND test_cxl_layout)

add_executable(cxl_transport_e2e_helper net/CxlTransportE2EHelper.cc)
target_link_libraries(cxl_transport_e2e_helper common)
target_include_directories(cxl_transport_e2e_helper
  PUBLIC
    $<BUILD_INTERFACE:${PROJECT_SOURCE_DIR}/src>
    ${PROJECT_SOURCE_DIR}
    ${PROJECT_BINARY_DIR}/src
    ${PROJECT_BINARY_DIR}
)
set_target_properties(cxl_transport_e2e_helper PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests"
)
add_dependencies(test_common cxl_transport_e2e_helper)

add_executable(test_cxl_transport_e2e net/TestCxlTransportE2E.cc)
target_link_libraries(test_cxl_transport_e2e gmock test_main common)
add_dependencies(test_cxl_transport_e2e cxl_transport_e2e_helper)
target_include_directories(test_cxl_transport_e2e
  PUBLIC
    $<BUILD_INTERFACE:${PROJECT_SOURCE_DIR}/src>
    ${PROJECT_SOURCE_DIR}
    ${PROJECT_BINARY_DIR}/src
    ${PROJECT_BINARY_DIR}
)
set_target_properties(test_cxl_transport_e2e PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests"
)
add_test(NAME test_cxl_transport_e2e COMMAND test_cxl_transport_e2e)

# Keep a dependency-minimal twin for sanitizer runs. Linking all of common
# triggers a pre-main UBSAN finding in Folly's intentional PackedSyncPtr
# representation before any CXL test can execute.
add_executable(test_cxl_layout_minimal
  net/cxl/TestCxlAbi.cc
  net/cxl/TestCxlEndpointState.cc
  net/cxl/TestCxlFabric.cc
  net/cxl/TestCxlLane.cc
  net/cxl/TestCxlRegion.cc
  net/cxl/TestCxlSocket.cc
  ${PROJECT_SOURCE_DIR}/src/common/net/cxl/CxlEndpointState.cc
  ${PROJECT_SOURCE_DIR}/src/common/net/cxl/CxlFabric.cc
  ${PROJECT_SOURCE_DIR}/src/common/net/cxl/CxlLane.cc
  ${PROJECT_SOURCE_DIR}/src/common/net/cxl/CxlLayout.cc
  ${PROJECT_SOURCE_DIR}/src/common/net/cxl/CxlMetrics.cc
  ${PROJECT_SOURCE_DIR}/src/common/net/cxl/CxlProgressEngine.cc
  ${PROJECT_SOURCE_DIR}/src/common/net/cxl/CxlRegion.cc
  ${PROJECT_SOURCE_DIR}/src/common/net/cxl/CxlSocket.cc
  ${PROJECT_SOURCE_DIR}/src/common/utils/StatusCode.cc
)
target_link_libraries(test_cxl_layout_minimal gmock_main fmt::fmt folly scn::scn)
target_compile_definitions(test_cxl_layout_minimal PRIVATE HF3FS_CXL_MINIMAL_TEST=1)
target_include_directories(test_cxl_layout_minimal
  PUBLIC
    $<BUILD_INTERFACE:${PROJECT_SOURCE_DIR}/src>
    ${PROJECT_SOURCE_DIR}
    ${PROJECT_BINARY_DIR}/src
    ${PROJECT_BINARY_DIR}
)
target_include_directories(test_cxl_layout_minimal SYSTEM PUBLIC
    ${PROJECT_SOURCE_DIR}/third_party/folly
    ${PROJECT_BINARY_DIR}/third_party/folly
)
set_target_properties(test_cxl_layout_minimal PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests"
)
add_test(NAME test_cxl_layout_minimal COMMAND test_cxl_layout_minimal)
