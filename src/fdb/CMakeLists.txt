if(HF3FS_FDB_ROOT)
    find_path(HF3FS_FDB_INCLUDE_DIR
        NAMES foundationdb/fdb_c_options.g.h
        PATHS "${HF3FS_FDB_ROOT}/include" "${HF3FS_FDB_ROOT}/usr/include"
        NO_DEFAULT_PATH
        REQUIRED
    )
    find_library(HF3FS_FDB_CLIENT_LIBRARY
        NAMES fdb_c
        PATHS "${HF3FS_FDB_ROOT}/lib" "${HF3FS_FDB_ROOT}/lib64"
              "${HF3FS_FDB_ROOT}/usr/lib" "${HF3FS_FDB_ROOT}/usr/lib64"
        NO_DEFAULT_PATH
        REQUIRED
    )
    add_library(hf3fs_fdb_c SHARED IMPORTED GLOBAL)
    set_target_properties(hf3fs_fdb_c PROPERTIES
        IMPORTED_LOCATION "${HF3FS_FDB_CLIENT_LIBRARY}"
        INTERFACE_INCLUDE_DIRECTORIES "${HF3FS_FDB_INCLUDE_DIR}"
    )
    target_add_lib(fdb common hf3fs_fdb_c)
else()
    target_add_lib(fdb common fdb_c)
endif()
