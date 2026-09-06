import dataclasses
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest


DEPLOY_DIR = Path(__file__).parents[2] / "deploy" / "cxl-riscv"
sys.path.insert(0, str(DEPLOY_DIR))

import preflight


def write_file(path: Path, data: bytes = b"fixture", *, executable: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o755 if executable else 0o644)
    return path


def write_riscv_elf(path: Path, *, machine: int = 243, executable: bool = False) -> Path:
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4] = 2  # ELFCLASS64
    header[5] = 1  # ELFDATA2LSB
    header[6] = 1  # EV_CURRENT
    struct.pack_into("<H", header, 16, 2 if executable else 3)
    struct.pack_into("<H", header, 18, machine)
    struct.pack_into("<I", header, 20, 1)
    struct.pack_into("<H", header, 52, 64)
    return write_file(path, bytes(header), executable=executable)


class Fixture:
    def __init__(self, root: Path):
        self.root = root
        self.superproject = root / "CXLMemSim-riscv"
        self.project = self.superproject / "components" / "3FS"
        self.sysroot = root / "sysroot"
        self.rootfs = root / "rootfs"
        self.tools = root / "tools"

        self.project.mkdir(parents=True)
        self.rootfs.mkdir()
        (self.sysroot / "usr" / "include" / "foundationdb").mkdir(parents=True)
        (self.sysroot / "usr" / "include" / "fuse3").mkdir(parents=True)

        self.qemu = write_file(
            self.superproject / "build" / "qemu" / "qemu-system-riscv64",
            executable=True,
        )
        self.cxlmemsim_server = write_file(
            self.superproject / "build" / "cxlmemsim" / "cxlmemsim_server",
            executable=True,
        )
        self.cxlmemsim_topology = write_file(
            self.superproject / "components" / "cxlmemsim" / "topology_simple.txt"
        )
        self.opensbi = write_file(
            self.superproject / "artifacts" / "fw_dynamic.bin"
        )
        self.uboot = write_file(self.superproject / "artifacts" / "u-boot.bin")
        self.kernel = write_file(self.superproject / "artifacts" / "Image")
        self.kernel_config = write_file(
            self.superproject / "artifacts" / "linux.config"
        )

        self.c_compiler = write_file(
            self.tools / "riscv64-linux-gnu-gcc", executable=True
        )
        self.cxx_compiler = write_file(
            self.tools / "riscv64-linux-gnu-g++", executable=True
        )
        self.cmake = write_file(self.tools / "cmake", executable=True)
        self.ninja = write_file(self.tools / "ninja", executable=True)
        self.fdb_client = write_riscv_elf(
            self.sysroot / "usr" / "lib" / "libfdb_c.so"
        )
        self.fdb_server = write_riscv_elf(
            self.sysroot / "usr" / "bin" / "fdbserver", executable=True
        )
        self.fuse_library = write_riscv_elf(
            self.sysroot / "usr" / "lib" / "libfuse3.so"
        )

    def inputs(self) -> preflight.Inputs:
        return preflight.Inputs(
            project_root=self.project,
            superproject_root=self.superproject,
            qemu=self.qemu,
            cxlmemsim_server=self.cxlmemsim_server,
            cxlmemsim_topology=self.cxlmemsim_topology,
            opensbi=self.opensbi,
            uboot=self.uboot,
            kernel=self.kernel,
            kernel_config=self.kernel_config,
            sysroot=self.sysroot,
            rootfs=self.rootfs,
            c_compiler=self.c_compiler,
            cxx_compiler=self.cxx_compiler,
            cmake=self.cmake,
            ninja=self.ninja,
            fdb_include=self.sysroot / "usr" / "include",
            fdb_client=self.fdb_client,
            fdb_server=self.fdb_server,
            fuse_include=self.sysroot / "usr" / "include" / "fuse3",
            fuse_library=self.fuse_library,
        )


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Fixture(Path(self.temporary.name))

    def test_rejects_missing_cxx_and_relative_sysroot(self):
        inputs = dataclasses.replace(
            self.fixture.inputs(),
            sysroot=Path("relative/sysroot"),
        )
        errors = preflight.validate(
            inputs,
            is_executable=lambda path, _mode: path.name
            != "riscv64-linux-gnu-g++",
        )
        self.assertIn("HF3FS_RISCV_SYSROOT must be absolute", errors)
        self.assertIn("HF3FS_RISCV_CXX is not executable", errors)

    def test_accepts_complete_absolute_inputs(self):
        self.assertEqual(preflight.validate(self.fixture.inputs()), [])

    def test_rejects_platform_symlink_that_resolves_outside_superproject(self):
        outside = write_file(
            self.fixture.root / "outside" / "qemu-system-riscv64",
            executable=True,
        )
        link = self.fixture.superproject / "build" / "qemu-link"
        link.symlink_to(outside)
        inputs = dataclasses.replace(self.fixture.inputs(), qemu=link)
        self.assertIn(
            "HF3FS_QEMU resolves outside HF3FS_SUPERPROJECT_ROOT",
            preflight.validate(inputs),
        )

    def test_rejects_non_riscv_target_elf(self):
        write_riscv_elf(self.fixture.fdb_client, machine=62)
        errors = preflight.validate(self.fixture.inputs())
        self.assertIn("HF3FS_RISCV_FDB_CLIENT is not RISC-V ELF64", errors)

    def test_rejects_target_library_outside_sysroot(self):
        outside = write_riscv_elf(self.fixture.root / "libfuse3.so")
        inputs = dataclasses.replace(self.fixture.inputs(), fuse_library=outside)
        self.assertIn(
            "HF3FS_RISCV_FUSE_LIBRARY resolves outside HF3FS_RISCV_SYSROOT",
            preflight.validate(inputs),
        )

    def test_environment_contract_reports_all_missing_names(self):
        with self.assertRaisesRegex(
            ValueError, "HF3FS_CMAKE.*HF3FS_SUPERPROJECT_ROOT"
        ):
            preflight.Inputs.from_environment({})

    def test_report_hashes_platform_inputs_and_is_stable(self):
        first = preflight.build_report(self.fixture.inputs())
        second = preflight.build_report(self.fixture.inputs())
        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(first["status"], "passed")
        self.assertEqual(first["errors"], [])
        self.assertRegex(first["platform_identity_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(first["resolved"]["qemu"]["sha256"], r"^[0-9a-f]{64}$")

    def test_cli_always_emits_json_on_missing_environment(self):
        output = io.StringIO()
        self.assertEqual(preflight.main([], env={}, stdout=output), 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["resolved"], {})
        self.assertTrue(result["errors"])


if __name__ == "__main__":
    unittest.main()
