if(CMAKE_CROSSCOMPILING)
    set(HF3FS_CARGO_EXECUTABLE "" CACHE FILEPATH "Private Cargo executable for the offline cross build")
    set(HF3FS_CARGO_TARGET_DIR "${CMAKE_BINARY_DIR}/cargo-target" CACHE PATH "Isolated Cargo output")
    if(NOT IS_ABSOLUTE "${HF3FS_CARGO_EXECUTABLE}" OR NOT EXISTS "${HF3FS_CARGO_EXECUTABLE}")
        message(FATAL_ERROR "Cross Cargo requires HF3FS_CARGO_EXECUTABLE")
    endif()
    set(CARGO_CMD ${CMAKE_COMMAND} -E env "CARGO_TARGET_DIR=${HF3FS_CARGO_TARGET_DIR}"
        ${HF3FS_CARGO_EXECUTABLE} build --locked --offline --target riscv64gc-unknown-linux-gnu -p chunk_engine)
    if(CMAKE_BUILD_TYPE STREQUAL "Debug")
        set(TARGET_DIR "debug")
    else()
        list(APPEND CARGO_CMD --release)
        set(TARGET_DIR "release")
    endif()
    add_custom_target(cargo_build_all ALL COMMAND ${CARGO_CMD} WORKING_DIRECTORY "${PROJECT_SOURCE_DIR}")
    macro(add_crate NAME)
        set(LIBRARY "${HF3FS_CARGO_TARGET_DIR}/riscv64gc-unknown-linux-gnu/${TARGET_DIR}/lib${NAME}.a")
        set(SOURCES "${HF3FS_CARGO_TARGET_DIR}/cxxbridge/${NAME}/src/cxx.rs.h"
            "${HF3FS_CARGO_TARGET_DIR}/cxxbridge/${NAME}/src/cxx.rs.cc")
        add_custom_command(OUTPUT ${SOURCES} ${LIBRARY} COMMAND ${CARGO_CMD}
            WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/${NAME}")
        add_library(${NAME} STATIC ${SOURCES} ${LIBRARY})
        target_link_libraries(${NAME} pthread dl ${LIBRARY})
        target_include_directories(${NAME} PUBLIC "${HF3FS_CARGO_TARGET_DIR}/cxxbridge")
        target_compile_options(${NAME} PUBLIC -Wno-dollar-in-identifier-extension)
        add_dependencies(${NAME} cargo_build_all)
    endmacro()
else()
if (CMAKE_BUILD_TYPE STREQUAL "Debug")
    set(CARGO_CMD cargo build)
    set(TARGET_DIR "debug")
else ()
    set(CARGO_CMD cargo build --release)
    set(TARGET_DIR "release")
endif ()

add_custom_target(
    cargo_build_all ALL
    COMMAND ${CARGO_CMD}
    WORKING_DIRECTORY "${PROJECT_SOURCE_DIR}"
)

macro(add_crate NAME)
    set(LIBRARY "${PROJECT_SOURCE_DIR}/target/${TARGET_DIR}/lib${NAME}.a")
    set(SOURCES
        "${PROJECT_SOURCE_DIR}/target/cxxbridge/${NAME}/src/cxx.rs.h"
        "${PROJECT_SOURCE_DIR}/target/cxxbridge/${NAME}/src/cxx.rs.cc"
    )

    add_custom_command(
        OUTPUT ${SOURCES} ${LIBRARY}
        COMMAND ${CARGO_CMD}
        WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/${NAME}"
    )

    add_library(${NAME} STATIC ${SOURCES} ${LIBRARY})
    target_link_libraries(${NAME} pthread dl ${LIBRARY})
    target_include_directories(${NAME} PUBLIC "${PROJECT_SOURCE_DIR}/target/cxxbridge")
    target_compile_options(${NAME} PUBLIC -Wno-dollar-in-identifier-extension)
    add_dependencies(${NAME} cargo_build_all)
endmacro()
endif()
