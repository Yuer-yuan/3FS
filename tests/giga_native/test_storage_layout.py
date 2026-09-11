import dataclasses
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / 'deploy/giga-native'))
import cluster
import topology
import storage_layout as layout


class StorageLayoutTest(unittest.TestCase):
    def test_nine_topologies_preserve_four_rf1_targets(self):
        for clients in (1, 2, 3):
            for servers in (1, 2, 4):
                selected = topology.topology(f'{clients}c{servers}s')
                self.assertEqual(selected.ranks, clients)
                self.assertEqual(selected.storage_count, servers)
                self.assertEqual([n.endpoint for n in selected.storage_nodes], [4, 5, 6, 9][:servers])
                self.assertEqual(len(selected.ports), len(set(selected.ports.values())))
                rows = layout.target_placements(servers)
                self.assertEqual([r.target_id for r in rows], [1000001001, 1000002001, 1000003001, 1000004001])
                self.assertTrue(all(r.target_id == r.chain_id for r in rows))
                self.assertEqual([r.node_id for r in rows], [10000 + i % servers for i in range(4)])
                self.assertEqual([r.disk_index for r in rows], [i // servers for i in range(4)])
                cpus = list(selected.client_cpus)
                for values in selected.server_cpus.values():
                    cpus.extend(values)
                self.assertEqual(len(cpus), len(set(cpus)))
        for name in ('1c0s', '1c3s', '1c8s', '0c1s', '4c1s', '1c1s\n'):
            with self.assertRaises(ValueError, msg=name):
                topology.topology(name)

    def test_single_server_manifest_is_byte_equivalent(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/single-server-baseline.json').read_text())
        for clients in (1, 2, 3):
            self.assertEqual(cluster._native_manifest(clients, 7, 1 << 30), fixture['manifests'][str(clients)])

    def test_native_routes_include_all_storage_without_changing_geometry(self):
        for clients in (1, 2, 3):
            old = cluster._native_manifest(clients, 7, 1 << 30)
            for servers in (2, 4):
                value = cluster._native_manifest(clients, 7, 1 << 30, storage_count=servers)
                self.assertEqual(value['ranges'], old['ranges'])
                self.assertEqual(value['laneCount'], old['laneCount'])
                self.assertEqual(value['endpointCount'], old['endpointCount'])
                routes = {r['targetEndpoint']: r['address'] for r in value['routes']}
                for node in topology.topology(f'{clients}c{servers}s').storage_nodes:
                    self.assertEqual(routes[node.endpoint], f'CXL://127.0.0.1:{node.port}')
                self.assertNotEqual(value['manifestSha256'], old['manifestSha256'])

    def render_base(self, root, clients=2, node=0):
        stage = root / f'node{node}'
        cluster.full_stack.render_config(stage, 7, clients=clients, node=node,
            region_length=1 << 30, lane_count=256, cohort_config=True,
            polling=cluster.CXL_POLL_PROFILES['adaptive'])
        base = stage / cluster.full_stack.CONFIG.removeprefix('/')
        cluster._replace_config_paths(base, old_directory=cluster.full_stack.CONFIG,
            log_directory=root / f'logs/node{node}', region=root / 'region',
            fdb_cluster=root / 'fdb.cluster', fdb_library=root / 'libfdb_c.so',
            storage=root / 'storage', manifest=root / 'manifest.json',
            lock=root / 'authority.lock', receipt='baseline-7')
        return base

    def test_single_server_rendering_matches_frozen_fixture(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/single-server-baseline.json').read_text())
        for clients in (1, 2, 3):
            with tempfile.TemporaryDirectory() as tmp:
                for node in range(clients + 1):
                    base = self.render_base(Path(tmp), clients, node)
                    actual = {p.name: hashlib.sha256(p.read_text().replace(tmp, '/fixture').encode()).hexdigest()
                              for p in sorted(base.iterdir()) if p.is_file()}
                    self.assertEqual(actual, fixture['rendered'][f'{clients}/{node}'])

    def test_node_configs_change_only_deployment_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = self.render_base(root)
            original = tomllib.loads((base / 'storage_main.toml').read_text())
            for node in topology.topology('2c4s').storage_nodes:
                destination = root / f'storage-{node.index}'
                paths = (root / 'data' / str(node.node_id) / 'data1',)
                proof = layout.render_storage_config(base, destination, node, paths, root / f'logs/storage-{node.index}')
                config = tomllib.loads((destination / 'storage_main.toml').read_text())
                launcher = tomllib.loads((destination / 'storage_main_launcher.toml').read_text())
                app = tomllib.loads((destination / 'storage_main_app.toml').read_text())
                self.assertEqual(app['node_id'], node.node_id)
                self.assertEqual(launcher['cxl']['endpoint'], node.endpoint)
                self.assertEqual(config['server']['base']['groups'][0]['listener']['listen_port'], node.port)
                self.assertEqual(config['server']['base']['groups'][1], original['server']['base']['groups'][1])
                self.assertEqual(config['server']['targets']['target_paths'], list(map(str, paths)))
                self.assertTrue(proof['semantic_fields_unchanged'])
                layout.validate_storage_config(destination, node, paths)

    def test_config_collision_and_baseline_drift_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = self.render_base(root)
            node = topology.topology('2c4s').storage_nodes[3]
            for bad in (dataclasses.replace(node, endpoint=7), dataclasses.replace(node, port=12507),
                        dataclasses.replace(node, node_id=10000), dataclasses.replace(node, port=12503)):
                with self.assertRaises(ValueError):
                    layout.render_storage_config(base, root / f'bad-{bad.endpoint}-{bad.port}', bad,
                        (root / 'data1',), root / 'logs')
            with self.assertRaisesRegex(ValueError, 'alias'):
                layout.render_storage_config(base, root / 'bad-paths', node,
                    (root / 'data1', root / 'data1'), root / 'logs')
            path = base / 'storage_main.toml'
            path.write_text(path.read_text().replace('listen_port = 0', 'listen_port = 12503'))
            with self.assertRaises(ValueError):
                layout.render_storage_config(base, root / 'bad-core', node, (root / 'data1',), root / 'logs')


if __name__ == '__main__':
    unittest.main()
