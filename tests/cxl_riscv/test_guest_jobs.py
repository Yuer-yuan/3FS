from pathlib import Path
import os
import re
import sys
import tempfile
import threading
import unittest
sys.path.insert(0, str(Path(__file__).parents[2] / 'deploy/cxl-riscv'))
import guest_jobs
import run_g0


class GuestJobsTest(unittest.TestCase):
    def test_waiting_workload_leaves_control_shell_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fifo = root / 'gate'
            os.mkfifo(fifo)
            console = run_g0.GuestConsole(['/bin/sh'], root / 'console.log', os.environ)
            self.addCleanup(console.stop, .01)
            jobs = guest_jobs.GuestJobs([console], lambda n, c, t: console.shell_command(c, t), [], 5)
            result = []
            thread = threading.Thread(target=lambda: result.append(jobs.run(0, f'cat {fifo}')))
            thread.start()
            # The control command releases a real blocked reader. A foreground
            # workload would prevent this command from being executed.
            console.wait_regex(re.compile(r'(?m)^HF3FS_JOB_START .*\n'), 2)
            console.shell_command(f"printf 'released\\n' >{fifo}", 2)
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertIn('released', result[0])
            self.assertEqual(jobs.records[0]['status'], 'passed')

    def test_timeout_preserves_console_and_session_can_be_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fifo = root / 'gate'; os.mkfifo(fifo)
            console = run_g0.GuestConsole(['/bin/sh'], root / 'console.log', os.environ)
            self.addCleanup(console.stop, .01)
            jobs = guest_jobs.GuestJobs([console], lambda n, c, t: console.shell_command(c, t), [], .2)
            with self.assertRaises(TimeoutError):
                jobs.run(0, f'cat {fifo}')
            self.assertIn('alive', console.shell_command("printf 'alive\\n'", 1))
            entry = jobs.records[0]
            entry['process_start'] += 1
            with self.assertRaises(run_g0.G0Error):
                jobs.stop(entry)
            os.kill(entry['pid'], 0)
            entry['process_start'] -= 1
            jobs.stop(entry)
            console.shell_command(f"wait ${entry['variable']} || true", 1)
            jobs.verify_stopped(entry)
            self.assertTrue(entry['stopped_verified'])
            self.assertTrue(jobs.records[0]['cancel_requested'])
            self.assertEqual(jobs.records[0]['status'], 'failed')

    def test_late_completion_preserves_timeout_without_unverified_kill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fifo = root / 'gate'; os.mkfifo(fifo)
            console = run_g0.GuestConsole(['/bin/sh'], root / 'console.log', os.environ)
            self.addCleanup(console.stop, .01)
            jobs = guest_jobs.GuestJobs([console], lambda n, c, t: console.shell_command(c, t), [], .2)
            with self.assertRaises(TimeoutError):
                jobs.run(0, f'cat {fifo}; exit 7')
            entry = jobs.records[0]
            original_error = entry['error']
            console.shell_command(f"echo released >{fifo}", 2)
            console.wait_regex(re.compile(rf"(?m)^HF3FS_JOB_END token={entry['token']} rc=7\r*\n"), 2)
            # Join the shell job so its leader no longer exists. Cleanup must
            # use the owned completion marker, never kill an unverified PID.
            console.shell_command(f"wait ${entry['variable']} || true", 2)
            jobs.stop(entry)
            jobs.verify_stopped(entry)
            self.assertEqual(entry['returncode'], 7)
            self.assertEqual(entry['status'], 'failed')
            self.assertEqual(entry['error'], original_error)
            self.assertTrue(entry['completion_observed_after_timeout'])
            self.assertNotIn('cancel_requested', entry)

    def test_nonzero_status_is_not_a_collection_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = run_g0.GuestConsole(['/bin/sh'], Path(tmp) / 'console.log', os.environ)
            self.addCleanup(console.stop, .01)
            jobs = guest_jobs.GuestJobs([console], lambda n, c, t: console.shell_command(c, t), [], 2)
            with self.assertRaisesRegex(RuntimeError, 'rc=7'):
                jobs.run(0, 'exit 7')
            self.assertEqual(jobs.records[0]['returncode'], 7)
            jobs.stop(jobs.records[0])


if __name__ == '__main__':
    unittest.main()
