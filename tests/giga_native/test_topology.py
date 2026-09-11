import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "deploy/cxl-riscv"))
MODULE_PATH = PROJECT / "deploy/giga-native/topology.py"
SPEC = importlib.util.spec_from_file_location("hf3fs_giga_topology", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class TopologyTest(unittest.TestCase):
    def test_fixed_topologies_share_one_server_stack(self):
        self.assertEqual(MODULE.topology("1c1s").client_cpus, (1,))
        self.assertEqual(MODULE.topology("2c1s").client_cpus, (1, 7))
        value = MODULE.topology("3c1s")
        self.assertEqual(value.client_cpus, (1, 7, 13))
        self.assertEqual(value.server_cpus, {
            "fdbserver": (18,),
            "cxl-fabricd": (19,),
            "mgmtd": (20,),
            "meta": (21,),
            "storage": (22, 23),
        })
        self.assertEqual(len(set(value.ports.values())), len(value.ports))
        with self.assertRaises(ValueError):
            MODULE.topology("4c1s")

    def test_phase1_manifest_supports_three_native_clients(self):
        import make_phase1_manifest

        source = make_phase1_manifest.load_topology(make_phase1_manifest.DEFAULT_TOPOLOGY)
        selected = make_phase1_manifest.select_roles(source, "storage", 1, 3)
        self.assertEqual(selected[-3:], ["client-0", "client-1", "client-2"])

    def test_effective_profile_changes_only_paths_and_find_rank_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = PROJECT / "deploy/giga-native/io500-all-bounded-1s.ini"
            output = Path(temporary) / "effective.ini"
            MODULE.render_io500_config(source, output, Path("/mnt/a"), Path("/mnt/b"), 3)
            original = MODULE.read_ini(source)
            effective = MODULE.read_ini(output)
            self.assertEqual(effective["global"]["datadir"], "/mnt/a")
            self.assertEqual(effective["global"]["resultdir"], "/mnt/b")
            for section in MODULE.FIND_PHASES:
                self.assertEqual(effective[section]["nproc"], "3")
            changed = {
                ("global", "datadir"),
                ("global", "resultdir"),
                *((section, "nproc") for section in MODULE.FIND_PHASES),
            }
            for section in original.sections():
                for key, value in original[section].items():
                    if (section, key) not in changed:
                        self.assertEqual(effective[section][key], value)

    def test_profile_enables_all_22_phases(self):
        profile = PROJECT / "deploy/giga-native/io500-all-bounded-1s.ini"
        record = MODULE.validate_profile(profile)
        self.assertEqual(record["stonewall_seconds"], 1)
        self.assertIs(record["expected_invalid"], True)
        self.assertEqual(tuple(record["phases"]), MODULE.EXPECTED_PHASES)
        parsed = MODULE.read_ini(profile)
        for section in ("ior-rnd4K", "ior-rnd1MB"):
            self.assertEqual(parsed[section]["blockSize"], "67108864")
            self.assertEqual(parsed[section]["randomPrefill"], "0")

    def test_parse_linux_list(self):
        self.assertEqual(MODULE.parse_linux_list("0-3,7,9-10\n"), {0, 1, 2, 3, 7, 9, 10})
        self.assertEqual(MODULE.parse_linux_list("\n"), set())
        with self.assertRaises(ValueError):
            MODULE.parse_linux_list("3-1")

    def test_validate_host_topology_from_fake_sysfs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root_path = Path(temporary)
            cpu_root = root_path / "devices/system/cpu"
            cpu_root.mkdir(parents=True)
            (cpu_root / "online").write_text("0-47\n")
            selected = {1: 0, 7: 1, 13: 2, 18: 3, 19: 3, 20: 3, 21: 3, 22: 3, 23: 3}
            for cpu, cache in selected.items():
                root = cpu_root / f"cpu{cpu}"
                (root / "topology").mkdir(parents=True)
                (root / "cache/index3").mkdir(parents=True)
                (root / "topology/core_id").write_text(f"{cpu}\n")
                (root / "topology/physical_package_id").write_text("0\n")
                (root / "cache/index3/id").write_text(f"{cache}\n")
            node = root_path / "devices/system/node/node1"
            node.mkdir(parents=True)
            (node / "cpulist").write_text("\n")
            record = MODULE.validate_host_topology(root_path)
            self.assertEqual([item["cpu"] for item in record["selected"]], sorted(selected))
            self.assertEqual(record["memory_node"], 1)

    def test_multiserver_rejects_offline_smt_and_wrong_llc(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cpu_root = root / 'devices/system/cpu'
            cpu_root.mkdir(parents=True)
            online = cpu_root / 'online'
            online.write_text('0-47')
            caches = {1: 0, 7: 1, 13: 2, 18: 3, 19: 3, 20: 3, 21: 3, 22: 3, 23: 3,
                      16: 2, 17: 2, 10: 1, 11: 1, 4: 0, 5: 0}
            for cpu, llc in caches.items():
                base = cpu_root / f'cpu{cpu}'
                for relative, value in [('topology/core_id', cpu), ('topology/physical_package_id', 0), ('cache/index3/id', llc)]:
                    path = base / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(str(value))
            node = root / 'devices/system/node/node1/cpulist'
            node.parent.mkdir(parents=True); node.write_text('')
            case = MODULE.topology('3c4s')
            self.assertEqual(len(MODULE.validate_host_topology(root, selected=case)['selected']), 15)
            online.write_text('0-3,5-47')
            with self.assertRaisesRegex(ValueError, 'offline'):
                MODULE.validate_host_topology(root, selected=case)
            online.write_text('0-47')
            (cpu_root / 'cpu5/topology/core_id').write_text('4')
            with self.assertRaisesRegex(ValueError, 'SMT'):
                MODULE.validate_host_topology(root, selected=case)
            (cpu_root / 'cpu5/topology/core_id').write_text('5')
            (cpu_root / 'cpu5/cache/index3/id').write_text('3')
            with self.assertRaisesRegex(ValueError, 'L3'):
                MODULE.validate_host_topology(root, selected=case)


if __name__ == "__main__":
    unittest.main()
