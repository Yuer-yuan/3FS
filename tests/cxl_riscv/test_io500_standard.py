from pathlib import Path
import copy
import configparser
import hashlib
import json
import os
import select
import shutil
import signal
import subprocess
import time
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[2] / 'deploy/cxl-riscv'))
import io500_standard as io500
import prepare_io500
import run_phase1
from test_phase1_evidence import passing_result


def official_text():
    return ('[run]\nprocs = 10\nmode = standard\nresult-dir = /mnt/3fs/io500-standard-results\n'
            'version = io500-isc26_v2-13-ga69cf60cf765\n') + '\n'.join(f'[{name}]\nscore = 1.0\nt_delta = 300.0\nt_start = 2026-09-06 00:00:00\n'
                     't_end = 2026-09-06 00:05:00\n' for name in io500.PHASES) + \
           '\n[SCORE]\nMD = 1.0\nBW = 1.0\nSCORE = 1.0\nhash = 12345\n'


def clock_samples(delta=0):
    return [dict(guest=n, host='server0' if n == 0 else f'client{n-1}',
                 realtime_ns=1700000000000000000+delta, monotonic_before_ns=1000000000+delta,
                 monotonic_after_ns=1000000001+delta, host_send_ns=900000000+delta,
                 host_receive_ns=1100000000+delta, set_epoch=None) for n in range(11)]


def passing_standard():
    result = passing_result()
    result.update(schema=io500.SCHEMA, result_class='measured', workload='io500-standard',
                  io500_bundle=dict(manifest_sha256='a'*64, record=dict(schema=prepare_io500.SCHEMA)),
                  clock_initialization=[dict(guest=n, set_epoch=1700000000) for n in range(11)])
    result['applications'].pop('clients')
    mpi = dict(returncode=0, proxy_returncodes=[0]*10, proxy_commands=[['proxy']]*10,
               clean_exit=True, host_start_ns=1, host_end_ns=4000*10**9)
    windows = [dict(name=name, seconds=300.0, host_window_seconds=300.0, observed_ns=(n+1)*300*10**9+1)
               for n, name in enumerate(io500.PHASES)]
    result['applications']['workload'] = dict(status='passed', profile='standard', stonewall_seconds=300,
        clients=10, ranks=10, clock_mutations_during_workload=False,
        clocks_before=clock_samples(), clocks_after=clock_samples(4000*10**9),
        probe=copy.deepcopy(mpi), standard=dict(mpi, banner_observed_ns=1, phases=windows),
        rank_placement=[dict(rank=n, guest=n+1, host=f'client{n}', endpoint=n+16, pid=200+n) for n in range(10)],
        placement_probe=dict(records=[dict(rank=n) for n in range(10)], realtime_overlap_ns=10**9),
        metrics=io500.parse_metrics(official_text()),
        files={n: dict(bytes=1024, sha256='a'*64) for n in ('config.ini', 'result.txt')},
        verifier=dict(returncode=0, ok=True, invalid=False, sha256='b'*64))
    return result


class IO500StandardTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('cc'), 'a native C compiler is needed for the PTY relay check')
    def test_linebuf_relay_flushes_lines_and_preserves_child_status(self):
        deploy = Path(__file__).parents[2]/'deploy/cxl-riscv'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            relay, child = root/'relay', root/'child'
            source = root/'child.c'
            source.write_text('#include <stdio.h>\n#include <unistd.h>\n'
                              'int main(void) { printf("ready\\n"); sleep(1); return 7; }\n')
            subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror',
                            str(deploy/'io500_linebuf.c'), '-o', str(relay)], check=True)
            subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror',
                            str(source), '-o', str(child)], check=True)
            process = subprocess.Popen([str(relay), str(child)], stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True)
            try:
                readable, _, _ = select.select([process.stdout], [], [], 0.5)
                self.assertTrue(readable, 'child output remained buffered behind the relay')
                self.assertEqual(process.stdout.readline(), 'ready\n')
                self.assertEqual(process.wait(timeout=2), 7)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
                process.stdout.close()

    def test_image_cache_identity_tracks_content_not_run_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            base = workspace / 'base.json'
            base.write_text('{"rootfs":"stable"}\n')
            roots = [workspace / name for name in ('full-026', 'full-027')]
            records = []
            for index, root in enumerate(roots):
                binary = root / 'opt/3fs/bin/storage_main'
                binary.parent.mkdir(parents=True)
                binary.write_text('same binary')
                records.append(dict(
                    base_rootfs_manifest=str(base), source_closure_sha256=str(index) * 64,
                    binaries={'storage_main': {'path': str(binary), 'sha256': 'a' * 64}},
                    runtime_files={'lib/libc.so': 'b' * 64},
                    guest_tools={'files': {'usr/bin/lsblk': 'c' * 64}},
                    bootstrap_files={'init': 'f' * 64},
                    io500_bundle={'manifest_sha256': 'd' * 64}))
            first, content = run_phase1.rootfs_image_identity(roots[0], records[0], 8192)
            second, _ = run_phase1.rootfs_image_identity(roots[1], records[1], 8192)
            self.assertEqual(first, second)
            self.assertEqual(content['image_bytes'], 8192)
            records[1]['runtime_files']['lib/libc.so'] = 'e' * 64
            changed, _ = run_phase1.rootfs_image_identity(roots[1], records[1], 8192)
            self.assertNotEqual(first, changed)
            records[1]['runtime_files']['lib/libc.so'] = 'b' * 64
            records[1]['bootstrap_files']['init'] = '0' * 64
            changed, _ = run_phase1.rootfs_image_identity(roots[1], records[1], 8192)
            self.assertNotEqual(first, changed)

    def test_measured_result_reuses_cohort_gates_without_smoke_substitution(self):
        result = passing_standard()
        self.assertEqual(run_phase1.validate_phase1_result(result), [])
        for mutate in (lambda r: r['applications']['transport_records'][0]['counters'].update(tcp_data_plane_bytes=1),
                       lambda r: r['teardown'].update(qemu_returncodes=[0]*10+[1]),
                       lambda r: r['g0_b'].update(verified=False),
                       lambda r: r['coherence'].update(complete=False)):
            bad = copy.deepcopy(result)
            mutate(bad)
            self.assertTrue(run_phase1.validate_phase1_result(bad))
        qualified = passing_result()
        qualified.update(schema=io500.SCHEMA, result_class='measured')
        self.assertTrue(run_phase1.validate_phase1_result(qualified))

    def test_reject_incomplete_verifier_phase_and_clock_evidence(self):
        for field, replacement in [('verifier', dict(returncode=1, ok=True, invalid=False)),
                                   ('rank_placement', []), ('clocks_after', []),
                                   ('stonewall_seconds', 30), ('clock_mutations_during_workload', True),
                                   ('files', {}), ('metrics', {})]:
            result = passing_standard()
            result['applications']['workload'][field] = replacement
            self.assertTrue(io500.validate_result(result), field)
        result = passing_standard()
        result['applications']['workload']['standard']['phases'][3]['seconds'] = 601
        self.assertTrue(io500.validate_result(result))

    def test_reference_only_allows_mount_root_rewrite(self):
        reference = Path(__file__).parents[4] / 'configs/io500-standard.ini'
        text = reference.read_text()
        normalized = prepare_io500.normalize_config(text)
        self.assertIn('datadir = /mnt/3fs/io500-standard', normalized)
        self.assertEqual(prepare_io500.normalize_config(text, normalized), normalized)
        for replacement in (normalized.replace('300', '30'), normalized + '\n[ior-easy]\ntransferSize=64k\n'):
            with self.assertRaises(ValueError):
                prepare_io500.normalize_config(text, replacement)
        with self.assertRaises(ValueError):
            prepare_io500.normalize_config(text.replace('300', '30'))

    def test_hydra_and_rank_output_can_follow_the_interactive_prompt(self):
        lines = ''.join(f'HYDRA_LAUNCH: /opt/io500/bin/hydra_pmi_proxy --proxy-id {n}\r\r\n'
                        for n in range(10))
        self.assertEqual(len(io500.parse_proxy_commands('hf3fs-g0-1# ' + lines)), 10)
        ranks = ''.join(f'HF3FS_IO500_RANK rank={n} guest={n+1} host=client{n} pid=123 endpoint={n+16}\n'
                        for n in range(10))
        self.assertEqual(len(io500.parse_ranks('hf3fs-g0-1# ' + ranks)), 10)
        probe = ''.join(f'HF3FS_IO500_PROBE rank={n} size=10 host=client{n} pid=123 begin_ns=100 end_ns=300 '
                        'monotonic_begin_ns=100 monotonic_end_ns=300\n' for n in range(10))
        self.assertEqual(len(io500.parse_probe('hf3fs-g0-1# ' + probe)['records']), 10)
        concatenated = probe.replace(
            'monotonic_end_ns=300\nHF3FS_IO500_PROBE rank=7',
            'monotonic_end_ns=300HF3FS_IO500_PROBE rank=7')
        self.assertEqual(len(io500.parse_probe(concatenated)['records']), 10)
        concatenated_ranks = ranks.replace(
            'endpoint=22\nHF3FS_IO500_RANK rank=7',
            'endpoint=22HF3FS_IO500_RANK rank=7')
        self.assertEqual(len(io500.parse_ranks(concatenated_ranks)), 10)

    @unittest.skipUnless(Path('/bin/busybox').exists(), 'native BusyBox is needed for the real process-session check')
    def test_owned_mpi_cleanup_does_not_wait_for_its_unreaped_leader(self):
        deploy = Path(__file__).parents[2]/'deploy/cxl-riscv'
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            env = dict(os.environ, HF3FS_IO500_JOB_DIR=str(directory))
            with (directory/'job.log').open('w') as log:
                job = subprocess.Popen(['/bin/busybox', 'setsid', '/bin/sh', str(deploy/'io500_job.sh'),
                                        'probe', 'mpi', '/bin/sleep', '120'], stdout=log, stderr=log, env=env)
                try:
                    ledger = directory/'io500-job-probe-mpi'
                    deadline = time.monotonic()+5
                    while not ledger.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(ledger.exists())
                    self.assertEqual(int(ledger.read_text().split()[0]), job.pid)
                    # Do not reap the session leader until the stopper returns:
                    # an exited child remains a zombie, which cannot be killed.
                    stopped = subprocess.run(['/bin/sh', str(deploy/'io500_stop_job.sh'), 'probe', 'mpi'],
                                             env=env, text=True, capture_output=True, timeout=10)
                    self.assertEqual(stopped.returncode, 0, stopped.stdout+stopped.stderr)
                    job.wait(timeout=2)
                finally:
                    try: os.killpg(job.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    job.wait(timeout=5)

    def test_manual_proxies_reject_duplicate_missing_and_wrong_executable(self):
        lines = [f'HYDRA_LAUNCH: /opt/io500/bin/hydra_pmi_proxy --control-port 10.73.0.3:2222 --proxy-id {n}\r\n'
                 for n in range(10)]
        parsed = io500.parse_proxy_commands(''.join(lines))
        self.assertEqual([int(p[-1]) for p in parsed], list(range(10)))
        for text in (''.join(lines[:-1]), ''.join(lines[:-1]+[lines[0]]),
                     ''.join(lines).replace('/opt/io500/bin/hydra_pmi_proxy', '/tmp/proxy', 1)):
            with self.assertRaises(ValueError):
                io500.parse_proxy_commands(text)

    def test_watchdog_handles_fragmentation_and_all_thirteen_phases(self):
        watchdog = io500.PhaseWatchdog()
        watchdog.observe('IO500 ver', 1)
        watchdog.observe('sion pinned\n', 1)
        for n, name in enumerate(io500.PHASES):
            now = (n+1)*300*10**9+1
            watchdog.observe(f'[RESULT] {name} 1.000 GiB/s : time 300.0 sec', now)
            watchdog.observe('onds\r\n', now)
        self.assertEqual([r['name'] for r in watchdog.records], list(io500.PHASES))
        watchdog.observe('', 5000*10**9)

    def test_watchdog_stops_on_the_first_fatal_ior_output(self):
        for line in ('WARNING: write(15, 0x7fffa7d29000, 2097152) failed I/O error',
                     'WARNING: read(15, 0x7fffa7d29000, 2097152) failed I/O error',
                     'Assertion failed: rc >= 0 (aiori-POSIX.c: POSIX_Xfer: 818)'):
            watchdog = io500.PhaseWatchdog()
            watchdog.observe('IO500 version pinned\nWARNING: nonfatal informational warning\n', 1)
            watchdog.observe(line[:20], 10)
            with self.assertRaisesRegex(RuntimeError, 'fatal IO500 output'):
                watchdog.observe(line[20:]+'\r\r\n', 11)
            self.assertEqual(watchdog.failure, dict(line=line, observed_ns=11,
                                                   phase='ior-easy-write', completed_phases=0))
            self.assertEqual(watchdog.records, [])

    def test_watchdog_rejects_timeout_invalid_duplicate_and_out_of_order(self):
        for content in ('[INVALID] result\n', 'ior-hard-write 1 GiB/s : time 300 seconds\n',
                        'ior-easy-write 1 GiB/s : time nan seconds\n',
                        'ior-easy-write 1 GiB/s : time 601 seconds\n'):
            watchdog = io500.PhaseWatchdog()
            watchdog.observe('IO500 version pinned\n', 1)
            with self.assertRaises((ValueError, TimeoutError)):
                watchdog.observe(content, 300*10**9)
        watchdog = io500.PhaseWatchdog()
        watchdog.observe('IO500 version pinned\n', 1)
        with self.assertRaises(TimeoutError):
            watchdog.observe('still alive\n', 601*10**9)
        watchdog = io500.PhaseWatchdog()
        watchdog.observe('IO500 version pinned\n', 1)
        line = 'ior-easy-write 1 GiB/s : time 300 seconds\n'
        watchdog.observe(line, 300*10**9)
        with self.assertRaises(ValueError):
            watchdog.observe(line, 301*10**9)

    def test_official_metrics_require_unique_complete_finite_phases(self):
        text = official_text()
        metrics = io500.parse_metrics(text)
        self.assertEqual(len(metrics['phases']), 13)
        self.assertEqual(metrics['official']['score'], 1.0)
        for bad in (text.replace('[ior-hard-write]', '[missing]'), text+text,
                    text.replace('t_delta = 300.0', 't_delta = 601', 1),
                    text.replace('score = 1.0', 'score = nan', 1), text + '\n[INVALID]\n'):
            with self.assertRaises((ValueError, configparser.Error)):
                io500.parse_metrics(bad)

    def test_clock_sampling_rejects_realtime_steps_and_guest_host_rate_divergence(self):
        before, after = clock_samples(), clock_samples(4000*10**9)
        self.assertEqual(io500.clock_validation(before, after), [])
        after[0]['realtime_ns'] += 10**10
        self.assertTrue(io500.clock_validation(before, after))
        after = clock_samples(4000*10**9)
        after[0]['monotonic_before_ns'] += 10**10
        after[0]['monotonic_after_ns'] += 10**10
        self.assertTrue(io500.clock_validation(before, after))
        after = clock_samples(4000*10**9)
        after[0]['set_epoch'] = 1700000000
        self.assertTrue(io500.clock_validation(before, after))

    def test_rank_records_prove_guest_and_endpoint_identity(self):
        text = ''.join(f'HF3FS_IO500_RANK rank={n} guest={n+1} host=client{n} pid=123 endpoint={n+16}\n'
                       for n in range(10))
        self.assertEqual(len(io500.parse_ranks(text)), 10)
        for bad in (text+text, text.replace('host=client9', 'host=client0'), text.replace('endpoint=25', 'endpoint=24')):
            with self.assertRaises(ValueError):
                io500.parse_ranks(bad)

    def test_probe_requires_real_overlap_and_ten_hosts(self):
        text = ''.join(f'HF3FS_IO500_PROBE rank={n} size=10 host=client{n} pid=123 begin_ns=100 end_ns=300 '
                       'monotonic_begin_ns=100 monotonic_end_ns=300\n' for n in range(10))
        self.assertEqual(io500.parse_probe(text)['realtime_overlap_ns'], 200)
        with self.assertRaises(ValueError):
            io500.parse_probe(text.replace('host=client9', 'host=client0'))
        with self.assertRaises(ValueError):
            io500.parse_probe(text.replace('begin_ns=100 end_ns=300', 'begin_ns=400 end_ns=500', 1))

    def test_every_fresh_guest_resolves_every_mpi_peer_hostname(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = [Path(tmp)/str(n) for n in range(11)]
            for root in roots:
                (root/'opt/io500/etc').mkdir(parents=True)
            record = io500.stage_roots(roots)
            for node, root in enumerate(roots):
                hosts = dict(line.split()[::-1] for line in (root/'etc/hosts').read_text().splitlines())
                self.assertEqual(hosts['server0'], '10.73.0.2')
                self.assertEqual([hosts[f'client{n}'] for n in range(10)],
                                 [f'10.73.0.{n+3}' for n in range(10)])
                self.assertEqual((root/'opt/io500/etc/guest-id').read_text(), f'{node}\n')
                self.assertEqual(record['guest_files'][node]['etc/hosts'],
                                 hashlib.sha256((root/'etc/hosts').read_bytes()).hexdigest())

    def test_failed_mpi_preflight_prevents_starting_the_3fs_services(self):
        class Console:
            def __init__(self): self.commands = []
            def shell_command(self, text, timeout):
                self.commands.append(text)
                return ''
        import full_stack
        consoles = [Console() for _ in range(11)]
        calls = []
        def prepare(consoles, command, run):
            calls.append('preflight')
            return dict(status='failed', first_failure='MPI peer address unresolved')
        with tempfile.TemporaryDirectory() as tmp:
            result = full_stack.execute(consoles, Path(tmp), 60, clients=10, prepare_workload=prepare)
        self.assertEqual(calls, ['preflight'])
        self.assertEqual(result['first_failure'], 'MPI peer address unresolved')
        self.assertFalse(any('/opt/3fs/bin/cxl-fabricd ' in cmd for console in consoles for cmd in console.commands))

    def test_existing_identical_musl_loader_is_reused_and_conflicts_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bundle, root = base/'bundle', base/'root'
            (bundle/'lib').mkdir(parents=True)
            (root/'lib').mkdir(parents=True)
            data = b'identical-musl-runtime'
            (bundle/'lib/libc.so').write_bytes(data)
            loader = root/'lib/ld-musl-riscv64.so.1'
            loader.write_bytes(data)
            manifest = bundle/'manifest.json'
            manifest.write_text('{}')
            verified = dict(files={'lib/libc.so': dict(sha256=hashlib.sha256(data).hexdigest(), elf={})})
            with mock.patch.object(prepare_io500, 'verify', return_value=verified):
                prepare_io500.install(manifest, root)
                self.assertFalse(loader.is_symlink())
                self.assertEqual(loader.read_bytes(), data)
            other = base/'conflict'
            (other/'lib').mkdir(parents=True)
            (other/'lib/ld-musl-riscv64.so.1').write_bytes(b'different-runtime')
            with mock.patch.object(prepare_io500, 'verify', return_value=verified):
                with self.assertRaisesRegex(ValueError, 'loader differs'):
                    prepare_io500.install(manifest, other)
            self.assertEqual((other/'lib/ld-musl-riscv64.so.1').read_bytes(), b'different-runtime')

    def test_bundle_hash_and_dependency_escape_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'test').write_text('one')
            manifest = root/'manifest.json'
            manifest.write_text(json.dumps(dict(schema=prepare_io500.SCHEMA, status='passed',
                files={'test': dict(sha256=hashlib.sha256(b'one').hexdigest())})))
            (root/'test').write_text('two')
            with self.assertRaisesRegex(ValueError, 'artifact changed'):
                prepare_io500.verify(manifest)
            (root/'test').unlink()
            (root/'test').symlink_to('/etc/passwd')
            with self.assertRaisesRegex(ValueError, 'escapes'):
                prepare_io500.verify(manifest)
        for interpreter, needed in (('/lib/ld-linux.so', []), (prepare_io500.LOADER, ['libbadfs_intercept.so'])):
            info = mock.Mock(interpreter=interpreter, needed=needed)
            with mock.patch.object(prepare_io500.stage, 'inspect_required_elf', return_value=info):
                with self.assertRaises(ValueError):
                    prepare_io500.validate_elf(Path('test'))


if __name__ == '__main__':
    unittest.main()
