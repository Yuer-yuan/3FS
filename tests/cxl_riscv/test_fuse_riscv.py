import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).parents[2]
DEPLOY_DIR = PROJECT_ROOT / "deploy/cxl-riscv"
sys.path.insert(0, str(DEPLOY_DIR))

import prepare_fuse_riscv


class FuseRiscvPreparationTest(unittest.TestCase):
    def test_lock_pins_complete_noble_riscv_package_set(self):
        lock = prepare_fuse_riscv.load_lock(DEPLOY_DIR / "fuse-riscv.lock.json")
        self.assertEqual(lock["version"], "3.14.0-5build1")
        self.assertEqual(
            {record["package"] for record in lock["packages"]},
            {"libfuse3-dev", "libfuse3-3", "fuse3"},
        )
        for record in lock["packages"]:
            self.assertEqual(record["architecture"], "riscv64")
            self.assertEqual(len(record["sha256"]), 64)
            self.assertGreater(record["size"], 0)
            self.assertTrue(record["url"].startswith("https://ports.ubuntu.com/"))

    def test_lock_rejects_redirectable_or_wrong_architecture_records(self):
        lock = json.loads((DEPLOY_DIR / "fuse-riscv.lock.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "bad.json"
            lock["packages"][0]["architecture"] = "amd64"
            bad.write_text(json.dumps(lock))
            with self.assertRaises(prepare_fuse_riscv.FusePreparationError):
                prepare_fuse_riscv.load_lock(bad)

    def test_output_must_remain_in_3fs_generated_tree(self):
        with self.assertRaises(prepare_fuse_riscv.FusePreparationError):
            prepare_fuse_riscv.require_output_root(Path("/tmp/fuse-overlay"))

    def test_package_absolute_library_link_is_normalized_inside_overlay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            link = root / "usr/lib/riscv64-linux-gnu/libfuse3.so"
            link.parent.mkdir(parents=True)
            link.symlink_to("/lib/riscv64-linux-gnu/libfuse3.so.3")
            prepare_fuse_riscv.normalize_overlay_symlinks(root)
            self.assertEqual(
                link.readlink(),
                Path("../../../lib/riscv64-linux-gnu/libfuse3.so.3"),
            )
            self.assertFalse(link.readlink().is_absolute())


if __name__ == "__main__":
    unittest.main()
