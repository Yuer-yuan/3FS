from pathlib import Path
import json
import re
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[2] / "deploy/cxl-riscv"))
import full_stack
import run_g0
from test_g0_evidence import passing_result


def affinity_output():
    return ''.join(f"HF3FS_CXL_CPU role={role} pid={pid} threads={threads} cpus={cpus} checked=true\n"
                   for role, pid, threads, cpus in [('fdbserver', 108, 5, '0'), ('cxl-fabricd', 120, 3, '1-3'),
                       ('mgmtd_main', 130, 28, '1-3'), ('meta_main', 140, 57, '1-2'),
                       ('storage_main', 150, 233, '2-3')])


class FullStackTest(unittest.TestCase):
    def test_affinity_scan_uses_one_bounded_awk_per_process(self):
        command = full_stack.cpu_affinity_command()
        self.assertEqual(command.count("/bin/busybox awk"), 1)
        self.assertIn("/proc/$hf3fs_pid/task/*/status", command)
        self.assertIn("meta_main) hf3fs_expected=1-2", command)
        self.assertLess(len(command), 1000)

    def test_affinity_isolates_fdb_and_shares_remaining_server_cpus(self):
        records = full_stack.parse_cpu_affinity(affinity_output())
        self.assertTrue(full_stack.validate_cpu_affinity(records))
        records[3]['cpus'] = '1-3'
        self.assertFalse(full_stack.validate_cpu_affinity(records))

    def test_slow_fuse_start_can_finish_within_bounded_bootstrap_budget(self):
        class Console:
            def shell_command(self, text, timeout):
                if 'for hf3fs_role_pid in ' in text:
                    return affinity_output()
                if "HF3FS_CXL_META_CREATE_OK" in text:
                    return "HF3FS_CXL_META_CREATE_OK\n"
                if "HF3FS_CXL_META_OBSERVE_OK" in text:
                    return "HF3FS_CXL_META_OBSERVE_OK\n"
                if "HF3FS_CXL_META_REMOVE_OK" in text:
                    return "HF3FS_CXL_META_REMOVE_OK\n"
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
                submitted.append((self.node, text))
                if 'for hf3fs_role_pid in ' in text:
                    return affinity_output()
                if "HF3FS_CXL_META_CREATE_OK" in text:
                    return "HF3FS_CXL_META_CREATE_OK\n"
                if "HF3FS_CXL_META_OBSERVE_OK" in text:
                    return "HF3FS_CXL_META_OBSERVE_OK\n"
                if "HF3FS_CXL_META_REMOVE_OK" in text:
                    return "HF3FS_CXL_META_REMOVE_OK\n"
                if text.startswith('/opt/3fs/bin/hf3fs_fuse_main '):
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
            self.assertTrue(any(node == 0 and text.startswith(f'/bin/busybox taskset {mask} /opt/3fs/bin/{name} ')
                                for node, text in submitted), name)
        self.assertTrue(any(node == 1 and text.startswith('/bin/busybox rmdir /mnt/3fs/')
                            for node, text in submitted))

    def test_failure_logs_are_captured_before_teardown(self):
        events = []

        class Console:
            def __init__(self, node):
                self.node = node

            def shell_command(self, text, timeout):
                if text.startswith("ulimit -Hn") and self.node == 0:
                    raise RuntimeError("injected startup failure")
                if "HF3FS_FAILURE_CAUSES" in text:
                    events.append(("snapshot", self.node))
                if text.startswith("! grep -q ' /mnt/3fs '"):
                    events.append(("teardown", self.node))
                return ""

        with tempfile.TemporaryDirectory() as temporary:
            result = full_stack.execute([Console(0), Console(1)], Path(temporary), 10)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_log_snapshot_guests"], [0, 1])
        self.assertTrue(events)
        self.assertEqual(events[0][0], "snapshot")

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
        self.assertEqual(result["unresponsive_guests"], [0])
        self.assertEqual(len(consoles[0].commands), 1)
        self.assertGreater(len(consoles[1].commands), 0)

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
            self.assertEqual(meta["server"]["base"]["groups"][0]["io_worker"]["data_connect_timeout"], "120s")
            self.assertEqual(meta["server"]["storage_client"]["retry"]["init_wait_time"], "120s")
            fuse = tomllib.loads((directory / "hf3fs_fuse_main.toml").read_text())
            self.assertEqual(fuse["storage"]["retry"]["init_wait_time"], "120s")
            self.assertEqual(fuse["storage"]["retry"]["max_wait_time"], "120s")
            self.assertEqual(fuse["storage"]["retry"]["max_retry_time"], "360s")
            self.assertEqual(fuse["meta"]["retry_default"]["rpc_timeout"], "120s")
            self.assertEqual(fuse["meta"]["retry_default"]["retry_total_time"], "360s")
            launcher = tomllib.loads((directory / "meta_main_launcher.toml").read_text())
            self.assertEqual(launcher["client"]["io_worker"]["data_connect_timeout"], "120s")
            mgmtd = tomllib.loads((directory / "mgmtd_main.toml").read_text())
            self.assertEqual(mgmtd["server"]["service"]["lease_length"], "43200s")
            self.assertEqual(mgmtd["server"]["service"]["heartbeat_timestamp_valid_window"], "43200s")
            self.assertEqual(mgmtd["server"]["service"]["heartbeat_fail_interval"], "43200s")
            self.assertNotEqual(mgmtd["server"]["service"].get("validate_lease_on_write"), False)

    def test_ten_client_meta_uses_bounded_background_cadence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            full_stack.render_config(root, 42, clients=10)
            meta = tomllib.loads((root / full_stack.CONFIG.removeprefix("/") / "meta_main.toml").read_text())
        client = meta["server"]["mgmtd_client"]
        self.assertEqual(client["auto_extend_client_session_interval"], "10s")
        self.assertEqual(client["auto_heartbeat_interval"], "10s")
        self.assertEqual(client["auto_refresh_interval"], "10s")
        self.assertFalse(meta["server"]["meta"]["gc"]["enable"])
        self.assertEqual(meta["server"]["meta"]["gc"]["scan_interval"], "60s")
        self.assertEqual(meta["server"]["base"]["thread_pool"]["num_proc_threads"], 4)
        self.assertEqual(meta["server"]["base"]["thread_pool"]["num_io_threads"], 4)

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
