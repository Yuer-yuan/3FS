from pathlib import Path
import re
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parents[2] / 'deploy/cxl-riscv'))
import minimal_requests


class MinimalRequestsTest(unittest.TestCase):
    def test_single_then_ten_clients_and_named_non_acceptance_result(self):
        def command(node, text, timeout):
            self.assertEqual(timeout, 600)
            if 'testRpc.command' in text:
                identity = re.search(r"printf '(\d+)", text)[1]
                return f'HF3FS_DIAGNOSTIC_TEST_RPC sequence={identity} status=0\n'
            action = re.search(r'--diagnostic (\S+)', text)[1]
            return f'HF3FS_DIAGNOSTIC_IO action={action} errno=0\n'
        with tempfile.TemporaryDirectory() as tmp, patch('full_stack.concurrent_io', return_value=[]) as io:
            result = minimal_requests.execute([None] * 11, command, Path(tmp))
        self.assertEqual(result['status'], 'passed', result)
        self.assertFalse(result['acceptance_evidence'])
        self.assertEqual(len(result['operations']), 43)
        for action in ('testRpc', 'stat-existing', 'stat-missing'):
            entries = [r for r in result['operations'] if r['action'] == action]
            self.assertEqual(entries[0]['concurrency'], 1)
            self.assertEqual({r['guest'] for r in entries[1:]}, set(range(1, 11)))
        io.assert_called_once()

    def test_failed_health_response_stops_before_business_load(self):
        with tempfile.TemporaryDirectory() as tmp, patch('full_stack.concurrent_io') as io:
            result = minimal_requests.execute([None] * 11, lambda *_: 'wrong identity', Path(tmp))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(result['operations']), 1)
        io.assert_not_called()

    def test_health_command_uses_available_applets_and_checks_publication(self):
        texts = []
        def command(node, text, timeout):
            texts.append(text)
            raise RuntimeError('capture only')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            minimal_requests.execute([], command, root)
            tools = root / 'bin'; tools.mkdir()
            for name in ('grep', 'cat', 'sleep'):
                (tools / name).symlink_to(shutil.which(name))
            text = texts[0].replace('/run/hf3fs-diagnostic', str(root))
            env = dict(os.environ, PATH=str(tools))
            (root / 'testRpc.result').write_text('HF3FS_DIAGNOSTIC_TEST_RPC sequence=10 status=0\n')
            done = subprocess.run(['/bin/sh', '-c', text], env=env, capture_output=True, timeout=2)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual((root / 'testRpc.command').read_text(), '10\n')
            (root / 'testRpc.result').unlink()
            (root / 'testRpc.command.tmp').mkdir()
            failed = subprocess.run(['/bin/sh', '-c', text], env=env, capture_output=True, timeout=2)
            self.assertNotEqual(failed.returncode, 0)

    def test_retirement_injection_requires_successful_business_work(self):
        with tempfile.TemporaryDirectory() as tmp, patch('minimal_requests.execute', return_value={'status': 'passed'}) as work:
            commands = []
            result = minimal_requests.execute_and_retire([], lambda *args: commands.append(args), Path(tmp))
            self.assertEqual(result['status'], 'failed')
            self.assertTrue(result['failure_injection']['business_passed'])
            self.assertFalse(result['failure_injection']['acceptance_evidence'])
            self.assertEqual(len(commands), 1)
            self.assertTrue((Path(tmp) / 'retirement-probe.json').exists())

    def test_retirement_injection_preserves_business_failure(self):
        failed = {'status': 'failed', 'first_failure': 'original business failure'}
        with tempfile.TemporaryDirectory() as tmp, patch('minimal_requests.execute', return_value=failed):
            commands = []
            result = minimal_requests.execute_and_retire([], lambda *args: commands.append(args), Path(tmp))
            self.assertIs(result, failed)
            self.assertFalse(commands)
            self.assertFalse((Path(tmp) / 'retirement-probe.json').exists())
