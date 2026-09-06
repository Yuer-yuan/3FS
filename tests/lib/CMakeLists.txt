add_library(cxl-test-process STATIC CxlTestProcess.cpp)
target_include_directories(cxl-test-process PUBLIC ${PROJECT_SOURCE_DIR})
target_link_libraries(cxl-test-process PUBLIC Threads::Threads)

add_executable(cxl_test_process_helper CxlTestProcessHelper.cpp)
target_link_libraries(cxl_test_process_helper cxl-test-process)
set_target_properties(cxl_test_process_helper PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests")

target_add_lib(test-fabric-lib storage-client storage meta-client meta mgmtd-test-helper gtest cxl-test-process)

add_executable(cxl_storage_test_helper CxlStorageTestHelper.cpp)
target_link_libraries(cxl_storage_test_helper test-fabric-lib migration)
set_target_properties(cxl_storage_test_helper PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests")
