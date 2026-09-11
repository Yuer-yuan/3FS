#!/usr/bin/env python3
"""Bounded RF1 placement and independent-client content smoke; no performance score."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

import cluster as cluster_module
import runtime
import storage_layout

FILES = 64
FILE_BYTES = 4 << 20
CHUNK_BYTES = 524288
FILE_TIMEOUT = 120
STAGE_TIMEOUT = 600


def payload(index: int) -> bytes:
    return hashlib.shake_256(f'hf3fs-storage-smoke-v1-file-{index}'.encode()).digest(FILE_BYTES)


def filename(index: int) -> str:
    return f'file-{index:03d}.bin'


def validate_content(records: list[dict]) -> None:
    if sorted(r['index'] for r in records) != list(range(FILES)):
        raise ValueError('smoke content records are missing or duplicated')
    for row in records:
        if row['bytes'] != FILE_BYTES or row['sha256'] != hashlib.sha256(payload(row['index'])).hexdigest():
            raise ValueError(f"smoke content mismatch for file {row['index']}")


def worker(phase: str, client: int, mount: Path, progress: Path, result: Path) -> None:
    records = []
    for index in range(FILES):
        writer = index % 2
        if client != (writer if phase == 'write' else 1 - writer):
            continue
        runtime.atomic_json(progress, dict(index=index, file_started_ns=time.monotonic_ns(), active=True))
        path = mount / 'test/storage-smoke' / filename(index)
        if phase == 'write':
            data = payload(index)
            with path.open('xb') as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            count, digest = len(data), hashlib.sha256(data).hexdigest()
        else:
            digest_state = hashlib.sha256()
            count = 0
            with path.open('rb') as source:
                while data := source.read(1 << 20):
                    count += len(data)
                    if count > FILE_BYTES:
                        raise ValueError(f'oversize smoke file: {index}')
                    digest_state.update(data)
            digest = digest_state.hexdigest()
        row = dict(index=index, client=client, bytes=count, sha256=digest)
        if count != FILE_BYTES or digest != hashlib.sha256(payload(index)).hexdigest():
            raise ValueError(f'content mismatch for file {index} on client {client}')
        records.append(row)
        runtime.atomic_json(progress, dict(index=index, active=False, completed=len(records)))
    runtime.atomic_json(result, dict(phase=phase, client=client, pid=os.getpid(),
                                     allowed_cpus=sorted(os.sched_getaffinity(0)), records=records))


def run_stage(cluster, phase: str) -> list[dict]:
    started = time.monotonic()
    workers = []
    for client in range(2):
        progress = cluster.roots.bundle / f'smoke-{phase}-{client}-progress.json'
        result = cluster.roots.bundle / f'smoke-{phase}-{client}.json'
        process = cluster._start(f'smoke-{phase}-{client}', Path(sys.executable).resolve(), [
            str(Path(__file__).resolve()), '--worker', phase, '--client', str(client),
            '--mount', str(cluster.mounts[client]), '--progress', str(progress), '--output', str(result),
        ], (cluster.topology.client_cpus[client],))
        workers.append((process, progress, result, client))
    while True:
        if time.monotonic() - started > STAGE_TIMEOUT:
            raise ValueError(f'smoke {phase} exceeded {STAGE_TIMEOUT}s stage deadline')
        done = True
        for process, progress, result, client in workers:
            rc = process.process.poll()
            if rc is not None:
                if rc != 0 or not result.is_file():
                    raise ValueError(f'smoke {phase} client {client} exited {rc}; see worker log')
                continue
            done = False
            if progress.is_file():
                value = json.loads(progress.read_text())
                if value['active'] and (time.monotonic_ns() - value['file_started_ns']) / 1e9 > FILE_TIMEOUT:
                    raise ValueError(f"smoke {phase} client {client} file {value['index']} exceeded {FILE_TIMEOUT}s")
        if done:
            break
        time.sleep(0.1)
    records = []
    for process, _, result, client in workers:
        value = json.loads(result.read_text())
        if value['pid'] != process.pid or value['allowed_cpus'] != [cluster.topology.client_cpus[client]]:
            raise ValueError('smoke worker identity/CPU differs')
        expected_indexes = [i for i in range(FILES) if client == (i % 2 if phase == 'write' else 1-i % 2)]
        if [r['index'] for r in value['records']] != expected_indexes:
            raise ValueError('smoke file ownership or independent reader differs')
        records.extend(value['records'])
    validate_content(records)
    return records


def parse_file_layout(output: str) -> dict:
    for key, expected in [('Layout-ChainTable', 1), ('Layout-ChunkSize', CHUNK_BYTES), ('Layout-StripeSize', 1)]:
        values = re.findall(rf'(?m)^{key}\s+(\d+)\s*$', output)
        if values != [str(expected)]:
            raise ValueError(f'smoke file {key} differs')
    rows = re.findall(r'(?m)^ChainId\((\d+)\)\s+(\d+)\s+(\d+)\s+(\d+)\s*$', output)
    inode_rows = re.findall(r'(?m)^Inode\s+(0x[0-9a-fA-F]{16})\s*$', output)
    if len(rows) != 1 or len(inode_rows) != 1:
        raise ValueError('smoke file does not have exactly one chain/chunk row')
    cid, chunk_index, size, length = map(int, rows[0])
    if cid not in storage_layout.TARGET_IDS or chunk_index != FILE_BYTES // CHUNK_BYTES - 1 or size != CHUNK_BYTES or length != FILE_BYTES:
        raise ValueError('smoke file has unexpected chain, last chunk, or stored length')
    # Stat.cc prints the chunk *index*. Construct the actual 16-byte ID exactly
    # as meta::ChunkId in fbs/meta/Schema.h: tenant/reserved, inode BE64,
    # track BE16 (=0 for this normal file), chunk index BE32.
    inode = int(inode_rows[0], 16)
    packed = (b'\0\0' + inode.to_bytes(8, 'big') + b'\0\0' + chunk_index.to_bytes(4, 'big')).hex().upper()
    chunk = '-'.join(packed[i:i+8] for i in range(0, 32, 8))
    return dict(chain_id=cid, inode=inode_rows[0], last_chunk_index=chunk_index,
                last_chunk=chunk, last_chunk_bytes=size, bytes=length)


def validate_distribution(files: list[dict], placements: list[dict]) -> list[dict]:
    if sorted(f['index'] for f in files) != list(range(FILES)):
        raise ValueError('file placement evidence missing or duplicated')
    expected = {p['chain_id']: p for p in placements}
    if len(expected) != 4 or {f['chain_id'] for f in files} != set(expected):
        raise ValueError('smoke files did not cover all four chains')
    counts = {}
    for row in files:
        p = expected[row['chain_id']]
        if row['node_id'] != p['node_id'] or row['bytes'] != FILE_BYTES:
            raise ValueError('file chain owner/bytes differs from actual placement')
        record = counts.setdefault(p['node_id'], dict(node_id=p['node_id'], files=0, bytes=0))
        record['files'] += 1
        record['bytes'] += row['bytes']
    if set(counts) != {p['node_id'] for p in placements}:
        raise ValueError('not every storage actually owns data')
    return sorted(counts.values(), key=lambda r: r['node_id'])


def validate_backend_read(output: str) -> None:
    # IOResult prints a Result<uint32_t> inside length{...}. A batch can return
    # successfully while its individual read failed; rc=0 alone is insufficient.
    matches = re.findall(r'(?m)^result: length\{([^}]+)\}', output)
    if matches != [str(CHUNK_BYTES)]:
        raise ValueError(f'backend chunk read/checksum did not succeed: {matches}')


def parse_admin_batch(output: str, commands: list[str]) -> list[str]:
    parts = re.split(r'(?m)^> Execute (.+)\n', output)
    if parts[1::2] != commands or len(parts[2::2]) != len(commands):
        raise ValueError('native admin batch commands are missing, duplicated, or reordered')
    return parts[2::2]


def admin_batch(cluster, commands: list[str], role: str) -> list[str]:
    if not commands or any(';' in c or '\n' in c for c in commands):
        raise ValueError('unsafe native admin batch delimiter')
    # Dispatcher::run supports semicolon-separated commands in one native
    # session. It may return rc=0 after an intermediate failure, so every
    # command marker AND every individual result must still be validated.
    output = cluster._run_command(role, [str(cluster.artifacts['admin_cli']), '--cfg',
        str(cluster.configs[0] / 'admin_cli.toml'), '--config.verbose=true',
        '--config.break_multi_line_command_on_failure=true', '--', ';'.join(commands)])
    outputs = parse_admin_batch(output, commands)
    for index, value in enumerate(outputs):
        # Retain individual output without duplicating the batch's final
        # transport record, which belongs to one process lifetime.
        value = re.sub(r'(?m)^HF3FS_CXL_TRANSPORT_COUNTERS .*\n?', '', value)
        (cluster.roots.bundle / 'logs' / f'{role}-{index:03d}.txt').write_text(value)
    return outputs


def run(cluster) -> dict:
    if cluster.topology.ranks != 2:
        raise ValueError('storage correctness smoke requires two independent clients')
    cluster._admin(['mkdir', '--perm', '0755', 'test/storage-smoke'], 'admin-smoke-mkdir')
    writes = run_stage(cluster, 'write')
    reads = run_stage(cluster, 'read')
    files = []
    placement = cluster.ledger.value['chain_readiness']['placement']
    owners = {p['chain_id']: p['node_id'] for p in placement}
    commands = [f'stat --display-layout --display-chain-list --display-chunks test/storage-smoke/{filename(i)}'
                for i in range(FILES)]
    for index, output in enumerate(admin_batch(cluster, commands, 'admin-smoke-stat')):
        row = parse_file_layout(output)
        files.append(dict(row, index=index, node_id=owners[row['chain_id']]))
    # Save observed distribution before enforcing coverage; never resample files.
    runtime.atomic_json(cluster.roots.bundle / 'smoke-file-placement.json', files)
    distribution = validate_distribution(files, placement)
    backend = []
    for node in cluster.topology.storage_nodes:
        sample = next(f for f in files if f['node_id'] == node.node_id)
        output = cluster._admin(['query-chunk', '--chain-id', str(sample['chain_id']), '--chunk',
                                  sample['last_chunk'], '--read'], f'admin-smoke-backend-{node.node_id}')
        validate_backend_read(output)
        backend.append(dict(node_id=node.node_id, chain_id=sample['chain_id'], chunk=sample['last_chunk'],
                            bytes=CHUNK_BYTES, checksum_enabled=True, status='passed'))
    return dict(status='passed', kind='storage-correctness', content=dict(status='passed', files=FILES,
                bytes=FILES*FILE_BYTES, writes=len(writes), independent_reads=len(reads)),
                placement=dict(status='passed', distribution=distribution), backend_reads=backend,
                limits=dict(file_seconds=FILE_TIMEOUT, stage_seconds=STAGE_TIMEOUT))


def main() -> int:
    if '--worker' in sys.argv:
        parser = argparse.ArgumentParser()
        parser.add_argument('--worker', choices=('write', 'read'), required=True)
        parser.add_argument('--client', type=int, choices=(0, 1), required=True)
        for name in ('mount', 'progress', 'output'):
            parser.add_argument('--' + name, type=Path, required=True)
        args = parser.parse_args()
        worker(args.worker, args.client, args.mount, args.progress, args.output)
        return 0
    args = cluster_module.parser().parse_args()
    if args.topology not in ('2c1s', '2c2s', '2c4s'):
        raise ValueError('use 2c1s, 2c2s, or 2c4s for the storage smoke')
    if args.preflight:
        print(json.dumps(cluster_module.preflight(args.repo, cluster_module.topology_module.topology(args.topology)), indent=2))
        return 0
    if args.run_id is None or args.build_manifest is None:
        raise ValueError('--run-id and --build-manifest are required')
    result = cluster_module.execute_cluster(cluster_module.ClusterConfig(args.repo, args.build_manifest,
        args.topology, args.run_id, args.profile, cxl_poll_profile=args.cxl_poll_profile,
        rpc_trace_methods=args.rpc_trace_methods), run)
    print(f"hf3fs-storage-smoke={result['status'].upper()} topology={args.topology} run_id={args.run_id}")
    return 0 if result['status'] == 'passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
