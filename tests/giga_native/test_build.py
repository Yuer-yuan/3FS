import importlib.util
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest import mock


PROJECT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT / "deploy/giga-native/build.py"
SPEC = importlib.util.spec_from_file_location("hf3fs_giga_build", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class BuildTest(unittest.TestCase):
    def test_native_cargo_compiler_environment_uses_linux_core_id_path(self):
        environment = MODULE.native_compiler_environment({"PATH": "/tools", "UNRELATED": "keep"}, 2)
        self.assertEqual(environment["CXX"], "/usr/bin/clang++-18")
        self.assertIn("--gcc-install-dir=/usr/lib/gcc/x86_64-linux-gnu/13", environment["CXXFLAGS"])
        self.assertIn("-DROCKSDB_SCHED_GETCPU_PRESENT=1", environment["CXXFLAGS"])
        self.assertEqual(environment["CARGO_BUILD_JOBS"], "2")
        self.assertEqual(environment["UNRELATED"], "keep")
        with self.assertRaises(ValueError):
            MODULE.native_compiler_environment({}, 0)

    def test_liburing_compat_does_not_redefine_open_how(self):
        include = PROJECT / "third_party/liburing-cmake"
        completed = subprocess.run(
            ["gcc", "-fsyntax-only", "-I", str(include), "-x", "c", "-"],
            input="#include <fcntl.h>\n#include <liburing/compat.h>\nint main(void) { struct open_how h = {0}; return (int)h.flags; }\n",
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_hf3fs_cmake_command_is_cxl_only(self):
        command = MODULE.hf3fs_cmake_command(Path("/src/3FS"), Path("/build"), Path("/fdb"))
        self.assertIn("-DHF3FS_ENABLE_CXL=ON", command)
        self.assertIn("-DHF3FS_ENABLE_RDMA=OFF", command)
        self.assertIn("-DENABLE_FUSE_APPLICATION=ON", command)
        self.assertIn("-DSHUFFLE_METHOD=g++11", command)
        self.assertIn("-DHF3FS_FDB_ROOT=/fdb", command)
        self.assertIn("-DCMAKE_C_COMPILER=/usr/bin/clang-18", command)
        self.assertIn("-DCMAKE_CXX_COMPILER=/usr/bin/clang++-18", command)
        self.assertIn("-DCMAKE_CXX_FLAGS=--gcc-install-dir=/usr/lib/gcc/x86_64-linux-gnu/13", command)

    def test_manifest_rejects_changed_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "binary"
            artifact.write_bytes(b"one")
            inspected = {
                "path": str(artifact), "sha256": MODULE.sha256(artifact), "size": 3,
                "file": "ELF 64-bit x86-64", "interpreter": None, "needed": [], "ldd": "",
            }
            payload = {"schema": MODULE.SCHEMA, "artifacts": {"binary": inspected}}
            payload["manifest_payload_sha256"] = MODULE.hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps(payload))
            artifact.write_bytes(b"two")
            with self.assertRaisesRegex(ValueError, "artifact changed"):
                MODULE.verify_manifest(manifest)

    def test_write_and_verify_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "binary"
            artifact.write_bytes(b"elf")
            info = {
                "path": str(artifact.resolve()), "sha256": MODULE.sha256(artifact), "size": 3,
                "file": "ELF 64-bit x86-64", "interpreter": None, "needed": [], "ldd": "",
            }
            with mock.patch.object(MODULE, "inspect_elf", return_value=info):
                manifest = MODULE.write_manifest(root / "manifest.json", {"binary": artifact}, host="giga")
            self.assertEqual(MODULE.verify_manifest(manifest)["host"], "giga")

    def test_constants_match_existing_native_pins(self):
        script = Path(__file__).resolve().parents[4] / "scripts/build_giga_native_io500.sh"
        text = script.read_text()
        self.assertIn(f"IO500_COMMIT={MODULE.IO500_COMMIT}", text)
        self.assertIn(f"IOR_COMMIT={MODULE.IOR_COMMIT}", text)
        self.assertIn(f"PFIND_COMMIT={MODULE.PFIND_COMMIT}", text)


if __name__ == "__main__":
    unittest.main()
