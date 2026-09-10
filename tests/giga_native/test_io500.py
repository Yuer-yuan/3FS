import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[2]
DEPLOY = PROJECT / "deploy/giga-native"
sys.path.insert(0, str(DEPLOY))
SPEC = importlib.util.spec_from_file_location("hf3fs_giga_io500", DEPLOY / "io500.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class IO500Test(unittest.TestCase):
    def result_lines(self):
        return "IO500 version pinned\n" + "".join(
            f"[RESULT] {name} 1.0 GiB/s : time 1.0 seconds\n"
            for name in MODULE.EXPECTED_PHASES
        )

    def test_watchdog_accepts_fragmented_ordered_22_phases(self):
        watchdog = MODULE.PhaseWatchdog(phase_timeout_seconds=120)
        text = self.result_lines()
        for offset in range(0, len(text), 7):
            watchdog.feed(text[offset:offset + 7], offset + 1)
        watchdog.finish()
        self.assertEqual([row["phase"] for row in watchdog.records], list(MODULE.EXPECTED_PHASES))

    def test_watchdog_rejects_duplicate_and_nonfinite(self):
        first = MODULE.EXPECTED_PHASES[0]
        for text in (
            f"IO500 version x\n[RESULT] {first} 1 x : time 1 seconds\n[RESULT] {first} 1 x : time 1 seconds\n",
            f"IO500 version x\n[RESULT] {first} NaN x : time 1 seconds\n",
        ):
            watchdog = MODULE.PhaseWatchdog()
            with self.assertRaises(MODULE.IO500Error):
                watchdog.feed(text, 1)

    def test_watchdog_rejects_split_fatal_line(self):
        watchdog = MODULE.PhaseWatchdog()
        watchdog.feed("IO500 version x\nRemote I/", 1)
        with self.assertRaisesRegex(MODULE.IO500Error, "fatal IO500 output"):
            watchdog.feed("O error\n", 2)

    def test_expected_invalid_verifier_is_required(self):
        value = MODULE.parse_verifier(1, "[OK] But this is an invalid run!\n", "")
        self.assertIs(value["expected_invalid_diagnosis"], True)
        with self.assertRaises(MODULE.IO500Error):
            MODULE.parse_verifier(0, "[OK]\n", "")

    def test_parse_result_requires_rank_count_and_invalid(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "result.txt"
            sections = "\n".join(f"[{phase}]\nscore = 1" for phase in MODULE.EXPECTED_PHASES)
            path.write_text(f"[run]\nprocs = 2\nSCORE = INVALID\n{sections}\n")
            self.assertTrue(MODULE.parse_result(path, 2)["expected_invalid"])
            with self.assertRaises(MODULE.IO500Error):
                MODULE.parse_result(path, 3)

    def test_rank_receipts_are_exact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mounts = [root / "m0", root / "m1"]
            for mount in mounts:
                mount.mkdir()
            for rank in range(2):
                value = {
                    "schema": "hf3fs.giga-native-rank.v1", "rank": rank,
                    "cpu": (1, 7)[rank], "allowed_cpus": [(1, 7)[rank]],
                    "mount": str(mounts[rank].resolve()), "endpoint": 16 + rank,
                    "returncode": 0,
                }
                (root / f"rank-{rank}.json").write_text(json.dumps(value))
            records = MODULE.validate_rank_receipts(root, (1, 7), mounts, (16, 17))
            self.assertEqual(len(records), 2)

    def test_archive_results_preserves_phase_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "mounted-results"
            destination = root / "bundle-results"
            source.mkdir()
            (source / "ior-rnd4K-read.txt").write_text("ERROR: short read\n")

            MODULE.archive_results(source, destination)

            self.assertEqual(
                (destination / "ior-rnd4K-read.txt").read_text(),
                "ERROR: short read\n",
            )

    def test_archive_results_is_noop_when_result_directory_is_absent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            MODULE.archive_results(root / "absent", root / "bundle-results")
            self.assertFalse((root / "bundle-results").exists())


if __name__ == "__main__":
    unittest.main()
