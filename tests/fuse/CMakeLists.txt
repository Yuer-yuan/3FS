add_executable(test_fuse_ioring_init TestIoRingTable.cc)
target_link_libraries(test_fuse_ioring_init PRIVATE gmock_main hf3fs_fuse)
target_link_options(test_fuse_ioring_init PRIVATE -Wl,--wrap=sem_open)
target_include_directories(test_fuse_ioring_init PRIVATE
  ${PROJECT_SOURCE_DIR}/src ${PROJECT_SOURCE_DIR} ${PROJECT_BINARY_DIR}/src ${PROJECT_BINARY_DIR})
set_target_properties(test_fuse_ioring_init PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests")
add_test(NAME test_fuse_ioring_init COMMAND test_fuse_ioring_init)

add_executable(test_fuse_piov_read TestPioVRead.cc)
target_link_libraries(test_fuse_piov_read PRIVATE gmock_main hf3fs_fuse)
target_include_directories(test_fuse_piov_read PRIVATE
  ${PROJECT_SOURCE_DIR}/src ${PROJECT_SOURCE_DIR} ${PROJECT_BINARY_DIR}/src ${PROJECT_BINARY_DIR})
set_target_properties(test_fuse_piov_read PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests")
add_test(NAME test_fuse_piov_read COMMAND test_fuse_piov_read)
