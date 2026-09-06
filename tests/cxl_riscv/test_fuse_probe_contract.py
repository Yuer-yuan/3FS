from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).parents[2]
SOURCE = PROJECT_ROOT / "src/tools/fuse_mount_smoke.cc"


class FuseProbeContractTest(unittest.TestCase):
    def test_probe_requires_real_mount_read_and_unmount(self):
        source = SOURCE.read_text(encoding="utf-8")
        for symbol in (
            "fuse_session_new",
            "fuse_session_mount",
            "fuse_session_loop",
            "fuse_session_unmount",
            "fuse_session_destroy",
            "fuse_reply_entry",
            "fuse_reply_open",
            "fuse_reply_buf",
            "::open",
            "::read",
        ):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, source)

    def test_probe_reports_each_kernel_request_marker(self):
        source = SOURCE.read_text(encoding="utf-8")
        for marker in (
            "lookup_seen",
            "open_seen",
            "read_seen",
            "unmounted",
            "riscv-fuse-ok",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)

    def test_unmount_wakes_the_session_loop_before_join(self):
        source = SOURCE.read_text(encoding="utf-8")
        success_cleanup = """fuse_session_exit(session);
  fuse_session_unmount(session);
  loop.join();"""
        failure_cleanup = """fuse_session_exit(session);
    fuse_session_unmount(session);
    loop.join();"""
        self.assertIn(success_cleanup, source)
        self.assertIn(failure_cleanup, source)


if __name__ == "__main__":
    unittest.main()
