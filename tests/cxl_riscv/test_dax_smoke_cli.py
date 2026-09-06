import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).parents[2]
SOURCE = PROJECT_ROOT / "src/tools/cxl_dax_smoke.cc"


class DaxSmokeCliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.temporary.name) / "cxl_dax_smoke"
        compiler = os.environ.get("CXX", "c++")
        subprocess.run(
            [compiler, "-std=c++20", "-O2", "-pthread", str(SOURCE), "-o", str(cls.binary)],
            check=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def invoke(self, backing, role, generation, *, offset="0", length="4096", payload_bytes=None):
        command = [
                str(self.binary),
                "--device", str(backing),
                "--offset", offset,
                "--length", length,
                "--role", role,
                "--generation", str(generation),
                "--timeout-ms", "20",
            ]
        if payload_bytes is not None:
            command.extend(("--payload-bytes", str(payload_bytes)))
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_rejects_unaligned_regular_file_range(self):
        with tempfile.NamedTemporaryFile() as backing:
            backing.truncate(8192)
            result = self.invoke(backing.name, "writer", 7, offset="1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("offset must satisfy mapping alignment", result.stderr)

    def test_file_backed_writer_and_reader_reproduce_generation_and_checksum(self):
        with tempfile.NamedTemporaryFile() as backing:
            backing.truncate(8192)
            writer = self.invoke(backing.name, "writer", 7)
            reader = self.invoke(backing.name, "reader", 7)
        self.assertEqual(writer.returncode, 0, writer.stderr)
        self.assertEqual(reader.returncode, 0, reader.stderr)
        written = json.loads(writer.stdout)
        read = json.loads(reader.stdout)
        self.assertEqual(written["status"], "passed")
        self.assertEqual(written["generation"], 7)
        self.assertEqual(written["bytes"], 4096)
        self.assertTrue(written["atomic_u32_lock_free"])
        self.assertTrue(written["atomic_u64_lock_free"])
        self.assertEqual(read["checksum"], written["checksum"])
        self.assertEqual(read["payload_bytes"], 4096 - 64)

    def test_payload_bytes_limits_touched_data_without_reducing_mapping(self):
        with tempfile.NamedTemporaryFile() as backing:
            backing.truncate(8192)
            writer = self.invoke(backing.name, "writer", 8, length="8192", payload_bytes=128)
            reader = self.invoke(backing.name, "reader", 8, length="8192", payload_bytes=128)
            invalid = self.invoke(backing.name, "writer", 9, length="4096", payload_bytes=4096)
        self.assertEqual(writer.returncode, 0, writer.stderr)
        self.assertEqual(reader.returncode, 0, reader.stderr)
        self.assertEqual(json.loads(writer.stdout)["bytes"], 8192)
        self.assertEqual(json.loads(reader.stdout)["payload_bytes"], 128)
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("payload-bytes exceeds", invalid.stderr)

    def test_reader_rejects_an_unpublished_generation(self):
        with tempfile.NamedTemporaryFile() as backing:
            backing.truncate(8192)
            result = self.invoke(backing.name, "reader", 99)
        self.assertEqual(result.returncode, 1)
        self.assertIn("timed out waiting", result.stderr)

    def test_source_uses_shared_mapping_and_release_acquire_atomic_refs(self):
        source = SOURCE.read_text(encoding="utf-8")
        for marker in (
            "MAP_SHARED",
            "std::atomic_ref<uint32_t>",
            "std::atomic_ref<uint64_t>",
            "std::memory_order_release",
            "std::memory_order_acquire",
            "is_always_lock_free",
            "static_assert(sizeof(SmokeHeader) == 64)",
            "/sys/dev/char",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)
        self.assertNotIn("/sys/class/dax", source)


if __name__ == "__main__":
    unittest.main()
