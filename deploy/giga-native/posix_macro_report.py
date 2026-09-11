#!/usr/bin/env python3
"""Validate filesystem-only macro evidence; never update paper figure inputs."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import tomllib

CASES = ['unet3d', 'checkpoint', 'kv-8b-storage', 'kv-8b-cpu-tier', 'kv-70b-storage']


def read(path):
    return json.loads(path.read_text())


def require(condition, message):
    if not condition:
        raise ValueError(message)


def cpus(value):
    result = set()
    for part in value.split(','):
        bounds = [int(x) for x in part.split('-')]
        result.update(range(bounds[0], bounds[-1] + 1))
    return result


def validate(bundle, system, benchmark_lock, expected_profile):
    result = read(bundle / 'result.json')
    lego = system == 'legofs'
    work = bundle if lego else bundle / 'macrobench'
    status = result if lego else read(work / 'result.json')
    require(status['status'] == ('completed' if lego else 'passed'), 'workload failed')
    require([c['name'] for c in status['commands']] == CASES, 'incomplete case set')
    require(all(c['returncode'] == 0 and not c.get('timed_out') for c in status['commands']), 'case failed')
    proof = dict(system=system, run=bundle.name, source_result_sha256=hashlib.sha256((bundle / 'result.json').read_bytes()).hexdigest())
    if lego:
        selected = read(bundle / 'selected-profile.json')
        require(selected['binary_hashes_verified'] and selected['profile'] == expected_profile, 'LegoFS binary mismatch')
        require(result['sha256'] == {k: v['sha256'] for k, v in expected_profile['binaries'].items()}, 'LegoFS identity')
        require(result['host_namespace_absent'] and result['region_bytes'] == 64 * 2**30, 'LegoFS cleanup/region')
        require((result['client_cpus'], result['server_cpus'], result['memory_node']) == ('0-3', '6,7', '1'), 'LegoFS placement')
        authorities = [a for p in bundle.glob('namespace-serving-*.json') for a in read(p)['authorities']]
        require(bool(authorities), 'missing LegoFS transport evidence')
        require(all(a['sqe_submitted'] == a['cqe_consumed'] for a in authorities), 'undrained LegoFS SQ')
        require(all(a[k] == 0 for a in authorities for k in ('blob_tcp_bytes_after_cxl_ready',
            'filesystem_tcp_requests_after_cxl_ready', 'legacy_tarpc_calls_after_cxl_ready',
            'transport_fallbacks_after_cxl_ready')), 'LegoFS fallback')
        proof['file_reads'] = {k: sum(a['namespace_file_reads'][k] for a in authorities)
                               for k in ('reads', 'grants', 'releases', 'bytes', 'byte_plan_resolutions')}
    else:
        require(result['status'] == 'passed' and result['teardown']['all_owned_processes_stopped'], '3FS cluster failed')
        require(result['storage_engine']['verified'], 'engine unverified')
        require(len(result['storage_engine']['targets']) == 4 and result['replication_factor'] == 1, 'storage layout')
        for target in result['storage_engine']['targets']:
            raw = (bundle / target['archive']).read_bytes()
            require(hashlib.sha256(raw).hexdigest() == target['sha256'], 'target hash')
            require(tomllib.loads(raw.decode())['only_chunk_engine'], 'wrong storage engine')
        transport = result['transport_totals']
        require(all(transport[k] == 0 for k in ('rdma_open_attempts', 'tcp_data_plane_bytes', 'bootstrap_serving_bytes')), '3FS fallback')
        require(all(transport[k] > 0 for k in ('cxl_rpc_requests', 'cxl_rpc_responses', 'cxl_bulk_read_bytes', 'cxl_bulk_write_bytes')), 'no CXL data')
        policy = result['macrobench']
        require(policy['client_cpus'] == policy['fuse_cpus'] == [0, 1, 2, 3], 'client CPU budget')
        require(policy['all_server_cpus'] == [18, 19, 20, 21, 22, 23] and policy['memory_node'] == 1, 'service placement')
        require(policy['fuse_read_cache'] is False and policy['fuse_writeback_cache'] is False, 'cache policy')
        for stage in ('before-io', 'after-io'):
            snapshot = read(bundle / ('snapshot-' + stage + '.json'))
            require(all(e['value'] == 'false' and e['verified'] for e in snapshot['effective_fuse_read_cache']), 'live read cache')
            for process in snapshot['processes']:
                allowed = {0, 1, 2, 3} if process['role'].startswith('fuse-') else set(policy['server_role_cpus'][process['role']])
                require(all(cpus(t['cpus']) <= allowed for t in process['task_affinities']), 'service CPU escape')
        for relative, digest in read(work / 'source-hashes.json').items():
            require(hashlib.sha256((work / 'executed-sources' / relative).read_bytes()).hexdigest() == digest, 'source snapshot hash')
            if relative in benchmark_lock:
                require(digest == benchmark_lock[relative], 'bench source changed')
        for name in ('unet3d-explore.yaml', 'checkpoint-smoke.yaml', 'kv-explore.yaml'):
            normalized = (work / 'configs' / name).read_text().replace(status['filesystem_root'], '/legofs-namespace')
            require(hashlib.sha256(normalized.encode()).hexdigest() == benchmark_lock['configs/' + name], 'bench config changed')
        for config in (bundle / 'config').glob('node*/hf3fs_fuse_main.toml'):
            require(tomllib.loads(config.read_text())['io_bufs']['write_buf_size'] == 0, 'FUSE write buffer')
        frontend = read(bundle / 'fs-frontend.json')
        require(frontend['mode'] == system and frontend['region_bytes'] == 16 * 2**30, 'FS frontend/region')
        require(frontend['benchmark_source_hashes'] == benchmark_lock and frontend['application_unchanged'], 'bench lock mismatch')
        require(len(frontend['commands']) == 5, 'missing process evidence')
        proof.update(transport=transport, path_counters=[])
        for name, process in zip(CASES, frontend['commands']):
            require(process['returncode'] == 0, 'FS application failed')
            row = dict(case=name, shadow_delta=process['shadow_delta'])
            if system == 'iov':
                records = [json.loads(line[len('HF3FS_POSIX_COUNTERS '):]) for line in
                    (work / (name + '.log')).read_text().splitlines() if line.startswith('HF3FS_POSIX_COUNTERS ')]
                require(len(records) == 1 and records[0]['pid'] == process['pid'], 'missing/duplicate FS counters')
                counter = records[0]
                require(counter['direct_read_bytes'] == counter['direct_write_bytes'] == 0, 'unexpected app-buffer change')
                require(counter['staged_read_bytes'] > 0 and counter['staged_write_bytes'] > 0, 'IOV was not used')
                require(counter['staging_copy_in_bytes'] == counter['staged_write_bytes'] and
                        counter['staging_copy_out_bytes'] == counter['staged_read_bytes'], 'unexpected copy amplification')
                require(counter['rejected_calls'] == 0, 'FS rejected application I/O')
                require(process['shadow_delta'] == [0, 0], 'shadow copy remains')
                placement = process['placement']
                require('libhf3fs_posix.so' in placement['maps'], 'adapter not mapped')
                observed = cpus(re.search(r'(?m)^Cpus_allowed_list:\s*(.*)$', placement['status']).group(1))
                process_name = re.search(r'(?m)^Name:\s*(.*)$', placement['status']).group(1)
                if process_name == 'taskset' and 'phase' not in placement:
                    # First cohort's observer captured the launcher before exec.
                    # Keep the sample and disclose launch-only placement evidence;
                    # do not relabel this as a live application affinity check.
                    require(process['argv'][:3] == ['/usr/bin/taskset', '-c', '0-3'], 'wrong launch binding')
                    row['placement_evidence'] = 'pre-exec taskset snapshot; successful taskset launch binding only'
                else:
                    require(observed and observed <= {0, 1, 2, 3}, 'application CPU escape')
                    row['placement_evidence'] = 'application-exec snapshot'
                row['adapter'] = counter
            else:
                require('LD_PRELOAD' not in process['environment'], 'ordinary FUSE has preload')
            proof['path_counters'].append(row)
    values = {}
    training = read(work / 'unet3d/summary.json')['metric']
    values['unet3d/read'] = training['train_io_mean_MB_per_second'] / 1024
    checkpoint = read(work / 'checkpoint/summary.json')['metric']
    require(abs(checkpoint['checkpoint_size_GB'] - .2598543167114258) < 1e-9, 'checkpoint size changed')
    values['checkpoint/write'] = checkpoint['save_checkpoint_io_mean_GB_per_second']
    values['checkpoint/read'] = checkpoint['load_checkpoint_io_mean_GB_per_second']
    proof['kv'] = []
    for case in CASES[2:]:
        ev = read(work / (case + '.io-evidence.json'))
        summary = read(work / (case + '.json'))['summary']
        stats = summary['cache_stats']
        require(not ev['errors'] and ev['workers_drained'] and ev['read_calls'] > 0 and ev['write_calls'] > 0, 'KV failed/draining')
        require(ev['numpy_roundtrip_sha256'] == '70bae6b84188070199f1132764d2162dfcdec061a9225b0bb8f742371b62f367', 'NPY corruption')
        for op in ('read', 'written'):
            require(abs(stats['tier_storage_kv_bytes_' + op + '_gb'] * 2**30 - ev['bytes_' + op]) < 1, 'KV byte mismatch')
        if system == 'iov':
            counter = next(row['adapter'] for row in proof['path_counters'] if row['case'] == case)
            require(counter['staged_read_bytes'] >= ev['bytes_read'] and
                    counter['staged_write_bytes'] >= ev['bytes_written'], 'KV payload bypassed adapter')
        for op in ('read', 'write'):
            values[case + '/' + op] = stats['tier_storage_' + op + '_bandwidth_gbps']
        proof['kv'].append(dict(case=case, bytes_read=ev['bytes_read'], bytes_written=ev['bytes_written'],
            completed_requests=summary['total_requests'], elapsed=summary['elapsed_time'],
            drain_seconds=ev['drain_seconds']))
    require(all(math.isfinite(value) and value > 0 for value in values.values()), 'invalid metric')
    return dict(values=values, proof=proof)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--cohort', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    base = args.repo / 'fast-paper/artifact/mlperf-storage'
    controller = read(args.cohort / 'controller.json')
    lock = controller['benchmark_source_hashes']
    for relative, digest in lock.items():
        require(hashlib.sha256((base / relative).read_bytes()).hexdigest() == digest, 'current bench changed')
    profile = read(base / 'profiles/namespace-fast.json')
    accepted, failures = [], []
    for row in controller['attempts']:
        if row['status'] != 'completed' or row.get('returncode') != 0 or row.get('collection_returncode') != 0:
            failures.append(row)
            continue
        evidence = validate(args.cohort / row['run_id'], row['system'], lock, profile)
        accepted.append(dict(system=row['system'], repeat=row['repeat'], run_id=row['run_id'], **evidence))
    groups = {}
    for run in accepted:
        for key, value in run['values'].items():
            groups.setdefault(run['system'], {}).setdefault(key, []).append(value)
    aggregate = {s: {key: dict(n=len(v), mean=statistics.mean(v), minimum=min(v), maximum=max(v), values=v)
                     for key, v in metrics.items()} for s, metrics in groups.items()}
    result = dict(status=controller['status'], benchmark_unchanged=True, accepted=accepted,
                  failed_or_incomplete=failures, aggregate=aggregate)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'summary.json').write_text(json.dumps(result, indent=2) + '\n')
    lines = ['# 原样 macrobench：仅改变文件系统', '', '## Material Passport', '',
        '- Origin Skill: academic-research-suite / experiment-agent', '- Origin Mode: run + validate',
        '- Origin Date: 2026-09-12', '- Verification Status: ANALYZED', '- Version Label: fs_only_macro_v1', '',
        f"Controller: {controller['status']}; accepted full runs: {len(accepted)}; failed/incomplete: {len(failures)}.", '',
        'GiB/s；完整独立轮次的描述性均值。原始重复、完成字节和失败在 summary.json。', '',
        '| 工作负载/方向 | LegoFS | 3FS FUSE | 3FS POSIX IOV | IOV/FUSE | LegoFS/IOV |',
        '|---|---:|---:|---:|---:|---:|']
    keys = list(next(iter(accepted))['values']) if accepted else []
    for key in keys:
        values = {s: aggregate.get(s, {}).get(key, {}).get('mean') for s in ('legofs', 'fuse', 'iov')}
        def fmt(value):
            return f'{value:.4f}' if value is not None else '—'
        ratios = [values['iov'] / values['fuse'] if values['iov'] and values['fuse'] else None,
                  values['legofs'] / values['iov'] if values['legofs'] and values['iov'] else None]
        lines.append('| ' + key + ' | ' + ' | '.join(fmt(values[s]) for s in ('legofs', 'fuse', 'iov'))
                     + ' | ' + ' | '.join(f'{r:.2f}×' if r is not None else '—' for r in ratios) + ' |')
    lines += ['', '## 三轮原始值（非置信区间）', '',
              '| 工作负载/方向 | LegoFS r1/r2/r3 | 3FS FUSE r1/r2/r3 | 3FS IOV r1/r2/r3 |',
              '|---|---|---|---|']
    for key in keys:
        cells = [' / '.join(f'{v:.4f}' for v in aggregate.get(s, {}).get(key, {}).get('values', [])) or '—'
                 for s in ('legofs', 'fuse', 'iov')]
        lines.append('| ' + key + ' | ' + ' | '.join(cells) + ' |')
    lines += ['', '## 验证与边界', '',
        f"- 控制器组间恢复次数：{len(controller.get('resumptions', []))}；原因见controller.json。已完成测量不重跑、不替换。",
        '- Bench/原有适配器/任务/YAML 未修改；没有 NumPy allocator、readinto 或 native API bench。',
        '- 应用使用普通 buffer。IOV 是 FS 内部 staging，每个完成 payload 字节仍有一次应用边界拷贝；不是 app/IOV 同 buffer 零拷贝。',
        '- IOV 路径每项退出计数、实际库映射、StorageClient shadow=0 和 CXL bulk 非零已验证；不包含隐式内核/语言运行时拷贝的总量证明。',
        '- 1c1s，NUMA1，app/FUSE CPU0–3；LegoFS authority6–7，3FS服务18–23，服务核心预算不等。首轮3FS部分快照抓到exec前的taskset，仅作启动绑定证据；后续FS观察器等待Python exec，不改I/O或bench。',
        '- 本轮普通FUSE也有StorageClient shadow=0，不能把IOV/FUSE之差归因于去掉该项；实际差异包括FUSE内核数据通路、分段和FS staging。',
        '- 两种3FS都是新chunk engine/RF1，read/writeback cache关闭、write buffer0、16GiB transport arena；LegoFS为原64GiB sparse region。',
        '- 原缓存开启的历史主图与本轮不同，不替换、不混合；FS前端/缓存/region差异不等于纯copy消融。',
        '- KV是20s闭环到达窗口，保留60s排空和180s任务截止；完成请求量可能随速度变化，不是固定请求集的单调用延迟对比。',
        '- CPU-only reduced MLPerf Storage/DLIO workloads；非官方提交、真实GPU训练或持久介质性能。', '',
        '## Statistical fallacy scan', '',
        '11/11 checked: per-workload/direction stratification (Simpson); no extrapolation to GPU training (ecological); '
        'fixed cohort and no outcome filtering (Berkson/collider); no conditional-probability claim (base rate); '
        'fixed rotated repeats (regression to mean); failures retained (survivorship); all nine metrics shown '
        '(look-elsewhere); settings fixed before running (forking paths); frontend/copy/cache and unequal cores '
        'disclosed, no isolated-copy causality claim (correlation/causation and reverse causality).', '',
        'Confidence: CAUTION for generalization. No p-values, CIs or formal reproducibility certification.']
    (args.output / 'REPORT.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({k: result[k] for k in ('status', 'benchmark_unchanged')}, indent=2))


if __name__ == '__main__':
    main()
