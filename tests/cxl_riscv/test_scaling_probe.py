from argparse import Namespace
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).parents[2] / 'deploy/cxl-riscv'))
import full_stack
import run_phase1
import scaling_probe


class ScalingProbeTest(unittest.TestCase):
    def test_scaling_keeps_ten_client_service_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = []
            for count in (1, 2, 10):
                root = Path(tmp) / str(count)
                record = full_stack.render_config(root, 123, clients=count, region_length=2046*1024**2,
                                                  lane_count=800, cohort_config=True)
                self.assertEqual(len(record['manifest']['participants']), count + 5)
                roots.append(root / full_stack.CONFIG.removeprefix('/'))
            for file in roots[-1].glob('*.toml'):
                for root in roots[:-1]:
                    self.assertEqual((root / file.name).read_text(), file.read_text(), file.name)

    def test_single_client_diagnostic_stages_both_config_disks(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / 'base'; base.mkdir()
            roots, record = full_stack.stage_roots(base, Path(tmp) / 'run', 123, clients=1,
                region_length=2046*1024**2, lane_count=800, cohort_config=True)
            self.assertEqual(len(roots), 2)
            self.assertEqual(set(record['guest_files']), {0, 1})
            for node in range(2):
                for name in record['guest_files'][node]:
                    self.assertTrue((roots[node] / full_stack.CONFIG.removeprefix('/') / name).is_file())

    def test_acceptance_cannot_reduce_client_count(self):
        for workload in ('qualification', 'io500-standard', 'diagnostic-minimal'):
            with self.assertRaisesRegex(ValueError, 'only allowed for diagnostic-scale'):
                run_phase1.execute(Namespace(workload=workload, diagnostic_clients=1))

    def test_one_cell_records_real_io_stages_and_verifies_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = scaling_probe.io_command(str(Path(tmp) / 'payload'), 1)
            command = command.replace('/var/lib/3fs/', tmp + '/')
            done = subprocess.run(['/bin/sh', '-c', command], capture_output=True, text=True, timeout=15)
            self.assertEqual(done.returncode, 0, done.stderr)
            for stage in ('write_close_end', 'fsync_close_end', 'read_close_end'):
                self.assertIn('name=' + stage, done.stdout)
            self.assertEqual(done.stdout.count('HF3FS_SCALE_IO_OK'), 1)
            self.assertEqual((Path(tmp) / 'scale-output').read_bytes(), bytes(65536))

    def test_first_failed_stage_stops_and_restores_affinity(self):
        controls = []
        class Command:
            def __call__(self, node, text, timeout):
                controls.append(text)
                return 'mask set\n'
            diagnostic = __call__
            def workload(self, *args):
                raise TimeoutError('bounded diagnostic observation ended')
        with tempfile.TemporaryDirectory() as tmp:
            result = scaling_probe.execute([None, None], Command(), Path(tmp))
            self.assertTrue((Path(tmp) / 'scaling-probe.json').exists())
        self.assertEqual(len(result['rounds']), 1)
        self.assertEqual(controls[-1], scaling_probe.affinity_command('1'))
        self.assertIn('bounded diagnostic observation', result['first_failure'])
        self.assertFalse(result['acceptance_evidence'])
        self.assertNotIn('comparison_completed', result)


if __name__ == '__main__':
    unittest.main()
