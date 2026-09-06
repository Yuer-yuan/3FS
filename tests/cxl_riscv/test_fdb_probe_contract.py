from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).parents[2]
SOURCE = PROJECT_ROOT / "src/tools/fdb_client_smoke.cc"


class FdbProbeContractTest(unittest.TestCase):
    def test_probe_uses_api_710_network_thread_commit_and_readback(self):
        source = SOURCE.read_text(encoding="utf-8")
        for symbol in (
            "#define FDB_API_VERSION 710",
            "fdb_select_api_version",
            "fdb_setup_network",
            "fdb_run_network",
            "fdb_transaction_commit",
            "fdb_transaction_get",
            "fdb_future_block_until_ready",
            "fdb_future_get_error",
            "fdb_future_get_value",
            "fdb_stop_network",
        ):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, source)

    def test_probe_uses_a_run_scoped_system_key_and_real_cluster_file(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("\\xff\\x02/hf3fs-cxl-g0/", source)
        self.assertIn("FDB_TR_OPTION_ACCESS_SYSTEM_KEYS", source)
        self.assertIn("fdb_create_database", source)
        self.assertNotIn("memory kv", source.lower())

    def test_every_future_path_has_block_and_error_checks(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("waitFuture", source)
        self.assertGreaterEqual(source.count("waitFuture("), 3)
        self.assertIn("fdb_future_destroy", source)


if __name__ == "__main__":
    unittest.main()
