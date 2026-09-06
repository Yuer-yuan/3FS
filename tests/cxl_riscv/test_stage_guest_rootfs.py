from pathlib import Path
import sys
import tempfile
import unittest


DEPLOY_DIR = Path(__file__).parents[2] / "deploy/cxl-riscv"
sys.path.insert(0, str(DEPLOY_DIR))

import stage_guest_rootfs


class StageGuestRootfsTest(unittest.TestCase):
    def test_output_must_be_a_fresh_3fs_generated_path(self):
        with self.assertRaises(stage_guest_rootfs.RootfsStageError):
            stage_guest_rootfs.require_output(Path("/tmp/rootfs"))

    def test_applet_contract_contains_guest_boot_and_test_commands(self):
        for applet in (
            "ip",
            "mount",
            "mknod",
            "nc",
            "poweroff",
            "sed",
            "tr",
            "umount",
            "uname",
        ):
            self.assertIn(applet, stage_guest_rootfs.APPLET_NAMES)

    def test_guest_init_waits_for_real_dax_and_reports_fuse_device(self):
        source = (DEPLOY_DIR / "guest/init").read_text(encoding="utf-8")
        self.assertIn("/usr/riscv64-linux-gnu/lib", source)
        self.assertIn("/sys/bus/dax/devices/dax*.*", source)
        self.assertIn("mknod \"$dax_path\" c", source)
        self.assertIn("[ -c /dev/fuse ]", source)
        self.assertIn("HF3FS_G0_GUEST_READY", source)
        self.assertNotIn("LEGOFS", source)

    def test_stage_includes_non_fdb_target_runtime_closure(self):
        source = (DEPLOY_DIR / "stage_guest_rootfs.py").read_text(encoding="utf-8")
        self.assertIn("def stage_runtime_closure", source)
        self.assertIn("--target-runtime-sysroot", source)
        self.assertIn("find_target_runtime_file", source)


if __name__ == "__main__":
    unittest.main()
