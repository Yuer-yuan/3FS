import copy
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / 'deploy/giga-native'))
import posix_io500_report as report
from topology import EXPECTED_PHASES

ROOT = PROJECT.parents[1] / 'fast-paper/artifact/io500/posix-cxl-scaling-20260912'


class ReportUnitTest(unittest.TestCase):
    def text(self):
        return ''.join(f'[RESULT] {p} 1.0 GiB/s : time 0.001 seconds\n' for p in EXPECTED_PHASES)

    def test_all22_parser(self):
        self.assertEqual(len(report.phases(self.text())), 22)

    def test_missing_duplicate_nonfinite_and_negative_fail(self):
        text = self.text()
        for broken in ('\n'.join(text.splitlines()[:-1]), text + text.splitlines()[0],
                       text.replace('1.0 GiB/s', 'nan GiB/s', 1),
                       text.replace('1.0 GiB/s', '-1.0 GiB/s', 1)):
            with self.assertRaises(ValueError):
                report.phases(broken)


@unittest.skipUnless((ROOT / 'cohort/inputs.json').exists(), 'archived experiment absent')
class ArchivedEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.inputs = json.loads((ROOT / 'cohort/inputs.json').read_text())
        self.matrix = json.loads((ROOT / 'cohort/matrix.json').read_text())

    def test_replay_first_three_paths(self):
        rows = self.matrix['cases'][:3]
        if len(rows) != 3 or any(r['status'] != 'passed' for r in rows):
            self.skipTest('first three paths not archived yet')
        for row in rows:
            self.assertEqual(len(report.verify_record(ROOT, row, self.inputs)), 22)

    def test_forged_phase_is_rejected(self):
        row = copy.deepcopy(self.matrix['cases'][0])
        row['verification']['phases'][0]['value'] += 1
        with self.assertRaisesRegex(ValueError, 'raw phase mismatch'):
            report.verify_record(ROOT, row, self.inputs)

    def test_changed_raw_result_is_rejected(self):
        row = copy.deepcopy(self.matrix['cases'][0])
        row['result_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'raw result hash mismatch'):
            report.verify_record(ROOT, row, self.inputs)

    def test_incomplete_is_not_final(self):
        state = copy.deepcopy(self.matrix)
        state['status'] = 'running'
        original = Path.read_text

        def read(path, *args, **kwargs):
            if path == ROOT / 'cohort/matrix.json':
                return json.dumps(state)
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, 'read_text', read):
            with self.assertRaisesRegex(ValueError, 'matrix incomplete'):
                report.summarize(ROOT)


if __name__ == '__main__':
    unittest.main()
