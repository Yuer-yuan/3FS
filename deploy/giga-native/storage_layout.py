"""Native RF1 placement and config adaptation; no transport or storage policy changes."""
from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys
import tomllib

import topology

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'cxl-riscv'))
import make_phase1_manifest as manifests

TARGET_IDS = (1000001001, 1000002001, 1000003001, 1000004001)
STORAGE_FILES = ('storage_main.toml', 'storage_main_app.toml', 'storage_main_launcher.toml')


@dataclass(frozen=True)
class TargetPlacement:
    target_id: int
    chain_id: int
    node_id: int
    disk_index: int


def target_placements(storage_count: int) -> tuple[TargetPlacement, ...]:
    if type(storage_count) is not int or storage_count not in (1, 2, 4):
        raise ValueError('RF1 placement requires 1, 2, or 4 storage nodes')
    return tuple(TargetPlacement(tid, tid, 10000 + i % storage_count, i // storage_count)
                 for i, tid in enumerate(TARGET_IDS))


def native_manifest(clients: int, storage_count: int, session: int, region_bytes: int) -> dict:
    selected = topology.topology(f'{clients}c{storage_count}s')
    value = manifests.load_topology(manifests.DEFAULT_TOPOLOGY)
    roles = value['roles']
    addresses = {'fabric-authority': 12499, 'mgmtd': 12501, 'meta': 12502, 'admin': 12507}
    for node in selected.storage_nodes:
        role = f'storage-{node.index}'
        roles[role] = dict(endpoint=node.endpoint, guest=0,
                           address=f'CXL://127.0.0.1:{node.port}', serves_data=True)
    for role, port in addresses.items():
        roles[role]['guest'] = 0
        roles[role]['address'] = f'CXL://127.0.0.1:{port}'
    roles['fdb']['guest'] = 0
    for index in range(clients):
        roles[f'client-{index}'] = dict(endpoint=16 + index, guest=0,
            address=f'CXL://127.0.0.1:{12516 + index}', serves_data=False)
    scenario = 'storage'
    if storage_count > 1:
        scenario = 'native-storage'
        # Explicit roles decouple node count from RF without changing the shared
        # RF2/RF3 generator or editing its generated routes/digest afterwards.
        value['scenarios'][scenario] = dict(
            base_roles=[*value['scenarios']['storage']['base_roles'],
                        *(f'storage-{n.index}' for n in selected.storage_nodes)],
            storage_roles=False, client_roles=True)
    return manifests.build_manifest(value, scenario=scenario, replication_factor=1,
        clients=clients, session_generation=session, total_region_bytes=region_bytes, lane_count=256)


def _validate_node(node: topology.StorageNode) -> None:
    if type(node.index) is not int or not 0 <= node.index < 4:
        raise ValueError('invalid storage index')
    expected = topology.topology('1c4s').storage_nodes[node.index]
    fields = ('index', 'node_id', 'endpoint', 'port', 'cpus')
    if any(getattr(node, f) != getattr(expected, f) for f in fields):
        raise ValueError('storage identity conflicts with the native role contract')


def _paths(paths: tuple[Path, ...]) -> list[str]:
    if len(paths) not in (1, 2, 4) or any(not p.is_absolute() for p in paths):
        raise ValueError('target paths must be 1/2/4 absolute paths')
    resolved = [str(p.resolve()) for p in paths]
    if len(set(resolved)) != len(resolved):
        raise ValueError('target paths alias one another')
    return resolved


def _read_configs(directory: Path) -> dict:
    return {name: tomllib.loads((directory / name).read_text()) for name in STORAGE_FILES}


def validate_storage_config(directory: Path, node: topology.StorageNode,
                            target_paths: tuple[Path, ...]) -> dict:
    _validate_node(node)
    configs = _read_configs(directory)
    cfg = configs['storage_main.toml']
    launcher = configs['storage_main_launcher.toml']
    app = configs['storage_main_app.toml']
    groups = cfg['server']['base']['groups']
    if (len(groups) != 2 or groups[0]['listener']['listen_port'] != node.port
            or groups[0]['listener']['filter_list'] != ['lo']
            or groups[1]['listener']['listen_port'] != 0):
        raise ValueError('storage Data/Core listeners differ from deployment contract')
    if app['node_id'] != node.node_id or app['allow_empty_node_id'] is not False:
        raise ValueError('storage app identity differs')
    if launcher['cxl']['endpoint'] != node.endpoint or launcher['cxl']['mode'] != 'Attach':
        raise ValueError('storage CXL attachment identity differs')
    if cfg['server']['targets']['target_paths'] != _paths(target_paths):
        raise ValueError('storage target directories differ')
    return configs


def _replace_line(text: str, key: str, value: object, *, old: str | None = None) -> str:
    value_pattern = old if old is not None else r'[^\n]+'
    pattern = rf'(?m)^{re.escape(key)}\s*=\s*{value_pattern}$'
    rendered, count = re.subn(pattern, lambda _: f'{key} = {json.dumps(value)}', text)
    if count != 1:
        raise ValueError(f'expected one baseline field for {key}, found {count}')
    return rendered


def render_storage_config(base: Path, destination: Path, node: topology.StorageNode,
                          target_paths: tuple[Path, ...], logs: Path) -> dict:
    _validate_node(node)
    paths = _paths(target_paths)
    configs = _read_configs(base)
    original = configs['storage_main.toml']
    groups = original['server']['base']['groups']
    if len(groups) != 2 or groups[0]['listener']['listen_port'] != 12503 or groups[1]['listener']['listen_port'] != 0:
        raise ValueError('baseline storage listener contract changed')
    if configs['storage_main_app.toml']['node_id'] != 10000 or configs['storage_main_launcher.toml']['cxl']['endpoint'] != 4:
        raise ValueError('baseline storage identity changed')
    expected = copy.deepcopy(configs)
    expected['storage_main_app.toml']['node_id'] = node.node_id
    expected['storage_main_launcher.toml']['cxl']['endpoint'] = node.endpoint
    expected['storage_main.toml']['server']['base']['groups'][0]['listener']['listen_port'] = node.port
    expected['storage_main.toml']['server']['targets']['target_paths'] = paths
    handlers = expected['storage_main.toml']['common']['log']['handlers']
    if len(handlers) != 1:
        raise ValueError('baseline storage log handlers changed')
    handlers[0]['file_path'] = str(logs / 'storage.log')
    texts = {name: (base / name).read_text() for name in STORAGE_FILES}
    texts['storage_main_app.toml'] = _replace_line(texts['storage_main_app.toml'], 'node_id', node.node_id, old='10000')
    texts['storage_main_launcher.toml'] = _replace_line(texts['storage_main_launcher.toml'], 'endpoint', node.endpoint, old='4')
    text = _replace_line(texts['storage_main.toml'], 'listen_port', node.port, old='12503')
    text = _replace_line(text, 'target_paths', paths)
    texts['storage_main.toml'] = _replace_line(text, 'file_path', str(logs / 'storage.log'))
    # Comparing the entire parsed object allows only the explicitly enumerated
    # deployment differences, including for worker pools and Core settings.
    if {name: tomllib.loads(text) for name, text in texts.items()} != expected:
        raise ValueError('storage rendering changed a non-deployment field')
    destination.mkdir(parents=True, exist_ok=False)
    logs.mkdir(parents=True, exist_ok=True)
    for name, text in texts.items():
        (destination / name).write_text(text)
    validate_storage_config(destination, node, target_paths)
    return {'semantic_fields_unchanged': True,
            'files': {name: hashlib.sha256(text.encode()).hexdigest() for name, text in texts.items()}}


def table_rows(output: str, header: str) -> list[list[str]]:
    """Parse one native CLI table, excluding its trailing transport record."""
    lines = output.splitlines()
    starts = [i for i, line in enumerate(lines) if line.split()[:1] == [header]]
    if len(starts) != 1:
        raise ValueError(f'expected one {header} table')
    rows = []
    for line in lines[starts[0] + 1:]:
        fields = line.split()
        if fields and fields[0].isdigit():
            rows.append(fields)
        elif fields and fields[0].startswith('HF3FS_'):
            break
    return rows


def validate_nodes(output: str, nodes: tuple[topology.StorageNode, ...],
                   expected_pids: dict[int, int]) -> list[dict]:
    rows = table_rows(output, 'Id')
    storage = [row for row in rows if len(row) >= 5 and row[1] == 'STORAGE']
    ids = [int(row[0]) for row in storage]
    if sorted(ids) != sorted(n.node_id for n in nodes):
        raise ValueError('STORAGE node identities are missing, extra, or duplicated')
    if not any(row[:3] == ['50', 'META', 'HEARTBEAT_CONNECTED'] for row in rows):
        raise ValueError('META heartbeat is not connected')
    result = []
    for row in storage:
        node_id, pid = int(row[0]), int(row[4])
        if row[2] != 'HEARTBEAT_CONNECTED' or expected_pids.get(node_id) != pid:
            raise ValueError(f'storage {node_id} heartbeat/PID differs')
        result.append(dict(node_id=node_id, pid=pid, status=row[2]))
    return result


def validate_placement(targets: str, chains: str, table_csv: str, storage_count: int) -> list[dict]:
    placements = target_placements(storage_count)
    expected = {(p.target_id, p.chain_id, p.node_id, p.disk_index) for p in placements}
    rows = table_rows(targets, 'TargetId')
    actual = []
    result = []
    for row in rows:
        if len(row) != 8 or row[2:5] != ['HEAD', 'SERVING', 'UPTODATE']:
            raise ValueError('target is not the sole healthy RF1 member')
        tid, cid, nid, disk, used = map(int, (row[0], row[1], row[5], row[6], row[7]))
        actual.append((tid, cid, nid, disk))
        result.append(dict(target_id=tid, chain_id=cid, node_id=nid, disk_index=disk, used_bytes=used))
    if len(actual) != 4 or set(actual) != expected:
        raise ValueError('target placement is missing, duplicated, or on the wrong node/disk')
    chain_rows = table_rows(chains, 'ChainId')
    actual_chains = []
    for row in chain_rows:
        members = re.findall(r'(\d+)\(([^()]+)\)', ' '.join(row[4:]))
        if len(row) < 6 or row[1] != '1' or row[3] != 'SERVING' or members != [(row[0], 'SERVING-UPTODATE')]:
            raise ValueError('chain is not a serving RF1 member of table 1')
        actual_chains.append(int(row[0]))
    if sorted(actual_chains) != sorted(TARGET_IDS):
        raise ValueError('chain identities are missing, extra, or duplicated')
    lines = table_csv.strip().splitlines()
    if not lines or lines[0] != 'ChainId' or [int(x) for x in lines[1:]] != list(TARGET_IDS):
        raise ValueError('chain table order or membership differs')
    return result


def validate_storage_transport(records: list[dict], nodes: tuple[topology.StorageNode, ...],
                               pids: dict[int, int], session: int, digest: str) -> list[dict]:
    result = []
    for node in nodes:
        values = [r for r in records if r.get('endpoint') == node.endpoint]
        if len(values) != 1:
            raise ValueError(f'storage {node.node_id} transport record missing or duplicated')
        record = values[0]
        if (record.get('pid') != pids[node.node_id] or record.get('session') != session
                or record.get('manifest_sha256') != digest or record.get('clean_shutdown') is not True
                or record.get('scope') != 'process-lifetime-receive'
                or record.get('schema') != 'hf3fs.transport-counters.v1'):
            raise ValueError(f'storage {node.node_id} transport identity differs')
        counters = record.get('counters', {})
        for key in ('cxl_rpc_requests', 'cxl_rpc_responses', 'cxl_bulk_read_bytes', 'cxl_bulk_write_bytes',
                    'rdma_open_attempts', 'tcp_data_plane_bytes', 'bootstrap_serving_bytes'):
            if type(counters.get(key)) is not int or counters[key] < 0:
                raise ValueError('invalid per-storage counter')
        if (counters['cxl_rpc_requests'] + counters['cxl_rpc_responses'] <= 0
                or counters['cxl_bulk_read_bytes'] + counters['cxl_bulk_write_bytes'] <= 0):
            raise ValueError(f'storage {node.node_id} has no actual CXL RPC/bulk participation')
        if any(counters[k] for k in ('rdma_open_attempts', 'tcp_data_plane_bytes', 'bootstrap_serving_bytes')):
            raise ValueError('storage used a forbidden serving fallback')
        result.append(dict(node_id=node.node_id, endpoint=node.endpoint, pid=record['pid'], counters=counters))
    return result


def validate_live_config(actual: dict, expected: dict) -> None:
    """Core materializes defaults and canonicalizes durations (120s -> 2min)."""
    def duration(value):
        match = re.fullmatch(r'(\d+)(ns|us|ms|s|min|h|day)', value) if isinstance(value, str) else None
        if match:
            return int(match[1]) * {'ns': 1, 'us': 1000, 'ms': 1000000, 's': 10**9,
                                   'min': 60*10**9, 'h': 3600*10**9, 'day': 86400*10**9}[match[2]]
        return value

    def check(found, wanted, path):
        if isinstance(wanted, dict):
            if not isinstance(found, dict) or not wanted.keys() <= found.keys():
                raise ValueError(f'live config missing field at {path}')
            for key, value in wanted.items():
                check(found[key], value, f'{path}.{key}')
        elif isinstance(wanted, list):
            if not isinstance(found, list) or len(found) != len(wanted):
                raise ValueError(f'live config list differs at {path}')
            for index, value in enumerate(wanted):
                check(found[index], value, f'{path}[{index}]')
        elif duration(found) != duration(wanted):
            raise ValueError(f'live config differs at {path}: {found!r} != {wanted!r}')

    check(actual, expected, 'storage')
    groups = actual['server']['base']['groups']
    if [(g['network_type'], g['service_plane'], g['services']) for g in groups] != [
            ('CXL', 'Data', ['StorageSerde']), ('TCP', 'Control', ['Core'])]:
        raise ValueError('live Data/Core service planes differ')


def validate_listeners(listeners: list[dict], node: topology.StorageNode, reserved_ports: set[int]) -> dict:
    data = [p for p in listeners if p['port'] == node.port]
    core = [p for p in listeners if p['port'] != node.port]
    # Native Core binds one ephemeral socket per eligible host NIC.
    if (len(data) != 1 or data[0]['address'] != '127.0.0.1' or not core
            or any(p['port'] in reserved_ports for p in core)):
        raise ValueError(f'storage {node.node_id} lacks distinct Data/Core listeners: {listeners}')
    return dict(data_listener=data[0], core_listeners=core)
