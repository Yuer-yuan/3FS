"""Evidence-only tests; original benchmark fixtures are never modified."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[4]
SPEC = importlib.util.spec_from_file_location('fs_macro_report',
    REPO / 'components/3FS/deploy/giga-native/posix_macro_report.py')
report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)
BASE = REPO / 'fast-paper/artifact/mlperf-storage'
ARCHIVE = BASE / 'comparison/posix-experiments/macro-fs-only-20260912'


class MacroEvidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (ARCHIVE / 'cohort/macro-fs-only-20260912-legofs-r1/result.json').exists():
            raise unittest.SkipTest('source-bound experiment archive is not installed')

    def validate(self, system='iov'):
        return report.validate(ARCHIVE / 'cohort' / ('macro-fs-only-20260912-' + system + '-r1'),
            system, report.read(ARCHIVE / 'bench-source-lock.json'),
            report.read(BASE / 'profiles/namespace-fast.json'))

    def test_all_nine_metrics(self):
        self.assertEqual(len(self.validate()['values']), 9)

    def test_legofs_complete(self):
        self.assertEqual(len(self.validate('legofs')['values']), 9)

    def test_preexec_evidence_not_mislabelled(self):
        evidence = self.validate()['proof']['path_counters']
        self.assertTrue(any('pre-exec' in row['placement_evidence'] for row in evidence))

    def test_partial_cases_rejected(self):
        original = report.read
        def changed(path):
            result = original(path)
            if path.name == 'result.json' and path.parent.name == 'macrobench':
                result['commands'].pop()
            return result
        with patch.object(report, 'read', changed), self.assertRaisesRegex(ValueError, 'incomplete case set'):
            self.validate()

    def test_corrupt_completed_bytes_rejected(self):
        original = report.read
        def changed(path):
            result = original(path)
            if path.name == 'kv-8b-storage.io-evidence.json':
                result['bytes_read'] += 1024
            return result
        with patch.object(report, 'read', changed), self.assertRaisesRegex(ValueError, 'KV byte mismatch'):
            self.validate()

    def test_shadow_copy_rejected(self):
        original = report.read
        def changed(path):
            result = original(path)
            if path.name == 'fs-frontend.json':
                result['commands'][0]['shadow_delta'][0] += 1
            return result
        with patch.object(report, 'read', changed), self.assertRaisesRegex(ValueError, 'shadow copy remains'):
            self.validate()

    def test_live_application_escape_rejected(self):
        original = report.read
        def changed(path):
            result = original(path)
            if path.name == 'fs-frontend.json':
                result['commands'][0]['placement']['status'] = 'Name:\tpython\nCpus_allowed_list:\t18-23\n'
            return result
        with patch.object(report, 'read', changed), self.assertRaisesRegex(ValueError, 'application CPU escape'):
            self.validate()


if __name__ == '__main__':
    unittest.main()
