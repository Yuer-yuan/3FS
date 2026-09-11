#!/usr/bin/env python3
"""Offline full22 source-bound summary; never launches or repairs workloads."""
import argparse
import configparser
import csv
import io
import json
import math
from pathlib import Path
import re
import statistics
import tomllib

from posix_frontend import sha, validate_counters
from posix_io500_compare import POINTS, schedule
from topology import EXPECTED_PHASES

PATTERN = re.compile(r'\[(?:RESULT| +)\]\s+(\S+)\s+(\S+)\s+(\S+)\s+:\s+time\s+(\S+)\s+seconds')


def require(ok, message):
    if not ok:
        raise ValueError(message)


def phases(text):
    values = [dict(phase=n, value=float(v), unit=u, seconds=float(s))
              for n, v, u, s in PATTERN.findall(text) if n in EXPECTED_PHASES]
    require(tuple(r['phase'] for r in values) == EXPECTED_PHASES, 'incomplete/duplicate/out-of-order phases')
    require(all(math.isfinite(r['value']) and r['value'] >= 0 and
                math.isfinite(r['seconds']) and r['seconds'] > 0 for r in values), 'invalid phase values')
    return values


def normalized(path):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read(path)
    for section, keys in {'global': ('datadir', 'resultdir'), 'find': ('nproc',),
                          'find-easy': ('nproc',), 'find-hard': ('nproc',)}.items():
        for key in keys:
            parser.remove_option(section, key)
    return {section: dict(parser[section]) for section in parser.sections()}


def verify_record(root, row, inputs):
    case = row['case']
    bundle = (root / 'cohort/legofs' / case['name'] if case['system'] == 'legofs' else
              root / 'threefs' / Path(row['bundle']).name)
    require(sha(bundle / 'result.json') == row['result_sha256'], 'raw result hash mismatch')
    result = json.loads((bundle / 'result.json').read_text())
    text = (bundle / 'io500.log').read_text(errors='replace')
    parsed = phases(text)
    fields = ('phase', 'value', 'unit', 'seconds')
    require(parsed == [{k: p[k] for k in fields} for p in row['verification']['phases']], 'raw phase mismatch')
    if case['system'] == 'legofs':
        require(result['io500_rc'] == 0 and result['all22_completed'] and
                result['server_children_reaped'] and result['run_root_removed'], 'LegoFS completion gate')
        require(result['client_cpus'] == case['client_cpus'] and result['server_cpus'] == case['server_cpus'] and
                result['server_workers'] == 1, 'LegoFS placement mismatch')
        require(all(result['sha256'][k] == inputs['lego']['binaries'][k]['sha256']
                    for k in ('formatter', 'server', 'preload')), 'LegoFS ELF mismatch')
        ranks = set()
        for path in bundle.glob('namespace-serving-*.json'):
            snap = json.loads(path.read_text())
            rank = snap['mpi_rank']
            rank = 0 if rank is None and case['clients'] == 1 else int(rank)
            require(rank not in ranks, 'duplicate LegoFS rank')
            ranks.add(rank)
            require(sorted(e['authority_id'] for e in snap['authorities']) == list(range(case['servers'])),
                    'LegoFS authority coverage')
            for e in snap['authorities']:
                require(e['sqe_submitted'] == e['cqe_consumed'] > 0, 'LegoFS CXL completion mismatch')
                require(e['bootstrap_tcp_connections'] == e['bootstrap_tcp_exchanges'] == 1,
                        'LegoFS bootstrap accounting mismatch')
                require(all(e[k] == 0 for k in ('filesystem_tcp_requests_after_cxl_ready',
                    'legacy_tarpc_calls_after_cxl_ready', 'blob_tcp_bytes_after_cxl_ready',
                    'transport_fallbacks_after_cxl_ready', 'unsupported_serving_calls_after_cxl_ready')),
                    'LegoFS transport fallback')
                reads, pool = e.get('namespace_file_reads'), e.get('namespace_read_mapping_pool')
                if reads is not None:
                    require(reads['grants'] == reads['releases'], 'outstanding LegoFS read capability')
                if pool is not None:
                    require(not pool['failed'] and pool['live_views'] == 0 and
                            pool['acquired_views'] == pool['released_views'], 'LegoFS read view leak')
        require(ranks == set(range(case['clients'])), 'LegoFS rank coverage')
        effective = bundle / 'io500.ini'
        verifier = bundle / 'matrix-verifier.log'
    else:
        require(result['status'] == 'passed' and result['workload']['returncode'] == 0, '3FS completion gate')
        require(result['fs_frontend']['mode'] == case['system'], '3FS FS mode mismatch')
        require(result['storage_count'] == case['servers'] and result['ranks'] == case['clients'],
                '3FS topology mismatch')
        require(result['storage_engine']['verified'] and len(result['storage_engine']['targets']) == 4,
                '3FS engine proof missing')
        for target in result['storage_engine']['targets']:
            require(target['only_chunk_engine'] and sha(bundle / target['archive']) == target['sha256'],
                    '3FS engine target mismatch')
        totals = result['transport_totals']
        require(totals['cxl_rpc_requests'] == totals['cxl_rpc_responses'] > 0 and
                totals['cxl_bulk_read_bytes'] > 0 and totals['cxl_bulk_write_bytes'] > 0, 'CXL bulk/RPC missing')
        require(all(totals[k] == 0 for k in ('rdma_open_attempts', 'tcp_data_plane_bytes', 'bootstrap_serving_bytes')),
                '3FS serving fallback')
        require(len(result['storage_transport']) == case['servers'], '3FS storage activity missing')
        require(all(x['verified'] and x['value'] == 'false' for x in result['effective_fuse_read_cache']),
                '3FS live cache policy mismatch')
        require(result['region']['bytes'] == 16 * 2**30 and result['region']['memory_node'] == 1,
                '3FS transport size/NUMA mismatch')
        ranks = result['workload']['ranks']
        require(len(ranks) == case['clients'], '3FS rank count mismatch')
        for rank in ranks:
            require(json.loads((bundle / f"ranks/rank-{rank['rank']}.json").read_text()) == rank, 'rank receipt mismatch')
            fs = rank['fs_frontend']
            require(fs['mode'] == case['system'] and fs['shadow_delta'] == [0, 0], 'rank FS/shadow mismatch')
            require(fs['placement']['allowed_cpus'] == [int(case['client_cpus'][rank['rank']])], 'live CPU mismatch')
            require((fs['library'] in fs['placement']['maps']) == (case['system'] == 'iov'), 'preload mismatch')
        counters = [json.loads(x) for x in re.findall(r'(?m)^HF3FS_POSIX_COUNTERS (\{[^\r\n]+\})', text)]
        if case['system'] == 'iov':
            selected = validate_counters(counters, [r['fs_frontend']['pid'] for r in ranks])
            require(selected == result['workload']['posix_counters'], 'counter archive differs from raw log')
        else:
            require(not counters, 'FUSE run unexpectedly used adapter')
        for name, expected in json.loads((bundle / 'rendered-config.json').read_text()).items():
            require(sha(bundle / 'config' / name) == expected, 'rendered config hash mismatch')
        for rank in range(case['clients'] + 1):
            config = tomllib.loads((bundle / f'config/node{rank}/hf3fs_fuse_main.toml').read_text())
            require(config['enable_read_cache'] is False and config['enable_writeback_cache'] is False and
                    config['io_bufs']['write_buf_size'] == 0, '3FS buffer/cache policy mismatch')
        require(result['teardown']['all_owned_processes_stopped'] and
                not any(result['teardown'][k] for k in ('errors', 'occupied_ports', 'active_mounts')), '3FS teardown')
        effective = bundle / 'io500-effective.ini'
        verifier = bundle / 'io500-verify.log'
    require(normalized(effective) == normalized(root / 'cohort/io500-profile.ini'), 'benchmark geometry drift')
    require('[OK] But this is an invalid run!' in verifier.read_text(), 'missing expected verifier diagnosis')
    score = configparser.ConfigParser(interpolation=None, strict=False)
    score.read(bundle / 'io500-results/result.txt')
    require(score.getint('run', 'procs') == case['clients'] and score['run']['mode'] == 'extended',
            'IO500 result rank count/mode mismatch')
    for r in parsed:
        require(r['phase'] in score, 'IO500 result missing phase')
        # Logs print rounded values; allow only their known six-decimal precision.
        value = float(score[r['phase']]['score'])
        require(abs(value - r['value']) <= max(0.000001, abs(value)*1e-6), 'result/log rate differs')
    return parsed


def summarize(root, allow_partial=False):
    cohort = root / 'cohort'
    state = json.loads((cohort / 'matrix.json').read_text())
    inputs = json.loads((cohort / 'inputs.json').read_text())
    require(sha(cohort / 'inputs.json') == state['inputs_sha256'], 'input receipt mismatch')
    require(state['schedule'] == schedule(), 'declared schedule changed')
    complete = state['status'] == 'completed' and len(state['cases']) == 45
    require(allow_partial or complete, 'matrix incomplete; no final report')
    observed = [r['case'] for r in state['cases']]
    require(observed == schedule()[:len(observed)], 'case sequence differs from declared prefix')
    groups, rows = {}, []
    for item in state['cases']:
        if item['status'] != 'passed':
            require(allow_partial, 'failed/incomplete case')
            continue
        case = item['case']
        for phase in verify_record(root, item, inputs):
            row = dict(system=case['system'], topology=case['topology'], repeat=case['repeat'], **phase)
            rows.append(row)
            groups.setdefault((case['system'], case['topology'], phase['phase']), []).append(row)
    summaries = []
    for (system, topology, phase), values in sorted(groups.items()):
        require(not complete or len(values) == 3, 'missing replicate')
        rates = [v['value'] for v in values]
        require(len({v['unit'] for v in values}) == 1, 'replicate units differ')
        summaries.append(dict(system=system, topology=topology, phase=phase, unit=values[0]['unit'],
            n=len(rates), median=statistics.median(rates), minimum=min(rates), maximum=max(rates),
            raw=rates, seconds=[v['seconds'] for v in values]))
    return dict(status=state['status'], complete=complete, samples=len(rows)//22,
                qualification=state['qualification'], failed=[r for r in state['cases'] if r['status'] == 'failed'],
                summary=summaries, measurements=rows)


def report(value):
    med = {(r['system'], r['topology'], r['phase']): r for r in value['summary']}
    systems = ('legofs', 'fuse', 'iov')
    labels = dict(legofs='LegoFS', fuse='3FS CXL FUSE', iov='3FS CXL POSIX/IOV staging')
    lines = ['# IO500 CXL FUSE / IOV client 与 server 扩展', '', '## Material Passport', '',
        '- Origin Skill: academic-research-suite / experiment-agent', '- Origin Mode: run + validate',
        '- Origin Date: 2026-09-12', '- Verification Status: ANALYZED',
        '- Version Label: posix_cxl_io500_scaling_v1', '',
        f"状态：{value['status']}；完整性能轮次 {value['samples']}/45；每轮22阶段。", '',
        ('三次独立轮次的中位数 [min,max]；短时诊断，不是正式IO500分数。' if value['complete'] else
         '进行中预览：仅统计当前已归档轮次，部分条件尚未凑齐三次；不是最终结论。'), '',
        'Bench原样；两条3FS路径均为CXL，模拟RDMA的client/server bulk复制保留。',
        'IOV使用普通应用buffer经FS staging；不是应用同buffer零拷贝。',
        '两条3FS均新引擎/RF1/stripe1/四targets、read/writeback cache关闭、writebuffer0、16GiBregion。',
        'NUMA1；client/FUSE CPU1/7/13；3FS服务6/8/12核，LegoFS authority1/2/4核，非等总核比较。',
        'LegoFS64GiB sparse region；固定3C扩server。3FS仅扩storage，不扩meta。',
        'IO500共享文件在stripe1下最多用一个storage，三份FPP文件最多用三个；全程计数不是逐阶段均衡证明。',
        '原cache-on/1GiB历史组不混入本轮；根盘只存紧凑证据，运行数据逐轮清理。', '',
        '## 异常与资格验证', '',
        '首个1C1S资格验证原始执行通过22阶段，但计数解析误计两个零I/O辅助进程。',
        '仅修正观察器并按原始证据重验；保留原失败JSON、独立验证回执，没有重跑或替换样本。',
        '1C1S、3C2S、3C4S资格验证与45次性能样本分开。详见 operational-notes.md。', '']
    if value['complete']:
        rate = lambda system, point, phase: med[system, point, phase]['median']
        ratios = [rate('iov', '3c1s', p) / rate('fuse', '3c1s', p)
                  for p in EXPECTED_PHASES if p.startswith('ior-')]
        lines += ['## 主要观察', '',
            f'3C1S的九个IOR数据阶段，IOV/FUSE中位数比值为{min(ratios):.3f}–{max(ratios):.3f}×。',
            '本轮没有出现IOV全面加速；不能把前述macrobench的大块I/O收益直接外推到IO500。',
            f"3C4S easy-read中IOV为FUSE的{rate('iov','3c4s','ior-easy-read')/rate('fuse','3c4s','ior-easy-read'):.3f}×，"
            f"但hard-read为{rate('iov','3c4s','ior-hard-read')/rate('fuse','3c4s','ior-hard-read'):.3f}×，收益依阶段而异。",
            f"3C1S LegoFS/IOV：easy-read {rate('legofs','3c1s','ior-easy-read')/rate('iov','3c1s','ior-easy-read'):.2f}×，"
            f"hard-read {rate('legofs','3c1s','ior-hard-read')/rate('iov','3c1s','ior-hard-read'):.2f}×；",
            f"mdtest-easy-stat {rate('legofs','3c1s','mdtest-easy-stat')/rate('iov','3c1s','mdtest-easy-stat'):.2f}×，"
            f"mdtest-easy-delete {rate('legofs','3c1s','mdtest-easy-delete')/rate('iov','3c1s','mdtest-easy-delete'):.2f}×。", '',
            '| 扩展轴/阶段 | FUSE | IOV | LegoFS |', '|---|---:|---:|---:|']
        for axis, first, last in [('1→3C，固定1S', '1c1s', '3c1s'), ('1→4S，固定3C', '3c1s', '3c4s')]:
            for phase in ('ior-easy-write', 'ior-easy-read'):
                ratios = [rate(s,last,phase)/rate(s,first,phase) for s in ('fuse','iov','legofs')]
                lines.append('| ' + axis + '/' + phase + ' | ' + ' | '.join(f'{x:.3f}×' for x in ratios) + ' |')
        lines += ['', '以上是各系统相对自己基线的扩展倍数，不代表绝对性能胜负。',
                  'hard/random/metadata并不全部随storage数增加而加速，完整逐阶段曲线如下。', '']
    for point in POINTS:
        lines += [f'## {point.upper()} 三路径对照', '',
            '| 阶段 | 单位 | LegoFS | FUSE | IOV | IOV/FUSE | LegoFS/IOV |',
            '|---|---|---:|---:|---:|---:|---:|']
        for phase in EXPECTED_PHASES:
            found = [med.get((s, point, phase)) for s in systems]
            if not all(found):
                continue
            l, f, v = found
            cells = [phase, l['unit']] + [f"{r['median']:.6f} [{r['minimum']:.6f},{r['maximum']:.6f}]" for r in found]
            cells += [f"{v['median']/f['median']:.3f}×" if f['median'] else 'N/A',
                      f"{l['median']/v['median']:.3f}×" if v['median'] else 'N/A']
            lines.append('| ' + ' | '.join(cells) + ' |')
        lines.append('')
    for axis, points in (('Client：1C→2C→3C，固定1S', POINTS[:3]),
                          ('Server：1S→2S→4S，固定3C', POINTS[2:])):
        lines += ['## ' + axis, '', '| 阶段 | LegoFS 中点/基线 | LegoFS 终点/基线 | FUSE 中点/基线 | FUSE 终点/基线 | IOV 中点/基线 | IOV 终点/基线 |',
                  '|---|---:|---:|---:|---:|---:|---:|']
        for phase in EXPECTED_PHASES:
            cells = [phase]
            for system in systems:
                r = [med.get((system, p, phase)) for p in points]
                cells += [f"{v['median']/r[0]['median']:.3f}×" if all(r) and r[0]['median'] else 'N/A' for v in r[1:]]
            lines.append('| ' + ' | '.join(cells) + ' |')
        lines.append('')
    lines += ['## 解释边界与统计检查', '',
        '全22阶段和全部三次原值保留在summary.json/measurements.csv；零匹配与预期INVALID保留。',
        '三次为描述性重复，范围不是置信区间。短固定工作阶段不能称为持续带宽。',
        'IOV/FUSE差异同时包含数据通路、分段、staging与调度；不能仅归因为shadow copy或权限下放。',
        '未测持久介质掉电语义、跨主机扩展、正式300s IO500或更高客户端数。', '',
        '11/11 fallacy scan：按阶段/拓扑分层（Simpson）；不外推个体延迟或跨主机（ecological）；',
        '固定矩阵不按速度选样（Berkson/collider）；无条件概率推断（base rate）；',
        '固定三轮不挑极端重测（regression to mean）；失败保留（survivorship）；',
        '全阶段展示不选显著项（look-elsewhere）；运行前固定矩阵（forking paths）；',
        '混合FS路径变化和不等资源明确披露，不作单机制因果或反向因果判断（causation/reverse causality）。',
        'Confidence: CAUTION for generalization. No p-values, CIs or formal performance reproducibility certification.']
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--allow-partial', action='store_true')
    args = parser.parse_args()
    value = summarize(args.root, args.allow_partial)
    (args.root / 'summary.json').write_text(json.dumps(value, indent=2) + '\n')
    (args.root / 'REPORT.md').write_text(report(value))
    with (args.root / 'measurements.csv').open('w') as stream:
        writer = csv.DictWriter(stream, fieldnames=('system', 'topology', 'repeat', 'phase', 'value', 'unit', 'seconds'))
        writer.writeheader()
        writer.writerows(value['measurements'])
    print(json.dumps(dict(status=value['status'], samples=value['samples'], phase_rows=len(value['measurements']))))


if __name__ == '__main__':
    main()
