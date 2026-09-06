from pathlib import Path
import sys
import unittest


DEPLOY_DIR = Path(__file__).parents[2] / "deploy/cxl-riscv"
sys.path.insert(0, str(DEPLOY_DIR))

import build_kernel_riscv
import guest_image


class BuildKernelRiscvTest(unittest.TestCase):
    def test_merge_replaces_enabled_disabled_and_string_options_once(self):
        base = "CONFIG_FUSE_FS=m\n# CONFIG_TUN is not set\nCONFIG_INITRAMFS_SOURCE=\"old\"\n"
        fragment = "CONFIG_FUSE_FS=y\nCONFIG_TUN=y\nCONFIG_INITRAMFS_SOURCE=\"\"\n"
        merged = build_kernel_riscv.merge_config(base, fragment)
        self.assertEqual(merged.count("CONFIG_FUSE_FS=y"), 1)
        self.assertEqual(merged.count("CONFIG_TUN=y"), 1)
        self.assertEqual(merged.count('CONFIG_INITRAMFS_SOURCE=""'), 1)
        self.assertNotIn("old", merged)
        self.assertNotIn("is not set", merged)

    def test_kernel_fragment_disables_the_parent_builtin_initramfs(self):
        options = guest_image.required_kernel_options()
        self.assertEqual(options["CONFIG_INITRAMFS_SOURCE"], '""')

    def test_build_command_is_out_of_tree_riscv_and_argv_only(self):
        olddefconfig, image = build_kernel_riscv.build_commands(
            make=Path("/usr/bin/make"),
            source=Path("/work/linux"),
            build=Path("/work/3FS/out/cxl-riscv/kernel/builds/g0-001"),
            cross_prefix=Path("/tools/riscv64-linux-gnu-"),
            jobs=4,
        )
        self.assertIn("-C", olddefconfig)
        self.assertIn("O=/work/3FS/out/cxl-riscv/kernel/builds/g0-001", olddefconfig)
        self.assertIn("ARCH=riscv", olddefconfig)
        self.assertIn("CROSS_COMPILE=/tools/riscv64-linux-gnu-", olddefconfig)
        self.assertEqual(olddefconfig[-1], "olddefconfig")
        self.assertEqual(image[-2:], ["-j4", "Image"])


if __name__ == "__main__":
    unittest.main()
