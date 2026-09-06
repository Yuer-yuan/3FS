import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).parents[2] / "deploy/cxl-riscv"))

import stage_full_rootfs


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StageFullRootfsCacheTest(unittest.TestCase):
    def test_payload_identity_sorts_runtime_libraries(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name in ("base.json", "tools.json", "io500.json", "init", "strip"):
                (directory / name).write_text(name)
            build = {"artifacts": [], "runtime_artifacts": [], "dependencies": {"libraries": {
                "z.so": {"sha256": "z" * 64}, "a.so": {"sha256": "a" * 64}}}}
            first, content = stage_full_rootfs.payload_identity(
                base_manifest=directory / "base.json", build=build, tools={},
                io500_manifest=directory / "io500.json", init=directory / "init",
                strip_debug_tool=directory / "strip")
            build["dependencies"]["libraries"] = dict(
                reversed(list(build["dependencies"]["libraries"].items())))
            second, _ = stage_full_rootfs.payload_identity(
                base_manifest=directory / "base.json", build=build, tools={},
                io500_manifest=directory / "io500.json", init=directory / "init",
                strip_debug_tool=directory / "strip")
            self.assertEqual(first, second)
            self.assertEqual([item["name"] for item in content["runtime_libraries"]], ["a.so", "z.so"])

    def test_verified_payload_is_reused_without_copying_the_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "full-001"
            binary = root / "opt/3fs/bin/storage_main"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"binary")
            init = root / "init"
            init.write_bytes(b"init")
            identity = "a" * 64
            candidate = directory / "full-001-rootfs-manifest.json"
            candidate.write_text(json.dumps({
                "schema": stage_full_rootfs.stage.MANIFEST_SCHEMA,
                "profile": "full-3fs",
                "root": str(root),
                "binaries": {"storage_main": {"path": str(binary), "sha256": sha256(binary)}},
                "runtime_files": {"opt/3fs/bin/storage_main": sha256(binary)},
                "bootstrap_files": {"init": sha256(init)},
                "rootfs_cache": {"schema": stage_full_rootfs.ROOTFS_CACHE_SCHEMA,
                                 "payload_identity": identity},
            }))
            build = directory / "build.json"
            closure = directory / "closure.json"
            build.write_text("{}")
            closure.write_text("{}")
            output = directory / "full-002"

            manifest = stage_full_rootfs._reuse_cached_payload(
                output, identity, build, closure, [candidate])

            self.assertEqual(manifest, directory / "full-002-rootfs-manifest.json")
            self.assertTrue(output.is_symlink())
            record = json.loads(manifest.read_text())
            self.assertTrue(record["rootfs_cache"]["cache_hit"])
            self.assertEqual(Path(record["binaries"]["storage_main"]["path"]),
                             output / "opt/3fs/bin/storage_main")

            binary.write_bytes(b"tampered")
            second = directory / "full-003"
            self.assertIsNone(stage_full_rootfs._reuse_cached_payload(
                second, identity, build, closure, [candidate]))
            self.assertFalse(second.exists())


if __name__ == "__main__":
    unittest.main()
