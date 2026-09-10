from pathlib import Path
import json
import re
import sys
import tempfile
import threading
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[2] / "deploy/cxl-riscv"))
import full_stack
import run_g0
from test_g0_evidence import passing_result


def affinity_output():
    return ''.join(f"HF3FS_CXL_CPU role={role} pid={pid} threads={threads} cpus={cpus} checked=true\n"
                   for role, pid, threads, cpus in [('fdbserver', 108, 5, '0'), ('cxl-fabricd', 120, 3, '3'),
                       ('mgmtd_main', 130, 28, '1'), ('meta_main', 140, 57, '2-3'),
                       ('storage_main', 150, 233, '3')])


class FullStackTest(unittest.TestCase):
    def setUp(self):
        # These orchestration consoles model commands, not file transfer. The
        # bounded counter export has a real-shell integration test separately.
        counters = patch('full_stack.capture_transport_counters', return_value=dict(verified=True, records=[]))
        counters.start()
        self.addCleanup(counters.stop)

    def test_affinity_scan_uses_one_bounded_awk_per_process(self):
        command = full_stack.cpu_affinity_command()
        self.assertEqual(command.count("/bin/busybox awk"), 1)
        self.assertIn("/proc/$hf3fs_pid/task/*/status", command)
        self.assertIn("meta_main) hf3fs_expected=2-3", command)
        self.assertLess(len(command), 1000)

    def test_affinity_isolates_fdb_and_reserves_mgmtd_cpu(self):
        records = full_stack.parse_cpu_affinity(affinity_output())
        self.assertTrue(full_stack.validate_cpu_affinity(records))
        records[3]['cpus'] = '1-3'
        self.assertFalse(full_stack.validate_cpu_affinity(records))

    def test_slow_fuse_start_can_finish_within_bounded_bootstrap_budget(self):
        class Console:
            def shell_command(self, text, timeout):
                if 'for hf3fs_role_pid in ' in text:
                    return affinity_output()
                if text.startswith('attempt=0; while [ ! -d /mnt/3fs/test ]'):
                    attempts = int(re.search(r'\[ \$attempt -lt (\d+) \]', text)[1])
                    # Configuration and client initialization can legitimately
                    # take longer than one RPC while staying within retries.
                    if attempts <= 180:
                        raise RuntimeError('startup ended while client initialization was still progressing')
                    if not 180 < timeout <= 720:
                        raise RuntimeError('bootstrap lacks a finite independent host deadline')
                return ''

        with tempfile.TemporaryDirectory() as temporary, patch('full_stack.concurrent_io', return_value=[]):
            result = full_stack.execute([Console() for _ in range(11)], Path(temporary), 1000, clients=10)
        self.assertEqual(result['status'], 'passed', result.get('first_failure'))

    def test_all_clients_start_before_parallel_mount_readiness(self):
        started, mounted, submitted = set(), set(), []

        class Console:
            def __init__(self, node):
                self.node = node

            def shell_command(self, text, timeout):
                if "\n" in text or "\r" in text:
                    raise AssertionError("shell command contains a physical line break")
                submitted.append((self.node, text))
                if 'for hf3fs_role_pid in ' in text:
                    return affinity_output()
                if '/opt/3fs/bin/hf3fs_fuse_main ' in text:
                    started.add(self.node)
                elif text.startswith('attempt=0; while [ ! -d /mnt/3fs/test ]'):
                    if started != set(range(1, 11)):
                        raise RuntimeError('mount readiness began before every client was started')
                    mounted.add(self.node)
                return ''

        def io(consoles, command, clients):
            self.assertEqual(started, set(range(1, 11)))
            self.assertEqual(mounted, started)
            return []

        with tempfile.TemporaryDirectory() as temporary, patch('full_stack.concurrent_io', side_effect=io) as workload:
            result = full_stack.execute([Console(n) for n in range(11)], Path(temporary), 10, clients=10)
        self.assertEqual(result['status'], 'passed', result.get('first_failure'))
        workload.assert_called_once()
        for name, mask in full_stack.SERVER_CPU_MASKS.items():
            self.assertTrue(any(node == 0 and text.startswith(f'/bin/busybox taskset {mask} ')
                                and f'/opt/3fs/bin/{name} ' in text
                                for node, text in submitted), name)
        server_starts = [text for node, text in submitted
                         if node == 0 and text.startswith('/bin/busybox taskset ')
                         and '_main --app_cfg' in text]
        self.assertEqual(
            [re.search(r'/opt/3fs/bin/(\w+)_main ', text)[1] for text in server_starts],
            ['mgmtd', 'meta', 'storage'],
        )
        self.assertTrue(all(any(node == client and "HF3FS_BOOTSTRAP_NETWORK_READY" in text
                                for node, text in submitted) for client in range(1, 11)))
        meta_ready = next(text for node, text in submitted
                          if node == 0 and "HF3FS_SERVICE_WAIT name=meta" in text)
        self.assertIn("log_ready=", meta_ready)
        self.assertIn("port_ready=", meta_ready)
        self.assertIn("HF3FS_SERVICE_STARTUP_MILESTONES name=meta", meta_ready)
        self.assertIn("HF3FS_SERVICE_STARTUP_THREADS name=meta", meta_ready)
        self.assertFalse(any('HF3FS_CXL_META_' in text or '/bin/busybox rmdir /mnt/3fs/' in text
                             for _, text in submitted))
        meta_start = next(text for node, text in submitted if node == 0 and '/opt/3fs/bin/meta_main ' in text)
        self.assertIn('HF3FS_RPC_TRACE_DIR=/var/log/3fs', meta_start)
        self.assertIn('HF3FS_RPC_TRACE_DIR=/var/log/3fs', meta_start)
        fuse_starts = [text for node, text in submitted if node > 0 and '/opt/3fs/bin/hf3fs_fuse_main ' in text]
        self.assertTrue(fuse_starts)
        self.assertTrue(all('HF3FS_RPC_TRACE_DIR=/var/log/3fs' in text for text in fuse_starts))

    def test_failure_logs_are_captured_before_teardown(self):
        events = []

        class Console:
            def __init__(self, node):
                self.node = node

            def shell_command(self, text, timeout):
                if text.startswith("ulimit -Hn") and self.node == 0:
                    raise RuntimeError("injected startup failure")
                if "HF3FS_LIVE_" in text:
                    events.append(("snapshot", self.node))
                if text.startswith("! grep -q ' /mnt/3fs '"):
                    events.append(("teardown", self.node))
                return ""

        def capture(command, node, text, destination, **kwargs):
            command(node, text, 30)
            return dict(outcome="passed", verified=True)

        with tempfile.TemporaryDirectory() as temporary, patch('diagnostics.capture', side_effect=capture):
            directory = Path(temporary)
            result = full_stack.execute([Console(0), Console(1)], directory, 10)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["failure_log_snapshot_guests"], [0, 1])
            self.assertTrue(events)
            self.assertEqual(events[0][0], "snapshot")
            commands = json.loads((directory / "full-commands.json").read_text())
            self.assertTrue(all(entry["host_end_ns"] >= entry["host_start_ns"] for entry in commands))
            self.assertTrue(all(entry["duration_ms"] >= 0 for entry in commands))
            failed = next(entry for entry in commands if entry["command"].startswith("ulimit -Hn"))
            self.assertEqual(failed["outcome"], "failed")
            self.assertIn("injected startup failure", failed["error"])
            snapshots = [entry for entry in commands if "HF3FS_LIVE_" in entry["command"]]
            self.assertTrue(snapshots)
            self.assertTrue(all(len(entry["command"]) < 800 for entry in snapshots))

    def test_live_diagnostics_read_proc_and_fuse_state_without_touching_mount(self):
        commands = full_stack.live_diagnostic_commands(1, "standard")
        combined = "\n".join(command for _, command in commands)
        self.assertIn("$p/wchan", combined)
        self.assertIn("$p/task/", combined)
        self.assertIn("/sys/fs/fuse/connections", combined)
        self.assertIn("/var/log/3fs/fuse.log", combined)
        self.assertNotIn("/mnt/3fs", combined)
        self.assertTrue(all(len(command) < 800 for _, command in commands))

        server_commands = dict(full_stack.live_diagnostic_commands(0, "standard"))
        self.assertIn("FDBTransaction.cc", server_commands["service_events"])
        self.assertIn("rpc-*.trace", server_commands["rpc_meta"])
        self.assertIn("head -c 256", server_commands["rpc_meta"])
        self.assertIn("/proc/$hf3fs_pid_meta_main", server_commands["server_threads"])
        self.assertNotIn("/proc/[0-9]*", server_commands["process"])
        self.assertNotIn("syscall", server_commands["process"])
        self.assertNotIn("cat $p/io", server_commands["process"])
        self.assertIn("SvrProc*", server_commands["meta_worker_stacks"])
        self.assertNotIn("$p/stack", server_commands["process"])
        self.assertTrue(all(len(command) < 800 for command in server_commands.values()))

    def test_network_failure_samples_both_ends_with_short_commands(self):
        class Console:
            def __init__(self, node):
                self.node = node

            def shell_command(self, text, timeout):
                if "until /bin/busybox ping" in text:
                    raise RuntimeError("injected network failure")
                if "HF3FS_NETWORK_FAILURE" in text:
                    return f"HF3FS_NETWORK_FAILURE node={self.node}\n"
                return ""

        with tempfile.TemporaryDirectory() as temporary:
            result = full_stack.execute([Console(0), Console(1)], Path(temporary), 10)
            commands = json.loads((Path(temporary) / "full-commands.json").read_text())
        self.assertEqual([entry["guest"] for entry in result["network_failure_diagnostics"]], [0, 1])
        network_commands = [entry["command"] for entry in commands
                            if "HF3FS_NETWORK_FAILURE" in entry["command"]]
        self.assertEqual(len(network_commands), 2)
        self.assertTrue(all(len(command) < 800 for command in network_commands))

    def test_default_qualification_does_not_schedule_live_collector(self):
        caller = threading.current_thread()

        class Console:
            def shell_command(self, text, timeout):
                if 'for hf3fs_role_pid in ' in text:
                    return affinity_output()
                return ""

        def finite_io(*_):
            self.assertIs(threading.current_thread(), caller,
                          'qualification unexpectedly entered the diagnostic worker')
            return []

        with tempfile.TemporaryDirectory() as temporary, patch('full_stack.concurrent_io', side_effect=finite_io):
            result = full_stack.execute([Console() for _ in range(11)], Path(temporary), 10,
                                        clients=10, rpc_trace=False)
        self.assertEqual(result['status'], 'passed', result.get('first_failure'))
        self.assertNotIn('live_diagnostics', result)
        self.assertEqual(result['diagnostic_policy'], dict(rpc_trace=False, live_server_interval_seconds=0))

    def test_blocked_qualification_samples_server_before_timeout(self):
        released = threading.Event()

        class Console:
            def __init__(self, node):
                self.node = node

            def shell_command(self, text, timeout):
                if 'for hf3fs_role_pid in ' in text:
                    return affinity_output()
                if self.node == 0 and "HF3FS_LIVE_SYSTEM" in text:
                    released.set()
                return ""

        def blocked_io(*_):
            if not released.wait(1):
                raise TimeoutError("server diagnostic did not run")
            return []

        with tempfile.TemporaryDirectory() as temporary, patch('full_stack.concurrent_io', side_effect=blocked_io):
            result = full_stack.execute(
                [Console(node) for node in range(11)], Path(temporary), 10, clients=10,
                diagnostic_interval=0.01)
        self.assertEqual(result["status"], "passed", result.get("first_failure"))
        self.assertTrue(result["live_diagnostics"])
        sample = result["live_diagnostics"][0]
        self.assertEqual(sample["phase"], "qualification")
        self.assertIsNone(sample["blocked_guest"])
        self.assertEqual(sample["sections"][0]["name"], "system")
        self.assertNotIn("server_threads", {section["name"] for section in sample["sections"]})

    def test_unresponsive_console_gets_no_more_teardown_commands(self):
        class Console:
            def __init__(self, stalled):
                self.stalled, self.commands = stalled, []

            def shell_command(self, text, timeout):
                self.commands.append(text)
                if self.stalled:
                    raise TimeoutError("test guest did not complete its command")
                return ""

        consoles = [Console(True), Console(False)]
        with tempfile.TemporaryDirectory() as temporary:
            result = full_stack.execute(consoles, Path(temporary), 10)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["clean_retirement"])
        self.assertEqual(result["service_startup_budget_seconds"], 10)
        self.assertEqual(result["unresponsive_guests"], [0])
        self.assertEqual(len(consoles[0].commands), 1)
        self.assertGreater(len(consoles[1].commands), 0)

    def test_business_and_collection_failure_do_not_imply_retirement_failure(self):
        class Console:
            def shell_command(self, text, timeout):
                return affinity_output() if 'for hf3fs_role_pid in ' in text else ''
        with tempfile.TemporaryDirectory() as tmp, \
                patch('full_stack.concurrent_io', side_effect=RuntimeError('original IO failure')), \
                patch('diagnostics.capture', return_value=dict(outcome='failed', error='collection failed')):
            result = full_stack.execute([Console() for _ in range(11)], Path(tmp), 10,
                                        clients=10, rpc_trace=False, diagnostic_interval=0)
        self.assertEqual(result['first_failure'], 'original IO failure')
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['clean_retirement'])
        self.assertTrue(result['failure_log_snapshot_errors'])

    def test_teardown_failure_is_preserved_separately_from_original_io_failure(self):
        class Console:
            def shell_command(self, text, timeout):
                if text.startswith('kill $hf3fs_pid_cxl_fabricd;'):
                    raise RuntimeError('fabric did not retire')
                return affinity_output() if 'for hf3fs_role_pid in ' in text else ''
        with tempfile.TemporaryDirectory() as tmp, \
                patch('full_stack.concurrent_io', side_effect=RuntimeError('original IO failure')), \
                patch('diagnostics.capture', return_value=dict(outcome='failed', error='collection failed')):
            result = full_stack.execute([Console() for _ in range(11)], Path(tmp), 10,
                                        clients=10, rpc_trace=False, diagnostic_interval=0)
        self.assertEqual(result['first_failure'], 'original IO failure')
        self.assertFalse(result['clean_retirement'])
        self.assertIn('fabric did not retire', result['retirement_errors'][0]['error'])

    def test_exported_trace_and_validation_receipts_are_combined(self):
        import hashlib
        class Console:
            def shell_command(self, text, timeout):
                if 'HF3FS_TRACE_FILE' in text:
                    return 'HF3FS_TRACE_FILE /var/log/3fs/rpc-123.trace\n'
                if 'dd if=/dev/urandom of=/var/lib/3fs/input' in text:
                    return ('a' * 64 + '  /var/lib/3fs/input\n' + 'a' * 64 +
                            '  /var/lib/3fs/output\nHF3FS_CXL_FUSE_SMOKE_OK bytes=2097152\n')
                return ''
        def export(command, node, path, destination, **kwargs):
            data = ('HF3FS_RPC_TRACE_V1 attempted=0 committed=0 dropped=0 errors=0 bytes=0 capacity=4194304'
                    .ljust(255) + '\n').encode()
            destination.write_bytes(data)
            return dict(path=str(destination), bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), verified=True)
        with tempfile.TemporaryDirectory() as tmp, patch('diagnostics.export_file', side_effect=export):
            result = full_stack.execute([Console(), Console()], Path(tmp), 10)
        self.assertEqual(result['status'], 'passed', result)
        self.assertNotIn('collection_errors', result)
        self.assertEqual(len(result['request_traces']), 2)
        self.assertTrue(all(r['verified'] and r['complete'] for r in result['request_traces']))

    def test_failed_trace_export_does_not_skip_remaining_processes(self):
        class Console:
            def shell_command(self, text, timeout):
                if 'HF3FS_TRACE_FILE' in text:
                    return ('HF3FS_TRACE_FILE /var/log/3fs/rpc-123.trace\n'
                            'HF3FS_TRACE_FILE /var/log/3fs/rpc-456.trace\n')
                if 'dd if=/dev/urandom of=/var/lib/3fs/input' in text:
                    return ('a' * 64 + '  /var/lib/3fs/input\n' + 'a' * 64 +
                            '  /var/lib/3fs/output\nHF3FS_CXL_FUSE_SMOKE_OK bytes=2097152\n')
                return ''
        def export(command, node, path, destination, **kwargs):
            if path.endswith('123.trace'):
                raise ValueError('first process trace is truncated')
            return dict(path=str(destination), verified=True)
        with tempfile.TemporaryDirectory() as tmp, \
                patch('diagnostics.export_file', side_effect=export), \
                patch('diagnostics.validate_rpc_trace', return_value=dict(complete=True)):
            result = full_stack.execute([Console(), Console()], Path(tmp), 10)
        self.assertEqual(len(result.get('request_traces', [])), 2, result)
        self.assertEqual(len(result['collection_errors']), 2)
        self.assertTrue(all(e['path'].endswith('123.trace') for e in result['collection_errors']))

    def test_rendered_configs_are_valid_dax_and_keep_tcp_control(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = full_stack.render_config(root, 42)
            directory = root / full_stack.CONFIG.removeprefix("/")
            for path in directory.glob("*.toml"):
                config = tomllib.loads(path.read_text())
                if "cxl" in config:
                    self.assertEqual(config["cxl"]["region_type"], "Dax")
                    self.assertEqual(config["cxl"]["region_path"], "/dev/dax0.0")
                    self.assertEqual(config["cxl"]["region_offset"], "2MB")
                    self.assertEqual(config["cxl"]["region_length"], "254MB")
                    self.assertEqual(config["cxl"]["poll_sleep"], "1ms")
            self.assertEqual(record["manifest"]["sessionGeneration"], 42)
            clients = [p for p in record["manifest"]["participants"] if p["endpoint"] == 16]
            self.assertEqual(clients[0]["localAddress"], "CXL://10.73.0.3:12516")
            meta = tomllib.loads((directory / "meta_main.toml").read_text())
            self.assertEqual(meta["server"]["fdb"]["clusterFile"], "/opt/3fs/etc/fdb.cluster")
            self.assertFalse(meta["server"]["meta"]["gc"]["enable"])
            self.assertEqual(meta["server"]["meta"]["gc"]["scan_interval"], "60s")
            self.assertEqual(meta["server"]["mgmtd_client"]["auto_refresh_interval"], "10s")
            self.assertEqual(meta["server"]["base"]["groups"][0]["io_worker"]["data_connect_timeout"], "120s")
            self.assertEqual(meta["server"]["storage_client"]["retry"]["init_wait_time"], "120s")
            fuse = tomllib.loads((directory / "hf3fs_fuse_main.toml").read_text())
            self.assertEqual(fuse["storage"]["retry"]["init_wait_time"], "120s")
            self.assertEqual(fuse["storage"]["retry"]["max_wait_time"], "120s")
            self.assertEqual(fuse["storage"]["retry"]["max_retry_time"], "360s")
            self.assertEqual(fuse["meta"]["retry_default"]["rpc_timeout"], "120s")
            self.assertEqual(fuse["meta"]["retry_default"]["retry_total_time"], "360s")
            self.assertFalse(fuse["client"]["io_worker"]["read_write_data_in_event_thread"])
            launcher = tomllib.loads((directory / "meta_main_launcher.toml").read_text())
            self.assertEqual(launcher["client"]["io_worker"]["data_connect_timeout"], "120s")
            mgmtd = tomllib.loads((directory / "mgmtd_main.toml").read_text())
            self.assertEqual(mgmtd["server"]["service"]["lease_length"], "43200s")
            self.assertEqual(mgmtd["server"]["service"]["heartbeat_timestamp_valid_window"], "43200s")
            self.assertEqual(mgmtd["server"]["service"]["heartbeat_fail_interval"], "43200s")
            self.assertNotEqual(mgmtd["server"]["service"].get("validate_lease_on_write"), False)
            self.assertEqual(mgmtd["server"]["base"]["thread_pool"]["num_proc_threads"], 1)
            self.assertEqual(mgmtd["server"]["base"]["thread_pool"]["num_io_threads"], 1)
            self.assertEqual(meta["server"]["base"]["thread_pool"]["num_proc_threads"], 2)
            storage = tomllib.loads((directory / "storage_main.toml").read_text())
            self.assertEqual(storage["server"]["base"]["thread_pool"]["num_proc_threads"], 2)
            self.assertEqual(storage["server"]["base"]["thread_pool"]["num_io_threads"], 2)
            self.assertEqual(storage["server"]["storage"]["write_worker"]["num_threads"], 4)

    def test_ten_client_meta_uses_bounded_background_cadence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            full_stack.render_config(root, 42, clients=10)
            meta = tomllib.loads((root / full_stack.CONFIG.removeprefix("/") / "meta_main.toml").read_text())
            fuse = tomllib.loads(
                (root / full_stack.CONFIG.removeprefix("/") / "hf3fs_fuse_main.toml").read_text())
            launcher = tomllib.loads(
                (root / full_stack.CONFIG.removeprefix("/") / "hf3fs_fuse_main_launcher.toml").read_text())
        client = meta["server"]["mgmtd_client"]
        self.assertEqual(fuse["periodic_sync"]["interval"], "300s")
        self.assertEqual(fuse["client"]["thread_pool"]["num_proc_threads"], 2)
        self.assertEqual(fuse["client"]["thread_pool"]["num_io_threads"], 2)
        self.assertTrue(fuse["client"]["io_worker"]["read_write_data_in_event_thread"])
        self.assertTrue(fuse["storage"]["net_client"]["io_worker"]["read_write_data_in_event_thread"])
        self.assertEqual(launcher["mgmtd_client"]["auto_extend_client_session_interval"], "30s")
        self.assertEqual(launcher["mgmtd_client"]["auto_heartbeat_interval"], "30s")
        self.assertEqual(launcher["mgmtd_client"]["auto_refresh_interval"], "30s")
        self.assertEqual(client["auto_extend_client_session_interval"], "10s")
        self.assertEqual(client["auto_heartbeat_interval"], "10s")
        self.assertEqual(client["auto_refresh_interval"], "10s")
        self.assertFalse(meta["server"]["meta"]["gc"]["enable"])
        self.assertEqual(meta["server"]["meta"]["gc"]["scan_interval"], "60s")
        self.assertEqual(meta["server"]["base"]["thread_pool"]["num_proc_threads"], 4)
        self.assertEqual(meta["server"]["base"]["thread_pool"]["num_io_threads"], 4)
        self.assertEqual(meta["server"]["meta"]["operation_timeout"], "90s")

    def test_stage_roots_only_materializes_overlay_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base"
            run = root / "run"
            base.mkdir()
            run.mkdir()
            (base / "large-rootfs-file").write_bytes(b"base-only")
            roots, record = full_stack.stage_roots(base, run, 42, clients=1)
            self.assertEqual(len(roots), 2)
            self.assertFalse(any((item / "large-rootfs-file").exists() for item in roots))
            self.assertTrue(all((item / full_stack.CONFIG.removeprefix("/")).is_dir() for item in roots))
            self.assertIn("manifest_sha256", record)

    def test_platform_only_result_cannot_pass_full_profile(self):
        errors = run_g0.validate_result(passing_result(), "full-3fs")
        self.assertIn("full 3FS application readiness is incomplete", errors)
        self.assertIn("G0-B build/source identity is incomplete", errors)

    def test_full_evidence_rejects_read_mismatch_and_missing_retirement(self):
        result = dict(status="passed", region_type="Dax", server_guest=0, fuse_guest=1,
                      region_offset=full_stack.REGION_OFFSET, region_length=full_stack.REGION_LENGTH,
                      ready=list(full_stack.APPLICATIONS), bytes=2097152, sha256="a" * 64,
                      readback_sha256="a" * 64, marker="HF3FS_CXL_FUSE_SMOKE_OK", clean_retirement=True)
        self.assertEqual(full_stack.validate_evidence(result), [])
        result.update(readback_sha256="b" * 64, clean_retirement=False)
        self.assertEqual(len(full_stack.validate_evidence(result)), 2)

    def test_network_uses_owned_loopback_socket_without_host_setup(self):
        self.assertIn("socket,id=control,listen=127.0.0.1:12345", full_stack.network_args(0, 12345))
        self.assertIn("socket,id=control,connect=127.0.0.1:12345", full_stack.network_args(1, 12345))
        with self.assertRaises(ValueError):
            full_stack.network_args(1, 0)

    def test_handoff_rejects_partial_results_and_preserves_existing_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            result_path, handoff = directory / "result.json", directory / "handoff.json"
            result = passing_result()
            result_path.write_text(json.dumps(result))
            with self.assertRaises(run_g0.G0Error):
                run_g0.write_full_handoff(result_path, handoff)
            self.assertFalse(handoff.exists())
            result.update(status="passed", gate="G0-B", build_manifest_sha256="a" * 64,
                          source_closure_sha256="b" * 64, platform_contract_sha256="c" * 64,
                          full_stack=dict(status="passed", region_type="Dax", server_guest=0, fuse_guest=1,
                                          region_offset=full_stack.REGION_OFFSET, region_length=full_stack.REGION_LENGTH,
                                          ready=list(full_stack.APPLICATIONS), bytes=2097152, sha256="d" * 64,
                                          readback_sha256="d" * 64, marker="HF3FS_CXL_FUSE_SMOKE_OK",
                                          clean_retirement=True))
            result["binaries"].update({name: {"machine": "RISC-V", "needed": []}
                                       for name in full_stack.APPLICATIONS})
            result_path.write_text(json.dumps(result))
            run_g0.write_full_handoff(result_path, handoff)
            original = handoff.read_bytes()
            run_g0.write_full_handoff(result_path, handoff)
            second = directory / "second.json"
            second.write_bytes(result_path.read_bytes())
            with self.assertRaises(run_g0.G0Error):
                run_g0.write_full_handoff(second, handoff)
            self.assertEqual(handoff.read_bytes(), original)
            self.assertEqual(json.loads(original)["result_sha256"], run_g0.sha256_file(result_path))


if __name__ == "__main__":
    unittest.main()
