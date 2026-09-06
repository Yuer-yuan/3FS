from pathlib import Path
import dataclasses
import sys
import unittest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy/cxl-riscv"
sys.path.insert(0, str(DEPLOY))
import build_riscv
import preflight


def inputs():
    return preflight.Inputs(**{field.name: Path("/private 3fs") / field.name
                               for field in dataclasses.fields(preflight.Inputs)})


class BuildRiscvTest(unittest.TestCase):
    def test_full_build_requires_source_closure(self):
        with self.assertRaisesRegex(ValueError, "source closure"):
            build_riscv.build_command(inputs(), Path("/build"), 8, "full-3fs")

    def test_full_build_cannot_enable_rdma(self):
        closure = {"schema": "hf3fs.cxl-source-closure.v1", "status": "passed"}
        with self.assertRaisesRegex(ValueError, "protected"):
            build_riscv.build_command(inputs(), Path("/build"), 8, "full-3fs", closure,
                                      {"HF3FS_ENABLE_RDMA": "ON"})

    def test_source_path_with_spaces_is_one_argument(self):
        command = build_riscv.build_command(inputs(), Path("/build space"), 8, "platform-smoke")
        self.assertEqual(command[command.index("-S") + 1], "/private 3fs/project_root/deploy/cxl-riscv/smoke")
        self.assertEqual(command[command.index("-B") + 1], "/build space")

    def test_rejects_nonpositive_jobs(self):
        with self.assertRaisesRegex(ValueError, "positive"):
            build_riscv.build_command(inputs(), Path("/build"), 0, "platform-smoke")

    def test_rejects_shell_text_as_target(self):
        closure = {"schema": "hf3fs.cxl-source-closure.v1", "status": "passed"}
        with self.assertRaisesRegex(ValueError, "target"):
            build_riscv.build_command(inputs(), Path("/build"), 8, "full-3fs", closure,
                                      extra_targets=["storage_main; command"])

    def test_platform_profile_rejects_extra_options(self):
        with self.assertRaisesRegex(ValueError, "require full-3fs"):
            build_riscv.build_command(inputs(), Path("/build"), 8, "platform-smoke",
                                      cmake_options={"SHUFFLE_METHOD": "stdshuffle"})


if __name__ == "__main__":
    unittest.main()
