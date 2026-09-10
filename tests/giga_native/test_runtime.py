import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT / "deploy/giga-native/runtime.py"
SPEC = importlib.util.spec_from_file_location("hf3fs_giga_runtime", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class RuntimeTest(unittest.TestCase):
    def test_run_roots_are_exact_children_and_never_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("volatile", "storage", "results"):
                (root / name).mkdir()
            roots = MODULE.RunRoots.create(
                root,
                "run-1c1s-r1",
                volatile_parent=root / "volatile",
                storage_parent=root / "storage",
                result_parent=root / "results",
            )
            self.assertEqual(roots.bundle.name, "run-1c1s-r1")
            with self.assertRaises(FileExistsError):
                MODULE.RunRoots.create(
                    root,
                    "run-1c1s-r1",
                    volatile_parent=root / "volatile",
                    storage_parent=root / "storage",
                    result_parent=root / "results",
                )
            for path in (roots.volatile, roots.storage, roots.bundle):
                MODULE.safe_remove_owned_root(path, path.parent, roots.owner_token)
        for bad in ("../escape", "/absolute", "", "space value"):
            with self.assertRaises(ValueError):
                MODULE.validate_run_id(bad)

    def test_owned_process_rejects_changed_start_ticks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            process = MODULE.OwnedProcess.start(
                role="blocked",
                argv=["/bin/sleep", "60"],
                log=root / "blocked.log",
                receipt_path=root / "blocked.json",
            )
            receipt = json.loads((root / "blocked.json").read_text())
            receipt["start_ticks"] += 1
            MODULE.atomic_json(root / "blocked.json", receipt)
            with self.assertRaises(MODULE.OwnershipError):
                process.stop(0.1, 0.1)
            os.kill(process.pid, signal.SIGKILL)
            process.process.wait()
            process.log_stream.close()

    def test_owned_process_stops_exact_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            process = MODULE.OwnedProcess.start(
                role="blocked",
                argv=["/bin/sleep", "60"],
                log=root / "blocked.log",
                receipt_path=root / "blocked.json",
            )
            result = process.stop(1, 1)
            self.assertEqual(result["returncode"], -signal.SIGTERM)
            self.assertEqual(result["actions"][0]["signal"], "SIGTERM")

    def test_first_failure_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "result.json"
            ledger = MODULE.ResultLedger(path, {"status": "running"})
            ledger.fail("workload", RuntimeError("first"))
            ledger.fail("cleanup", RuntimeError("second"))
            result = json.loads(path.read_text())
            self.assertEqual(result["first_failure"]["message"], "first")
            self.assertEqual(result["secondary_failures"][0]["message"], "second")

    def test_port_probe(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        self.assertFalse(MODULE.port_is_free(port))
        listener.close()
        self.assertTrue(MODULE.port_is_free(port))

    def test_safe_remove_rejects_wrong_receipt_and_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "owned"
            root.mkdir()
            (root / ".hf3fs-run-owner").write_text("right\n")
            with self.assertRaises(MODULE.OwnershipError):
                MODULE.safe_remove_owned_root(root, parent, "wrong")
            link = parent / "link"
            link.symlink_to(root, target_is_directory=True)
            with self.assertRaises(MODULE.OwnershipError):
                MODULE.safe_remove_owned_root(link, parent, "right")


if __name__ == "__main__":
    unittest.main()
