from pathlib import Path
import copy
import json
import sys
import tempfile
import threading
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[2] / 'deploy/cxl-riscv'))
import full_stack
import run_g0 as g0
import run_phase1 as phase1


def passing_result():
    manifest = dict(sessionGeneration=42, manifestSha256='a' * 64,
                    participants=[dict(endpoint=n) for n in (1, 2, 3, 4, 7, *range(16, 26))])
    records = []
    for endpoint in (1, 2, 3, 4, 7, *range(16, 26)):
        counters = dict.fromkeys(phase1.COUNTERS, 0)
        counters.update(tcp_receive_bytes=30, core_tcp_bytes=10, bootstrap_control_bytes=20,
                        cxl_rpc_requests=2, cxl_rpc_responses=2, cxl_rpc_bytes=128,
                        cxl_bulk_read_bytes=64, cxl_bulk_write_bytes=64)
        records.append(dict(schema='hf3fs.transport-counters.v1', guest=0 if endpoint < 16 else endpoint-15,
            pid=100+endpoint, session=42, endpoint=endpoint, generation=1, manifest_sha256='a'*64,
            scope='process-lifetime-receive', clean_shutdown=True, counters=counters))
    clients = [dict(guest=n, endpoint=15+n, bytes=2097152, sha256='b'*64, readback_sha256='b'*64,
                    io=dict(block_bytes=65536, write='direct', read='direct'),
                    neighbor_sha256='b'*64, neighbor_guest=n % 10 + 1, marker='HF3FS_CXL_FUSE_SMOKE_OK',
                    begin_observed_ns=100+n, end_observed_ns=200+n) for n in range(1, 11)]
    probe = dict(status='passed', generation=1, checksum=12, bytes=full_stack.REGION_OFFSET,
                 payload_bytes=phase1.DAX_PROBE_PAYLOAD_BYTES,
                 atomic_u32_lock_free=True, atomic_u64_lock_free=True)
    directions = [dict(writer_host=a, reader_host=b, writer=probe.copy(), reader=probe.copy())
                  for n in range(1, 11) for a, b in ((0, n), (n, 0))]
    return dict(schema=phase1.SCHEMA, result_class='qualification', first_failure=None,
        build_manifest_sha256='c'*64, source_closure_sha256='d'*64,
        g0_b=dict(verified=True, build_manifest_sha256='c'*64, source_closure_sha256='d'*64),
        capacity=phase1.CAPACITY, lane_count=phase1.LANES,
        configuration=dict(manifest=manifest), dax_directions=directions,
        guests=[dict(host_id=n, backing_inode=[1, 100+n], readiness=dict(size=phase1.CAPACITY,
                     device='/dev/dax0.0', fuse_device=True)) for n in range(11)],
        coherence=dict(complete=True), fdb=dict(guest_server=True, guest=0, bind_address='127.0.0.1:4500',
                                              api_version=710, set_ok=True, get_ok=True, value_match=True,
                                              server_knobs=dict(max_read_transaction_life_versions=60_000_000,
                                                                max_write_transaction_life_versions=60_000_000,
                                                                commit_proxy_liveness_timeout=120,
                                                                system_monitor_interval=30,
                                                                worker_logging_interval=30,
                                                                storage_logging_delay=30,
                                                                run_loop_profiling_interval=0)),
        applications=dict(status='passed', clean_retirement=True, ready=list(full_stack.APPLICATIONS),
                          cpu_affinity={stage: [dict(role=role, pid=pid, threads=threads, cpus=cpus, checked=True)
                              for role, pid, threads, cpus in [('fdbserver', 108, 5, '0'), ('cxl-fabricd', 120, 3, '1-3'),
                                  ('mgmtd_main', 130, 28, '1-3'), ('meta_main', 140, 57, '1-2'),
                                  ('storage_main', 150, 233, '2-3')]]
                              for stage in ('before_io', 'after_io')},
                          clients=clients, transport_records=records, region_offset=full_stack.REGION_OFFSET,
                          region_length=phase1.REGION_LENGTH),
        teardown=dict(all_owned_processes_stopped=True, qemu_returncodes=[0]*11, cxlmemsim_returncode=0))


class Phase1EvidenceTest(unittest.TestCase):
    def test_reference_reuses_g0_when_platform_and_staged_binaries_are_identical(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result_path = root / 'g0-result.json'
            handoff_path = root / 'g0-handoff.json'
            old_build, old_source = '1' * 64, '2' * 64
            current_build, current_source = '3' * 64, '4' * 64
            platform_sha, binary_sha, contract_sha = '5' * 64, '6' * 64, '7' * 64
            result = dict(
                status='passed', first_failure=None,
                build_manifest_sha256=old_build, source_closure_sha256=old_source,
                platform_contract_sha256=contract_sha,
                platform_artifacts={'qemu': {'sha256': platform_sha}},
                binaries={'storage_main': {'sha256': binary_sha}},
            )
            result_path.write_text(json.dumps(result))
            handoff = dict(
                schema='hf3fs.cxl-riscv-g0-b-handoff.v1',
                result_path=str(result_path), result_sha256=g0.sha256_file(result_path),
                build_manifest_sha256=old_build, source_closure_sha256=old_source,
                platform_contract_sha256=contract_sha,
            )
            handoff_path.write_text(json.dumps(handoff))
            rootfs = dict(
                build_manifest_sha256=current_build, source_closure_sha256=current_source,
                binaries={'storage_main': {'sha256': binary_sha}},
            )
            artifacts = {'qemu': {'sha256': platform_sha}}
            with patch.object(g0, 'validate_result', return_value=[]):
                verified = phase1.verify_reference(handoff_path, rootfs, artifacts)
                self.assertTrue(verified['verified'])
                self.assertEqual(verified['compatibility'], 'exact-platform-and-staged-binary-hashes')
                self.assertEqual(verified['build_manifest_sha256'], current_build)
                self.assertEqual(verified['source_closure_sha256'], current_source)
                self.assertEqual(verified['g0_build_manifest_sha256'], old_build)
                self.assertEqual(verified['g0_source_closure_sha256'], old_source)

                changed_platform = {'qemu': {'sha256': '8' * 64}}
                with self.assertRaisesRegex(ValueError, 'platform artifact changed'):
                    phase1.verify_reference(handoff_path, rootfs, changed_platform)
                changed_rootfs = copy.deepcopy(rootfs)
                changed_rootfs['binaries']['storage_main']['sha256'] = '9' * 64
                with self.assertRaisesRegex(ValueError, 'staged binary changed'):
                    phase1.verify_reference(handoff_path, changed_rootfs, artifacts)

    def test_cpu_isolation_requires_real_thread_masks_and_stable_processes(self):
        result = passing_result()
        records = result['applications']['cpu_affinity']['before_io']
        output = "prompt# printf 'HF3FS_CXL_CPU role=fdbserver pid=99 threads=1 cpus=0 checked=true'\r\r\n"
        output += ''.join('HF3FS_CXL_CPU role={role} pid={pid} threads={threads} cpus={cpus} checked=true\r\r\n'.format(**r)
                          for r in records)
        self.assertEqual(full_stack.parse_cpu_affinity(output), records)
        for replacement in ([], records + [records[0]], records[:-1],
                            [dict(r, cpus='0-3') for r in records]):
            bad = copy.deepcopy(result)
            bad['applications']['cpu_affinity']['before_io'] = replacement
            self.assertIn('resident guest CPU affinity is absent or overlaps', phase1.validate_phase1_result(bad))
        result['applications']['cpu_affinity']['after_io'][0].update(pid=972)
        self.assertIn('resident processes changed across the workload', phase1.validate_phase1_result(result))
        with self.assertRaises(ValueError):
            full_stack.parse_cpu_affinity("prompt# HF3FS_CXL_CPU role=fdbserver pid=99 threads=1 cpus=0 checked=true")

    def test_client_io_profile_must_record_direct_64k_requests(self):
        result = passing_result()
        result['applications']['clients'][0]['io'] = dict(block_bytes=65536, write='buffered', read='direct')
        self.assertIn('client I/O profile is absent or changed', phase1.validate_phase1_result(result))
        result['applications']['clients'][0].pop('io')
        self.assertIn('client I/O profile is absent or changed', phase1.validate_phase1_result(result))

    def test_fdb_deadlines_fit_emulated_cohort_without_changing_g0(self):
        command = g0.fdb_server_command(clients=10)
        self.assertTrue(command.startswith('/bin/busybox taskset 1 /opt/foundationdb/bin/fdbserver '))
        self.assertNotIn('taskset', g0.fdb_server_command())
        self.assertNotIn('--knob-', g0.fdb_server_command())
        for name, value in passing_result()['fdb']['server_knobs'].items():
            self.assertIn(f'--knob-{name} {value} ', command)
        self.assertIn('-p 127.0.0.1:4500 ', command)

    def test_fdb_experimental_budget_must_be_recorded(self):
        result = passing_result()
        result['fdb'].pop('server_knobs')
        self.assertIn('FoundationDB simulation budget is absent or changed', phase1.validate_phase1_result(result))
        result = passing_result()
        result['fdb']['server_knobs']['commit_proxy_liveness_timeout'] = 0
        self.assertIn('FoundationDB simulation budget is absent or changed', phase1.validate_phase1_result(result))

    def test_fdb_diagnostics_profile_is_bounded_and_cannot_silently_change(self):
        command = g0.fdb_server_command(clients=10)
        # The rootfs interactive BusyBox shell has a 1024-byte input limit;
        # reserve space for shell_command's nonce and return-code trailer.
        self.assertLess(len(command.encode()) + 128, 1024)
        self.assertNotIn('--knob-trace_flush_interval', command)
        self.assertNotIn('--knob-min_trace_severity', command)
        for knob in ('system_monitor_interval', 'worker_logging_interval',
                     'storage_logging_delay', 'run_loop_profiling_interval'):
            result = passing_result()
            result['fdb']['server_knobs'].pop(knob)
            self.assertIn('FoundationDB simulation budget is absent or changed', phase1.validate_phase1_result(result))

    def test_serial_observations_accept_real_double_cr_and_ignore_echoed_commands(self):
        class Console:
            condition = threading.Lock()
            output = ("prompt# printf 'HF3FS_CXL_IO_%s node=10\\n' BEGIN\r\r\n"
                      "HF3FS_CXL_IO_BEGIN node=10\r\r\n"
                      "dd completed\r\r\nHF3FS_CXL_IO_END node=10\r\r\n")
            split = output.index('dd completed')
            output_chunks = [(split, 100), (len(output), 200)]
        self.assertEqual(full_stack.observe_io_markers(Console(), 10, 0),
                         dict(begin_observed_ns=100, end_observed_ns=200))
        with self.assertRaises(ValueError):
            full_stack.observe_io_markers(Console(), 9, 0)

    def test_complete_qualification_and_forbidden_paths(self):
        result = passing_result()
        self.assertEqual(phase1.validate_phase1_result(result), [])
        for counter in ('rdma_open_attempts', 'tcp_data_plane_bytes', 'bootstrap_serving_bytes'):
            with self.subTest(counter=counter):
                bad = copy.deepcopy(result)
                bad['applications']['transport_records'][0]['counters'][counter] += 1
                self.assertIn(counter + ' must be zero', phase1.validate_phase1_result(bad))

    def test_missing_duplicate_wrong_generation_and_unclassified_evidence(self):
        result = passing_result()
        record = result['applications']['transport_records'].pop()
        self.assertIn('transport evidence does not cover every endpoint', phase1.validate_phase1_result(result))
        result['applications']['transport_records'].extend([record, record])
        self.assertIn('duplicate process transport evidence', phase1.validate_phase1_result(result))
        result = passing_result()
        result['applications']['transport_records'][0]['session'] = 41
        result['applications']['transport_records'][1]['counters']['tcp_receive_bytes'] += 4
        errors = phase1.validate_phase1_result(result)
        self.assertIn('transport identity or retirement mismatch', errors)
        self.assertIn('unclassified TCP receive bytes remain', errors)
        result = passing_result()
        admin = next(r for r in result['applications']['transport_records'] if r['endpoint'] == 7)
        admin['generation'] = 2
        self.assertIn('admin generation evidence has a gap or duplicate', phase1.validate_phase1_result(result))

    def test_checksums_overlap_fdb_and_cleanup_cannot_be_inferred_from_exit_zero(self):
        result = passing_result()
        result['applications']['clients'][-1]['begin_observed_ns'] = 999
        result['applications']['clients'][0]['neighbor_sha256'] = '0'*64
        result['fdb']['bind_address'] = '10.73.0.2:4500'
        result['teardown']['qemu_returncodes'][3] = -9
        errors = phase1.validate_phase1_result(result)
        self.assertIn('ten client I/O intervals did not overlap in serial observations', errors)
        self.assertIn('client or cross-client finite FUSE verification failed', errors)
        self.assertIn('machine-local TCP FoundationDB transaction did not pass', errors)
        self.assertIn('owned process cleanup is incomplete or forced', errors)

    def test_reference_build_and_distinct_guests_required(self):
        result = passing_result()
        result['source_closure_sha256'] = '0'*64
        result['guests'][1]['backing_inode'] = result['guests'][0]['backing_inode']
        result['dax_directions'].pop()
        errors = phase1.validate_phase1_result(result)
        self.assertIn('G0-B artifact identity mismatch', errors)
        self.assertIn('guest CXL backing files are not all distinct', errors)
        self.assertIn('DAX publication does not cover both directions for every client', errors)

    def test_ten_fuse_configs_have_unique_endpoints_and_identical_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifests = []
            for node in range(11):
                root = Path(temporary) / str(node)
                record = full_stack.render_config(root, 42, clients=10, node=node,
                                                  region_length=phase1.REGION_LENGTH, lane_count=phase1.LANES)
                manifests.append(record['manifest'])
                directory = root / full_stack.CONFIG.removeprefix('/')
                for path in directory.glob('*.toml'):
                    config = tomllib.loads(path.read_text())
                    if 'cxl' in config:
                        self.assertEqual(config['cxl']['region_length'], '2046MB')
                        self.assertEqual(config['cxl']['poll_sleep'], '1ms')
                mgmtd = tomllib.loads((directory / 'mgmtd_main.toml').read_text())
                self.assertEqual(mgmtd['server']['service']['lease_length'], '43200s')
                self.assertEqual(mgmtd['server']['service']['heartbeat_timestamp_valid_window'], '43200s')
                self.assertEqual(mgmtd['server']['service']['heartbeat_fail_interval'], '43200s')
                self.assertNotEqual(mgmtd['server']['service'].get('validate_lease_on_write'), False)
                if node:
                    config = tomllib.loads((directory / 'hf3fs_fuse_main_launcher.toml').read_text())
                    self.assertEqual(config['cxl']['endpoint'], 15 + node)
                    self.assertEqual(config['client']['default_timeout'], '120s')
                    self.assertEqual(config['client']['thread_pool']['num_io_threads'], 1)
            storage = tomllib.loads((directory / 'storage_main.toml').read_text())
            self.assertEqual(storage['server']['base']['thread_pool']['num_io_threads'], 10)
            self.assertEqual(storage['server']['base']['thread_pool']['num_proc_threads'], 10)
            self.assertEqual(storage['server']['storage']['write_worker']['num_threads'], 20)
            self.assertEqual(storage['server']['storage']['write_worker']['bg_num_threads'], 2)
            self.assertEqual(storage['server']['aio_read_worker']['num_threads'], 4)
            self.assertEqual(storage['server']['sync_worker']['num_threads'], 1)
            self.assertEqual(storage['server']['coroutines_pool_update']['threads_num'], 4)
            for pool in ('coroutines_pool_read', 'coroutines_pool_sync', 'coroutines_pool_default'):
                self.assertEqual(storage['server'][pool]['threads_num'], 1)
            buffers = storage['server']['buffer_pool']
            self.assertEqual(buffers['buffer_count'] * 512 * 1024 +
                             buffers['big_buffer_count'] * 4 * 1024 * 1024, 16 * 1024 * 1024)
            self.assertTrue(all(m == manifests[0] for m in manifests))
            self.assertEqual(len(manifests[0]['participants']), 15)
            self.assertEqual(manifests[0]['laneCount'] // manifests[0]['endpointCount'], 32)
            bulk = next(r['length'] for r in manifests[0]['ranges'] if r['kind'] == 'BulkArenas')
            self.assertGreater(bulk // 15, 64 * 1024**2)

    def test_platform_geometry_and_loopback_network_are_explicit(self):
        paths = g0.PlatformPaths(*(Path('/platform/' + n) for n in ('qemu','sim','topo','sbi','uboot','kernel')))
        common = dict(coherence_port=12345, root_image=Path('/run/root'), endpoint_memory=Path('/run/cxl'), lsa=Path('/run/lsa'))
        with self.assertRaises(ValueError):
            g0.build_qemu_command(paths, g0.RuntimeConfig(), node=10, **common)
        command = g0.build_qemu_command(paths, g0.RuntimeConfig(), node=10, guest_count=11,
                                       capacity=phase1.CAPACITY, **common)
        self.assertIn('size=2048M', ' '.join(command))
        self.assertIn('coherence-v2-host-id=10', ' '.join(command))
        self.assertIn('coherence-v2-write-through=off', ' '.join(command))
        self.assertIn('localaddr=127.0.0.1', ' '.join(phase1.network_args(10, '239.73.1.2', 20000)))
        with self.assertRaises(ValueError):
            phase1.network_args(11, '239.73.1.2', 20000)


if __name__ == '__main__':
    unittest.main()
