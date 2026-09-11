import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from types import SimpleNamespace
from unittest import mock
from types import SimpleNamespace


PROJECT = Path(__file__).resolve().parents[2]
DEPLOY = PROJECT / "deploy/giga-native"
sys.path.insert(0, str(DEPLOY))
SPEC = importlib.util.spec_from_file_location("hf3fs_giga_cluster", DEPLOY / "cluster.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ClusterTest(unittest.TestCase):
    def test_meta_readiness_requires_completed_non_discarded_routing_view(self):
        received = 'MgmtdClient: get new routing info version 15\n'
        completed = '[MgmtdClientOp RefreshRoutingInfo force=false No.1] succeeded. latency: 1ms\n'
        self.assertEqual(MODULE._completed_routing_version(received), 0)
        self.assertEqual(MODULE._completed_routing_version(received + completed), 15)
        discarded = 'MgmtdClient: discard incomplete routing info version 15, [10003] still connecting\n'
        self.assertEqual(MODULE._completed_routing_version(received + discarded + completed), 0)
        self.assertEqual(MODULE._completed_routing_version(received.replace('15', '6') + completed + received), 6)

    def test_partial_storage_start_cleans_all_owned_processes_and_preserves_first_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cluster = MODULE.NativeCluster.__new__(MODULE.NativeCluster)
            cluster.roots = SimpleNamespace(bundle=root)
            cluster.repo = root
            cluster.config = SimpleNamespace(rpc_trace_methods=None)
            cluster.library_path = str(root)
            cluster.topology = MODULE.topology_module.topology('2c2s')
            cluster.processes = []
            cluster.mounts = []
            cluster.ledger = MODULE.runtime.ResultLedger(root/'result.json', dict(status='running', start_order=[], stop_order=[]))
            cluster.prepare = mock.Mock()
            cluster._transport_records = mock.Mock(return_value=[])
            stopped = []
            def start():
                for role in ('fdbserver', 'cxl-fabricd', 'mgmtd', 'meta', 'storage'):
                    process = mock.Mock()
                    process.receipt.role = role
                    process.process.poll.return_value = 0
                    process.stop.side_effect = lambda *args, r=role: stopped.append(r) or {'role': r}
                    cluster.processes.append(process)
                # The second OwnedProcess.start fails before returning a receipt.
                with mock.patch.object(MODULE.runtime.OwnedProcess, 'start', side_effect=OSError('second storage failed')):
                    cluster._start('storage-1', Path('/bin/false'), [], (16,17))
            cluster.start = start
            with mock.patch.object(MODULE, 'NativeCluster', return_value=cluster), mock.patch.object(MODULE.runtime, 'port_is_free', return_value=True):
                result = MODULE.execute_cluster(None, mock.Mock())
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['first_failure']['message'], 'second storage failed')
            self.assertEqual(stopped, ['storage', 'meta', 'mgmtd', 'cxl-fabricd', 'fdbserver'])
            self.assertTrue(result['teardown']['all_owned_processes_stopped'])
            with mock.patch.object(MODULE.runtime, 'port_is_free', return_value=False):
                self.assertFalse(cluster.stop()['all_owned_processes_stopped'])

    def target_cluster(self, root, servers=1, overrides=None):
        active = MODULE.NativeCluster.__new__(MODULE.NativeCluster)
        active.topology = MODULE.topology_module.topology(f"1c{servers}s")
        active.roots = SimpleNamespace(bundle=root / "bundle")
        active.ledger = mock.Mock()
        active.storage_paths = {
            node.node_id: tuple(root / str(node.node_id) / f"data{i + 1}"
                                for i in range(4 // servers))
            for node in active.topology.storage_nodes
        }

        def create(arguments, role):
            fields = {key: arguments[arguments.index(key) + 1] for key in
                      ("--node-id", "--disk-index", "--target-id", "--chain-id")}
            parent = active.storage_paths[int(fields["--node-id"])][int(fields["--disk-index"])]
            directory = parent / fields["--target-id"]
            directory.mkdir(parents=True)
            config = dict(path=str(directory), target_id=int(fields["--target-id"]),
                          chain_id=int(fields["--chain-id"]), only_chunk_engine=True)
            config.update(overrides or {})
            text = "\n".join(f"{key} = {json.dumps(value)}" for key, value in config.items()
                             if value is not None) + "\n"
            (directory / "target.toml").write_text(text)
            return "target created"

        active._admin = mock.Mock(side_effect=create)
        return active

    def test_native_targets_explicitly_use_and_record_upstream_chunk_engine(self):
        for servers in (1, 2, 4):
            with self.subTest(servers=servers), tempfile.TemporaryDirectory() as temporary:
                active = self.target_cluster(Path(temporary), servers)
                active._create_targets()
                self.assertEqual(active._admin.call_count, 4)
                placements = MODULE.storage_layout.target_placements(servers)
                for index, (call, target) in enumerate(zip(active._admin.call_args_list, placements), 1):
                    self.assertEqual(call.args, ([
                        "create-target", "--node-id", str(target.node_id),
                        "--disk-index", str(target.disk_index), "--target-id", str(target.target_id),
                        "--chain-id", str(target.chain_id), "--use-new-chunk-engine",
                    ], f"admin-target-{index}"))
                proof = active.ledger.update.call_args.kwargs["storage_engine"]
                self.assertEqual(proof["name"], "upstream-chunk-engine")
                self.assertIs(proof["verified"], True)
                self.assertEqual(len(proof["targets"]), 4)
                for row in proof["targets"]:
                    saved = active.roots.bundle / row["archive"]
                    self.assertEqual(MODULE.runtime.sha256(saved), row["sha256"])
                    self.assertIs(tomllib.loads(saved.read_text())["only_chunk_engine"], True)

    def test_native_targets_reject_missing_false_or_non_boolean_engine_selection(self):
        for value in (None, False, 1, "true"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temporary:
                active = self.target_cluster(Path(temporary), overrides={"only_chunk_engine": value})
                with self.assertRaisesRegex(MODULE.ClusterError, "chunk engine"):
                    active._create_targets()
                self.assertEqual(active._admin.call_count, 1)
                self.assertTrue((active.roots.bundle / "storage-targets/1000001001.toml").is_file())

    def test_native_targets_reject_wrong_identity_or_path(self):
        for override in ({"target_id": 123}, {"chain_id": 123}, {"path": "/unowned/target"}):
            with self.subTest(override=override), tempfile.TemporaryDirectory() as temporary:
                active = self.target_cluster(Path(temporary), overrides=override)
                with self.assertRaisesRegex(MODULE.ClusterError, "target identity"):
                    active._create_targets()
                self.assertEqual(active._admin.call_count, 1)

    def test_native_target_creation_failure_does_not_retry_or_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            active = self.target_cluster(Path(temporary))
            active._admin.side_effect = MODULE.ClusterError("target creation failed")
            with self.assertRaisesRegex(MODULE.ClusterError, "target creation failed"):
                active._create_targets()
            self.assertEqual(active._admin.call_count, 1)

    def test_mountpoint_detection_is_exact(self):
        text = "36 25 0:31 / /dev/shm rw - tmpfs tmpfs rw\n40 36 0:45 / /dev/shm/run/mnt rw - fuse.hf3fs hf3fs rw\n"
        self.assertTrue(MODULE._mountpoint_in(text, Path("/dev/shm/run/mnt")))
        self.assertFalse(MODULE._mountpoint_in(text, Path("/dev/shm/run/other")))

    def test_chain_readiness_rejects_offline_targets(self):
        offline = "1001 SERVING target-1(SERVING-OFFLINE)\n"
        serving = "\n".join(f"{index} SERVING target-{index}(SERVING-UPTODATE)" for index in range(4))
        self.assertFalse(MODULE._chains_ready(offline, 1))
        self.assertTrue(MODULE._chains_ready(serving, 4))

    def test_nofile_soft_limit_is_raised_for_storage_engine(self):
        with mock.patch.object(MODULE.resource, "getrlimit", return_value=(1024, 1_048_576)), \
             mock.patch.object(MODULE.resource, "setrlimit") as setrlimit:
            self.assertEqual(MODULE._raise_nofile_limit(), 1_048_576)
            setrlimit.assert_called_once_with(MODULE.resource.RLIMIT_NOFILE, (1_048_576, 1_048_576))

    def test_session_is_stable_positive_generation(self):
        self.assertEqual(MODULE._session("run-1"), MODULE._session("run-1"))
        self.assertGreater(MODULE._session("run-1"), 0)

    def test_native_manifest_has_exact_loopback_participants(self):
        value = MODULE._native_manifest(3, 7, 1 << 30)
        self.assertEqual(value["laneCount"], 256)
        participants = {item["endpoint"]: item for item in value["participants"]}
        self.assertEqual(set(participants), {1, 2, 3, 4, 7, 16, 17, 18})
        self.assertEqual(participants[18]["localAddress"], "CXL://127.0.0.1:12518")

    def test_fabric_lock_is_byte_exact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "cxl-fabricd.toml"
            config.write_text("authority_receipt = 'receipt-1'\n")
            lock = root / "authority.lock"
            lock.write_text("receipt-1")
            receipt = config.read_text().split("'", 2)[1]
            self.assertEqual(lock.read_text(), receipt)

    def test_service_readiness_requires_loopback_listener(self):
        source = (DEPLOY / "cluster.py").read_text()
        self.assertIn(
            'lambda p=port, path=service_log: self._port(p) and self._contains(path, "Start server finished")',
            source,
        )

    def test_cxl_listener_accepts_native_loopback(self):
        source = (PROJECT / "src/common/net/Listener.cc").read_text()
        cxl_case = source.split("case Address::CXL:", 1)[1].split("case Address::IPoIB:", 1)[0]
        self.assertIn('nic == "lo"', cxl_case)

    def test_native_config_filters_only_first_listener_to_loopback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "mgmtd_main.toml"
            listener = "[server.base.groups.listener]\n"
            config.write_text(listener + "listen_port = 12501\n" + listener + "listen_port = 0\n")
            MODULE._replace_config_paths(
                root,
                old_directory="/old/config",
                log_directory=root / "logs",
                region=root / "region",
                fdb_cluster=root / "fdb.cluster",
                fdb_library=root / "libfdb_c.so",
                storage=root / "storage",
                manifest=root / "manifest.json",
                lock=root / "authority.lock",
                receipt="receipt",
            )
            rendered = config.read_text()
            self.assertEqual(rendered.count("filter_list = ['lo']"), 1)
            self.assertIn(listener + "filter_list = ['lo']\nlisten_port = 12501", rendered)

    def test_native_storage_uses_fallocate_capable_tmpfs(self):
        source = (DEPLOY / "cluster.py").read_text()
        self.assertIn('STORAGE_PARENT = Path("/tmp")', source)
        self.assertIn("_probe_storage_fallocate(STORAGE_PARENT)", source)

    def test_native_cxl_poll_profiles_are_explicit_and_rendered(self):
        expected = {
            "adaptive": ("0us", 0, "0us", True, "1us", "50us"),
            "native-default": ("20us", 8, "10us", False, "1us", "50us"),
            "sleep-1ms": ("0us", 0, "1ms", False, "1us", "50us"),
            "sleep-10ms": ("0us", 0, "10ms", False, "1us", "50us"),
            "busy": ("0us", 0, "0us", False, "1us", "50us"),
        }
        self.assertEqual(set(MODULE.CXL_POLL_PROFILES), set(expected))
        for name, values in expected.items():
            polling = MODULE.CXL_POLL_PROFILES[name]
            self.assertEqual(
                (polling.spin, polling.yields, polling.sleep,
                 polling.adaptive, polling.idle_sleep_min, polling.idle_sleep_max),
                values,
            )
            with self.subTest(profile=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                MODULE.full_stack.render_config(
                    root, 42, clients=1, node=0, cohort_config=True, polling=polling,
                )
                directory = root / MODULE.full_stack.CONFIG.removeprefix("/")
                rendered = 0
                for path in directory.glob("*.toml"):
                    config = tomllib.loads(path.read_text())
                    if "cxl" not in config:
                        continue
                    rendered += 1
                    self.assertEqual(config["cxl"]["poll_spin"], values[0])
                    self.assertEqual(config["cxl"]["poll_yields"], values[1])
                    self.assertEqual(config["cxl"]["poll_sleep"], values[2])
                    self.assertEqual(config["cxl"]["poll_adaptive"], values[3])
                    self.assertEqual(config["cxl"]["poll_idle_sleep_min"], values[4])
                    self.assertEqual(config["cxl"]["poll_idle_sleep_max"], values[5])
                self.assertGreater(rendered, 0)

    def test_native_cli_defaults_to_verified_poll_profile(self):
        args = MODULE.parser().parse_args([
            "--repo", str(PROJECT), "--topology", "1c1s", "--preflight",
        ])
        self.assertEqual(args.cxl_poll_profile, "adaptive")


if __name__ == "__main__":
    unittest.main()
