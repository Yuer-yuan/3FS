target_add_bin(hf3fs-admin "admin.cc" admin-commands)

target_add_bin(cxl_dax_smoke "cxl_dax_smoke.cc" common)
target_add_bin(fuse_mount_smoke "fuse_mount_smoke.cc" common hf3fs_fuse3)
target_add_bin(fdb_client_smoke "fdb_client_smoke.cc" fdb)

add_subdirectory(commands)
add_subdirectory(cxl-fabricd)
