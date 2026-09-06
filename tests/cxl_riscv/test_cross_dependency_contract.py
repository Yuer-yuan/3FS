from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy/cxl-riscv"
sys.path.insert(0, str(DEPLOY))
import full_deps


def elf(machine: int) -> bytes:
    ident = b"\x7fELF\x02\x01\x01" + bytes(9)
    return struct.pack("<16sHHIQQQIHHHHHH", ident, 1, machine, 1, 0, 0, 0, 0, 64, 0, 0, 0, 0, 0)


class CrossDependencyContractTest(unittest.TestCase):
    def test_rejects_host_elf(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "host.so"
            path.write_bytes(elf(62))
            with self.assertRaisesRegex(ValueError, "not RISC-V"):
                full_deps.check_target_library(path)

    def test_accepts_riscv_elf(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "target.so"
            path.write_bytes(elf(243))
            full_deps.check_target_library(path)

    def test_rejects_mixed_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "target.o").write_bytes(elf(243))
            (root / "host.o").write_bytes(elf(62))
            archive = root / "mixed.a"
            subprocess.run(["ar", "rcs", str(archive), str(root / "target.o"), str(root / "host.o")], check=True)
            with self.assertRaisesRegex(ValueError, "non-RISC-V"):
                full_deps.check_target_library(archive)

    def test_rejects_library_symlink_outside_sysroot(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "sysroot"
            root.mkdir()
            (parent / "host.so").write_bytes(elf(62))
            (root / "lib.so").symlink_to(parent / "host.so")
            with self.assertRaisesRegex(ValueError, "escapes sysroot"):
                full_deps.contained_file(root, "lib.so")


if __name__ == "__main__":
    unittest.main()
