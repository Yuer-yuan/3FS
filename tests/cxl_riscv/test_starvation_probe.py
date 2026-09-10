from pathlib import Path
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parents[2] / 'deploy/cxl-riscv'))
import starvation_probe as probe


class StarvationProbeTest(unittest.TestCase):
    def test_partial_pause_restores_even_lost_ack_and_keeps_business_error(self):
        resumed = threading.Event()
        calls = []
        class Command:
            def __call__(self, node, text, timeout):
                calls.append((node, text))
                if 'kill -STOP' in text and node == 3:
                    raise TimeoutError('STOP acknowledgement lost')
                if text == probe.restore_command():
                    resumed.set()
                return 'State: S\n'
            def diagnostic(self, *args):
                return 'HF3FS_STARVE_THREAD uptime=1 tid=1 name=fdb runtime_ns=1 queued_ns=0 slices=1\n'
            def workload(self, node, text, timeout):
                if not resumed.wait(3):
                    raise AssertionError('failed intervention did not restore clients')
                if node == 0:
                    raise RuntimeError('original FDB failure')
                return 'HF3FS_DIAGNOSTIC_IO action=stat-existing errno=0\n'
        with tempfile.TemporaryDirectory() as tmp, patch.object(probe, 'BASELINE_SECONDS', 0):
            result = probe.execute([], Command(), Path(tmp))
            self.assertTrue((Path(tmp) / 'starvation-probe.json').exists())
        self.assertEqual({n for n, t in calls if t == probe.restore_command()}, set(probe.CLIENTS_TO_PAUSE))
        self.assertIn('original FDB failure', result['first_failure'])
        self.assertFalse(result['acceptance_evidence'])
        self.assertFalse(result.get('pause_window_verified', False))
        self.assertTrue(result['intervention_errors'])

    def test_real_restore_rejects_reused_identity_and_resumes_matching_child(self):
        # Actual process states prove the shell command does not resume an
        # unrelated/reused identity, then resumes the still-owned stopped child.
        restore = probe.restore_command().replace('/bin/busybox awk', 'awk')
        script = ("sleep 20 & sp=$!; trap 'kill -CONT $sp; kill $sp; wait $sp' EXIT; "
                  "kill -STOP $sp; ss=wrong; " + restore + "; rejected=$?; "
                  "test $rejected -ne 0 || exit 9; "
                  "awk '/^State:/{if ($2 != \"T\") exit 1}' /proc/$sp/status || exit 8; "
                  "ss=$(awk '{print $22}' /proc/$sp/stat); " + restore)
        done = subprocess.run(['/bin/sh', '-c', script], capture_output=True, text=True, timeout=3)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_guest_watchdog_restores_without_host_followup(self):
        with tempfile.TemporaryDirectory() as tmp:
            fifo = Path(tmp) / 'release'
            import os
            os.mkfifo(fifo)
            pause = probe.pause_command().replace('/bin/busybox awk', 'awk')
            pause = pause.replace('sleep 30', f'read gate <{fifo}')
            script = ("sleep 20 & hf3fs_pid_hf3fs_fuse_main=$!; "
                      "trap 'kill -CONT $hf3fs_pid_hf3fs_fuse_main; kill $hf3fs_pid_hf3fs_fuse_main; wait $hf3fs_pid_hf3fs_fuse_main' EXIT; "
                      + pause + "; watchdog=$!; "
                      "awk '/^State:/{if ($2 != \"T\") exit 1}' /proc/$hf3fs_pid_hf3fs_fuse_main/status || exit 8; "
                      f"printf 'go\\n' >{fifo}; wait $watchdog; "
                      "awk '/^State:/{if ($2 == \"T\") exit 1}' /proc/$hf3fs_pid_hf3fs_fuse_main/status")
            done = subprocess.run(['/bin/sh', '-c', script], capture_output=True, text=True, timeout=3)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn('HF3FS_STARVE_AUTO_RESUME', done.stdout)

    def test_no_stall_skips_intervention_and_cannot_pass_acceptance(self):
        class Command:
            def __call__(self, *args):
                raise AssertionError('no pause expected')
            def diagnostic(self, *args):
                return 'HF3FS_STARVE_THREAD uptime=1 tid=1 name=fdb runtime_ns=1 queued_ns=0 slices=1\n'
            def workload(self, node, *args):
                return 'HF3FS_DIAGNOSTIC_IO action=stat-existing errno=0\n'
        with tempfile.TemporaryDirectory() as tmp, patch.object(probe.run_g0, '_require_probe_record', return_value={'status': 'passed'}):
            result = probe.execute([], Command(), Path(tmp))
        self.assertIn('intervention_skipped', result)
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(all(p['status'] == 'passed' for p in result['operations']))


if __name__ == '__main__':
    unittest.main()
