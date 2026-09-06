from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).parents[2]
SMOKE_CMAKE = PROJECT_ROOT / "deploy/cxl-riscv/smoke/CMakeLists.txt"
TOOLCHAIN = PROJECT_ROOT / "deploy/cxl-riscv/toolchain-riscv64.cmake"


class SmokeCmakeContractTest(unittest.TestCase):
    def test_standalone_smoke_does_not_configure_the_3fs_project(self):
        source = SMOKE_CMAKE.read_text(encoding="utf-8")
        self.assertNotIn("add_subdirectory", source)
        self.assertNotIn(" common", source)
        for target in (
            "cxl_dax_smoke",
            "fuse_mount_smoke",
            "fdb_client_smoke",
        ):
            self.assertIn(f"add_executable({target}", source)

    def test_fdb_probe_uses_only_pinned_import_and_threads(self):
        source = SMOKE_CMAKE.read_text(encoding="utf-8")
        self.assertIn("add_library(hf3fs_fdb_c SHARED IMPORTED", source)
        self.assertIn("HF3FS_RISCV_FDB_CLIENT", source)
        self.assertIn("HF3FS_RISCV_FDB_INCLUDE", source)
        self.assertIn(
            "target_link_libraries(fdb_client_smoke PRIVATE hf3fs_fdb_c Threads::Threads)",
            source,
        )

    def test_toolchain_never_searches_host_for_target_inputs(self):
        source = TOOLCHAIN.read_text(encoding="utf-8")
        self.assertIn("set(CMAKE_SYSTEM_PROCESSOR riscv64)", source)
        self.assertIn("set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)", source)
        self.assertIn("set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)", source)
        self.assertIn("set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)", source)
        self.assertIn("set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)", source)
        self.assertIn(
            "set(CMAKE_TRY_COMPILE_PLATFORM_VARIABLES HF3FS_RISCV_SYSROOT)",
            source,
        )
        self.assertNotIn("BOTH", source)


if __name__ == "__main__":
    unittest.main()
