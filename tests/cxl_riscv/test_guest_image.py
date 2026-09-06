import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import shutil
import subprocess


DEPLOY_DIR = Path(__file__).parents[2] / "deploy/cxl-riscv"
sys.path.insert(0, str(DEPLOY_DIR))

import guest_image


class GuestImageTest(unittest.TestCase):
    def test_kernel_fragment_requires_dax_fuse_tun_and_storage(self):
        options = guest_image.required_kernel_options()
        self.assertEqual(options["CONFIG_CXL_MEM"], "y")
        self.assertEqual(options["CONFIG_CXL_REGION"], "y")
        self.assertEqual(options["CONFIG_DEV_DAX"], "y")
        self.assertEqual(options["CONFIG_DEV_DAX_CXL"], "y")
        self.assertEqual(options["CONFIG_DEV_DAX_KMEM"], "n")
        self.assertEqual(options["CONFIG_FUSE_FS"], "y")
        self.assertEqual(options["CONFIG_TUN"], "y")
        self.assertEqual(options["CONFIG_VIRTIO_BLK"], "y")
        self.assertEqual(options["CONFIG_EXT4_FS"], "y")

    def test_kernel_validation_reports_every_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / ".config"
            config.write_text("CONFIG_CXL_MEM=y\n# CONFIG_TUN is not set\n")
            errors = guest_image.validate_kernel_config(config)
        self.assertIn("CONFIG_TUN must be y, found n", errors)
        self.assertIn("CONFIG_FUSE_FS must be y, found missing", errors)

    def test_rootfs_rejects_wrong_elf_machine(self):
        errors = guest_image.validate_elf_machine(
            {"/opt/3fs/bin/storage_main": "Advanced Micro Devices X86-64"}
        )
        self.assertEqual(errors, ["/opt/3fs/bin/storage_main is not RISC-V"])

    def test_profiles_have_an_explicit_binary_boundary(self):
        smoke = guest_image.required_binaries("platform-smoke")
        full = guest_image.required_binaries("full-3fs")
        self.assertIn("/opt/foundationdb/bin/fdbserver", smoke)
        self.assertNotIn("/opt/3fs/bin/storage_main", smoke)
        self.assertIn("/opt/3fs/bin/storage_main", full)
        with self.assertRaises(ValueError):
            guest_image.required_binaries("unknown")

    def test_image_commands_are_argv_and_manifest_owns_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            rootfs = temporary / "rootfs"
            rootfs.mkdir()
            output = temporary / "guest.ext4"

            def fake_run(command, check):
                self.assertTrue(check)
                if command[0] == "/tools/truncate":
                    Path(command[-1]).write_bytes(b"image")
                return mock.Mock(returncode=0)

            with mock.patch.object(guest_image.subprocess, "run", side_effect=fake_run):
                manifest = guest_image.create_image(
                    rootfs=rootfs,
                    output=output,
                    image_bytes=4096,
                    truncate=Path("/tools/truncate"),
                    mkfs_ext4=Path("/tools/mkfs.ext4"),
                    e2fsck=Path("/tools/e2fsck"),
                )
            record = json.loads(manifest.read_text())
            self.assertEqual(record["schema"], guest_image.IMAGE_SCHEMA)
            self.assertEqual(record["image"], str(output))
            self.assertEqual([item[0] for item in record["commands"]], [
                "/tools/truncate", "/tools/mkfs.ext4", "/tools/e2fsck"
            ])
            self.assertIn("lazy_itable_init=0,lazy_journal_init=0", record["commands"][1])
            with self.assertRaises(guest_image.GuestImageError):
                guest_image.create_image(
                    rootfs=rootfs,
                    output=output,
                    image_bytes=4096,
                    truncate=Path("/tools/truncate"),
                    mkfs_ext4=Path("/tools/mkfs.ext4"),
                    e2fsck=Path("/tools/e2fsck"),
                )

    @unittest.skipUnless(all(shutil.which(name) for name in ("truncate", "mkfs.ext4", "e2fsck", "debugfs", "cp")),
                         "ext4 image tools are required")
    def test_cached_base_is_reused_and_node_patch_is_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rootfs = root / "rootfs"
            (rootfs / "etc").mkdir(parents=True)
            (rootfs / "etc/value").write_text("base\n")
            base = root / "cache/base.ext4"
            tools = dict(truncate=Path(shutil.which("truncate")), mkfs_ext4=Path(shutil.which("mkfs.ext4")),
                         e2fsck=Path(shutil.which("e2fsck")))
            first, hit = guest_image.ensure_cached_image(
                rootfs=rootfs, output=base, image_bytes=32 * 1024**2, **tools)
            second, hit_again = guest_image.ensure_cached_image(
                rootfs=rootfs, output=base, image_bytes=32 * 1024**2, **tools)
            self.assertFalse(hit)
            self.assertTrue(hit_again)
            self.assertEqual(second["sha256"], first["sha256"])
            self.assertEqual(base.stat().st_mode & 0o777, 0o444)
            self.assertEqual(second["immutable_receipt"], guest_image.immutable_image_receipt(base))

            patch = root / "new-value"
            patch.write_text("node-specific\n")
            output = root / "node.ext4"
            manifest = guest_image.clone_patched_image(
                base_image=base, base_sha256=first["sha256"], output=output,
                patches={"/etc/value": patch}, copy=Path(shutil.which("cp")),
                debugfs=Path(shutil.which("debugfs")), e2fsck=tools["e2fsck"])
            record = json.loads(manifest.read_text())
            self.assertEqual(record["schema"], guest_image.CLONE_SCHEMA)
            self.assertEqual(record["patches"]["/etc/value"], guest_image._sha256_file(patch))
            dumped = subprocess.run([shutil.which("debugfs"), "-R", "cat /etc/value", str(output)],
                                    text=True, capture_output=True, check=True)
            self.assertIn("node-specific", dumped.stdout)

            config = root / "config.ext4"
            config_manifest = guest_image.create_config_image(
                output=config, patches={"/etc/value": patch}, image_bytes=16 * 1024**2,
                truncate=Path(shutil.which("truncate")), mkfs_ext4=Path(shutil.which("mkfs.ext4")),
                e2fsck=Path(shutil.which("e2fsck")))
            config_record = json.loads(config_manifest.read_text())
            self.assertEqual(config_record["schema"], guest_image.CONFIG_SCHEMA)
            self.assertEqual(config.stat().st_mode & 0o777, 0o444)
            dumped = subprocess.run([shutil.which("debugfs"), "-R", "cat /etc/value", str(config)],
                                    text=True, capture_output=True, check=True)
            self.assertIn("node-specific", dumped.stdout)


if __name__ == "__main__":
    unittest.main()
