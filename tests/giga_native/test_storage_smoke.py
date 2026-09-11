import copy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'deploy/giga-native'))
import storage_smoke as smoke
import storage_layout as layout
import topology


class StorageEvidenceTest(unittest.TestCase):
    def test_admin_batch_cannot_hide_early_stop_or_reordered_results(self):
        commands = ['stat a', 'stat b']
        output = '> Execute stat a\nresult a\n> Execute stat b\nresult b\n'
        self.assertEqual(smoke.parse_admin_batch(output, commands), ['result a\n', 'result b\n'])
        for bad in (output.split('> Execute stat b')[0], output.replace('stat b', 'stat a'),
                    output + '> Execute stat b\nresult b\n'):
            with self.assertRaises(ValueError):
                smoke.parse_admin_batch(bad, commands)

    def test_multihomed_core_keeps_distinct_ephemeral_listeners(self):
        node = topology.topology('2c1s').storage_nodes[0]
        listeners = [dict(address='127.0.0.1', port=12503),
                     dict(address='128.114.59.41', port=36063),
                     dict(address='128.114.59.190', port=43395)]
        ports = set(topology.BASE_PORTS.values())
        self.assertEqual(len(layout.validate_listeners(listeners, node, ports)['core_listeners']), 2)
        for bad in (listeners[:1], listeners[1:], listeners + [listeners[0]],
                    [listeners[0], dict(address='128.114.59.41', port=12507)]):
            with self.assertRaises(ValueError):
                layout.validate_listeners(bad, node, ports)

    def tables(self, servers=4):
        targets = 'TargetId ChainId Role PublicState LocalState NodeId DiskIndex UsedSize\n'
        chains = 'ChainId ReferencedBy ChainVersion Status PreferredOrder Target\n'
        for p in layout.target_placements(servers):
            targets += f'{p.target_id} {p.chain_id} HEAD SERVING UPTODATE {p.node_id} {p.disk_index} 4096\n'
            chains += f'{p.chain_id} 1 1 SERVING [] {p.target_id}(SERVING-UPTODATE)\n'
        table = 'ChainId\n' + '\n'.join(map(str, layout.TARGET_IDS)) + '\n'
        return targets, chains, table

    def test_exact_rf1_placement_rejects_wrong_owner_disk_duplicates_and_replication(self):
        for servers in (1, 2, 4):
            values = self.tables(servers)
            self.assertEqual(len(layout.validate_placement(*values, servers)), 4)
        targets, chains, table = self.tables()
        invalid = [
            (targets.replace('10003 0', '10000 0'), chains, table),
            (targets.replace('10003 0', '10003 1'), chains, table),
            (targets + targets.splitlines()[1] + '\n', chains, table),
            (targets, chains.replace('1000001001(SERVING-UPTODATE)', '1000001001(SERVING-UPTODATE) 999(SERVING-UPTODATE)'), table),
            (targets, chains.replace('UPTODATE', 'OFFLINE'), table),
            (targets, chains + chains.splitlines()[1] + '\n', table),
            (targets, chains, table.replace('1000004001', '1000003001')),
        ]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                layout.validate_placement(*values, 4)

    def test_all_storage_heartbeats_match_owned_pids(self):
        nodes = topology.topology('2c4s').storage_nodes
        pids = {n.node_id: 800 + n.index for n in nodes}
        text = 'Id Type Status Hostname Pid\n50 META HEARTBEAT_CONNECTED host 700\n'
        text += ''.join(f'{n.node_id} STORAGE HEARTBEAT_CONNECTED host {pids[n.node_id]}\n' for n in nodes)
        self.assertEqual(len(layout.validate_nodes(text, nodes, pids)), 4)
        for bad in (text.rsplit('\n', 2)[0], text.replace('host 803', 'host 999'),
                    text.replace('10003 STORAGE HEARTBEAT_CONNECTED', '10003 STORAGE HEARTBEAT_DISCONNECTED'),
                    text + text.splitlines()[-1] + '\n'):
            with self.assertRaises(ValueError):
                layout.validate_nodes(bad, nodes, pids)

    def test_per_storage_transport_identity_and_real_activity(self):
        nodes = topology.topology('2c4s').storage_nodes
        pids = {n.node_id: 800 + n.index for n in nodes}
        records = [dict(schema='hf3fs.transport-counters.v1', scope='process-lifetime-receive',
            pid=pids[n.node_id], endpoint=n.endpoint, session=7, manifest_sha256='abc', clean_shutdown=True,
            counters=dict(cxl_rpc_requests=1, cxl_rpc_responses=0, cxl_bulk_read_bytes=4096,
                cxl_bulk_write_bytes=0, rdma_open_attempts=0, tcp_data_plane_bytes=0, bootstrap_serving_bytes=0))
            for n in nodes]
        self.assertEqual(len(layout.validate_storage_transport(records, nodes, pids, 7, 'abc')), 4)
        invalid = [records[:-1], records + [records[0]]]
        for key, value in [('pid', 999), ('session', 8), ('manifest_sha256', 'bad'), ('endpoint', 8), ('clean_shutdown', False)]:
            bad = copy.deepcopy(records); bad[-1][key] = value; invalid.append(bad)
        for key, value in [('cxl_bulk_read_bytes', 0), ('cxl_rpc_requests', 0), ('tcp_data_plane_bytes', 1)]:
            bad = copy.deepcopy(records); bad[-1]['counters'][key] = value; invalid.append(bad)
        for bad in invalid:
            with self.assertRaises(ValueError):
                layout.validate_storage_transport(bad, nodes, pids, 7, 'abc')

    def test_content_requires_complete_readback(self):
        with mock.patch.object(smoke, 'FILES', 4), mock.patch.object(smoke, 'FILE_BYTES', 4096):
            records = [dict(index=i, bytes=4096, sha256=hashlib.sha256(smoke.payload(i)).hexdigest()) for i in range(4)]
            smoke.validate_content(records)
            self.assertEqual(len({r['sha256'] for r in records}), 4)
            for bad in (records[:-1], records + [records[0]], [dict(r, sha256='bad') for r in records]):
                with self.assertRaises(ValueError):
                    smoke.validate_content(bad)

    def test_file_layout_and_distribution_reject_unhit_server(self):
        output = ('Inode 0x000000000000e803\nLayout-ChainTable 1\nLayout-ChunkSize 524288\nLayout-StripeSize 1\n'
                  'Layout-Chains last chunk last chunk len last offset\n'
                  'ChainId(1000001001) 7 524288 4194304\ntotal 4194304\n')
        self.assertEqual(smoke.parse_file_layout(output)['chain_id'], layout.TARGET_IDS[0])
        self.assertEqual(smoke.parse_file_layout(output)['last_chunk'], '00000000-00000000-E8030000-00000007')
        for bad in (output.replace('StripeSize 1', 'StripeSize 4'), output.replace('524288 4194304', '0 0')):
            with self.assertRaises(ValueError):
                smoke.parse_file_layout(bad)
        placements = [asdict(p) for p in layout.target_placements(4)]
        files = [dict(index=i, chain_id=layout.TARGET_IDS[i % 4], node_id=10000+i % 4, bytes=smoke.FILE_BYTES) for i in range(64)]
        self.assertEqual(len(smoke.validate_distribution(files, placements)), 4)
        for bad in ([dict(f, chain_id=layout.TARGET_IDS[0]) if f['node_id'] == 10003 else f for f in files],
                    [dict(f, node_id=10000) for f in files]):
            with self.assertRaises(ValueError):
                smoke.validate_distribution(bad, placements)

    def test_backend_read_checks_io_result_even_when_batch_rc_zero(self):
        smoke.validate_backend_read('result: length{524288} version{1/1} checksum{x} {1}\n')
        for bad in ('', 'result: length{0}', 'result: length{StatusCode(5): checksum mismatch}'):
            with self.assertRaises(ValueError):
                smoke.validate_backend_read(bad)

    def test_workers_write_and_independent_read_partition(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(smoke, 'FILES', 4), mock.patch.object(smoke, 'FILE_BYTES', 4096):
            root = Path(temporary)
            (root / 'test/storage-smoke').mkdir(parents=True)
            for phase in ('write', 'read'):
                records = []
                for client in (0, 1):
                    result = root / f'{phase}-{client}.json'
                    smoke.worker(phase, client, root, root / 'progress.json', result)
                    rows = json.loads(result.read_text())['records']
                    self.assertEqual([r['index'] for r in rows], list(range(client if phase == 'write' else 1-client, 4, 2)))
                    records.extend(rows)
                smoke.validate_content(records)


if __name__ == '__main__':
    unittest.main()
