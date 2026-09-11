import importlib.util
from pathlib import Path
import sys
import unittest

DEPLOY = Path(__file__).resolve().parents[2] / 'deploy/giga-native'
sys.path.insert(0, str(DEPLOY))
import posix_frontend as frontend


class PosixFrontendTest(unittest.TestCase):
    def test_fixed_scaling_schedule_is_complete_and_balanced(self):
        import posix_io500_compare as comparison
        rows = comparison.schedule()
        self.assertEqual(len(rows), 45)
        self.assertEqual(len({r['name'] for r in rows}), 45)
        for point in comparison.POINTS:
            for mode in ('fuse', 'iov', 'legofs'):
                self.assertEqual(sum(r['topology'] == point and r['system'] == mode for r in rows), 3)
        self.assertEqual(rows[15]['topology'], '3c4s')

    def test_fuse_removes_inherited_iov_state(self):
        env = frontend.rank_environment({'LD_PRELOAD': '/wrong',
            'HF3FS_CXL_NATIVE_IOV': '1', 'HF3FS_CXL_POSIX_MOUNT': '/wrong',
            'KEEP': 'yes'}, 'fuse', Path('/view'), Path('/lib/posix.so'))
        self.assertEqual(env, {'KEEP': 'yes'})

    def test_iov_uses_rank_private_view_not_other_client(self):
        env = frontend.rank_environment({}, 'iov', Path('/private/view'), Path('/lib/posix.so'))
        self.assertEqual(env['HF3FS_CXL_POSIX_MOUNT'], '/private/view')
        self.assertEqual(env['HF3FS_CXL_NATIVE_IOV'], '1')
        self.assertEqual(env['HF3FS_CXL_POSIX_REPORT'], '1')
        self.assertEqual(env['LD_PRELOAD'], '/lib/posix.so')

    def test_counters_require_one_complete_staged_record_per_rank(self):
        row = dict(pid=42, direct_read_bytes=0, direct_write_bytes=0,
            staged_read_bytes=100, staged_write_bytes=200,
            staging_copy_in_bytes=200, staging_copy_out_bytes=100,
            rejected_calls=0, read_calls=1, write_calls=1)
        self.assertEqual(frontend.validate_counters([row], [42]), [row])
        auxiliary = {k: 0 for k in row}
        auxiliary['pid'] = 100
        self.assertEqual(frontend.validate_counters([auxiliary, row], [42]), [row])
        auxiliary['read_calls'] = 1
        with self.assertRaises(ValueError):
            frontend.validate_counters([auxiliary, row], [42])
        for rows, pids in (([], [42]), ([row, row], [42]), ([row], [43])):
            with self.assertRaises(ValueError):
                frontend.validate_counters(rows, pids)
        for key in ('direct_read_bytes', 'rejected_calls', 'staging_copy_in_bytes'):
            changed = dict(row, **{key: row[key] + 1})
            with self.assertRaises(ValueError):
                frontend.validate_counters([changed], [42])


if __name__ == '__main__':
    unittest.main()
