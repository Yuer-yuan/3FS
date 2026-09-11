#!/usr/bin/env python3
"""Pinned serial CXL full22 FS comparison; no workload retries or bench edits."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import tomllib

from posix_frontend import sha, validate_counters

POINTS = ('1c1s', '2c1s', '3c1s', '3c2s', '3c4s')
TAG = 'posix-io500-20260912'
MANIFEST_SHA = '39793f854de71f867145ba8923b6e60a1541e9cb2c508ee4865c21016fbfe59b'
PROFILE_SHA = '4138e81d71092821174d49c4f6b7e1ab06cced907f0dc3937431a6c83387a12a'
IO500_SHA = '28a17bd09cb8dac6b87ddda58eb68af48182dd3de4e51f99938c131ce3fe237d'
FRONTEND_SHA = 'c4df70ce88485ec892629c5d444a8f88f303288f0cf1ce9fdb5af4b4a4351550'


def schedule():
    cases = []
    for repeat in (1, 2, 3):
        for i, point in enumerate(POINTS if repeat != 2 else tuple(reversed(POINTS))):
            modes = ('fuse', 'iov', 'legofs')
            shift = (repeat - 1 + i) % 3
            for mode in modes[shift:] + modes[:shift]:
                clients, servers = int(point[0]), int(point[2])
                cases.append(dict(name=f'{mode}-{point}-r{repeat}', system=mode,
                    topology=point, clients=clients, servers=servers, repeat=repeat,
                    client_cpus=['1', '7', '13'][:clients],
                    server_cpus=['19', '18', '20', '21'][:servers],
                    server_physical_cores=servers if mode == 'legofs' else 4 + 2*servers))
    return cases


def require(value, message):
    if not value:
        raise ValueError(message)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def check_hashes(inputs):
    for path, expected in inputs['sha256'].items():
        require(sha(path) == expected, 'input changed: ' + path)


def check_three_fs(bundle, case, require_clean=True):
    import io500
    import run_namespace_io500_matrix as matrix
    selected = bundle / 'validated-result.json'
    if selected.exists():
        value = json.loads(selected.read_text())
        require(value['validation_recovery']['original_result_sha256'] == sha(bundle / 'result.json'),
                'original qualification result changed')
    else:
        value = json.loads((bundle / 'result.json').read_text())
    require(value['status'] == 'passed', '3FS failure: ' + repr(value.get('first_failure')))
    require(value['topology'] == case['topology'], 'topology mismatch')
    require(value['build_manifest_sha256'] == MANIFEST_SHA, 'service manifest changed')
    require(value['profile_sha256'] == PROFILE_SHA, 'workload profile changed')
    require(value['server_physical_cores'] == case['server_physical_cores'], 'core budget changed')
    require(value['region']['bytes'] == 16 * 2**30 and value['region']['memory_node'] == 1,
            'CXL region or NUMA policy changed')
    require(value['replication_factor'] == 1 and value['storage_engine']['verified'], 'RF/engine mismatch')
    targets = value['storage_engine']['targets']
    require(len(targets) == 4 and all(t['only_chunk_engine'] is True and
            sha(bundle / t['archive']) == t['sha256'] for t in targets), 'target proof mismatch')
    work = value['workload']
    require(work['status'] == 'passed' and work['returncode'] == 0 and work['fs_mode'] == case['system'],
            'workload/FS mode mismatch')
    watchdog = io500.PhaseWatchdog()
    watchdog.feed((bundle / 'io500.log').read_text(), 1)
    watchdog.finish()
    fields = ('phase', 'value', 'unit', 'seconds')
    require([{k: p[k] for k in fields} for p in work['phases']] ==
            [{k: p[k] for k in fields} for p in watchdog.records], 'raw phase log mismatch')
    io500.parse_result(bundle / 'io500-results/result.txt', case['clients'])
    require('[OK] But this is an invalid run!' in (bundle / 'io500-verify.log').read_text(),
            'missing IO500 verifier receipt')
    profile = Path(__file__).with_name('io500-all-bounded-1s.ini')
    require(matrix.normalized_profile(bundle / 'io500-effective.ini') == matrix.normalized_profile(profile),
            'effective benchmark geometry changed')
    ranks = work['ranks']
    require(len(ranks) == case['clients'], 'rank evidence missing')
    for r in ranks:
        disk = json.loads((bundle / 'ranks' / f"rank-{r['rank']}.json").read_text())
        require(disk == r, 'rank receipt changed')
        f = r['fs_frontend']
        require(f['mode'] == case['system'] and f['shadow_delta'] == [0, 0], 'FS copy mode changed')
        require(f['library_sha256'] == FRONTEND_SHA, 'frontend build changed')
        require(f['placement']['phase'] == 'application-exec' and
                f['placement']['allowed_cpus'] == [int(case['client_cpus'][r['rank']])], 'rank CPU mismatch')
        loaded = f['library'] in f['placement']['maps']
        require(loaded == (case['system'] == 'iov'), 'actual DSO loading differs from FS mode')
    if case['system'] == 'iov':
        validate_counters(work['posix_counters'], [r['fs_frontend']['pid'] for r in ranks])
    require(all(x['verified'] and x['value'] == 'false' for x in value['effective_fuse_read_cache']),
            'effective FUSE read cache enabled')
    for index in range(case['clients'] + 1):
        config = tomllib.loads((bundle / f'config/node{index}/hf3fs_fuse_main.toml').read_text())
        require(config['enable_read_cache'] is False and config['enable_writeback_cache'] is False and
                config['io_bufs']['write_buf_size'] == 0, 'FUSE buffer/cache policy changed')
    require(len(value['storage_transport']) == case['servers'], 'storage transport coverage missing')
    totals = value['transport_totals']
    require(totals['cxl_rpc_requests'] == totals['cxl_rpc_responses'] > 0, 'CXL RPC closure failed')
    require(totals['cxl_bulk_read_bytes'] > 0 and totals['cxl_bulk_write_bytes'] > 0, 'CXL bulk absent')
    require(all(totals[k] == 0 for k in ('rdma_open_attempts', 'tcp_data_plane_bytes', 'bootstrap_serving_bytes')),
            'forbidden serving fallback')
    teardown = value['teardown']
    require(teardown['all_owned_processes_stopped'] and all(not teardown[k] for k in
            ('errors', 'occupied_ports', 'active_mounts')), 'owned teardown incomplete')
    if require_clean:
        require(all(not Path(value['runtime_roots'][k]).exists() for k in ('volatile', 'storage')),
                'owned scratch still present')
    return dict(phases=work['phases'], accounting=totals, fs_mode=case['system'],
                posix_counters=work['posix_counters'], expected_invalid=True)


def recover_completed_qualification(bundle, case):
    """Replay only an exact known observer failure; never rerun or edit raw data."""
    import cluster
    import io500
    import re
    import runtime
    import storage_layout
    import topology
    value = json.loads((bundle / 'result.json').read_text())
    require(value['status'] == 'failed' and value['first_failure']['stage'] == 'workload' and
            value['first_failure']['message'] == 'missing/duplicate/unexpected POSIX process counter',
            'not the exact recoverable post-workload validation failure')
    watchdog = io500.PhaseWatchdog()
    text = (bundle / 'io500.log').read_text()
    watchdog.feed(text, 1)
    watchdog.finish()
    ranks = [json.loads(p.read_text()) for p in sorted((bundle / 'ranks').glob('rank-*.json'))]
    require(all(r['returncode'] == 0 for r in ranks), 'application failure is not recoverable')
    rows = [json.loads(s) for s in re.findall(r'(?m)^HF3FS_POSIX_COUNTERS (\{[^\r\n]+\})', text)]
    counters = validate_counters(rows, [r['fs_frontend']['pid'] for r in ranks])
    selected = topology.topology(case['topology'])
    participants = {p['endpoint'] for p in value['manifest']['participants']}
    transport = value['teardown']['transport_records']
    totals = cluster.validate_transport(transport, participants, case['clients'])
    nodes = storage_layout.validate_storage_transport(transport, selected.storage_nodes,
        {n.node_id: json.loads((bundle / 'processes' / (n.role + '.json')).read_text())['pid']
         for n in selected.storage_nodes}, cluster._session(value['run_id']), value['manifest']['manifestSha256'])
    original_failure = value.pop('first_failure')
    value.update(status='passed', transport_totals=totals, storage_transport=nodes,
        workload=dict(status='passed', returncode=0, fs_mode='iov', phases=watchdog.records,
                      ranks=ranks, posix_counters=counters,
                      auxiliary_posix_counters=[r for r in rows if r not in counters]),
        validation_recovery=dict(original_result_sha256=sha(bundle / 'result.json'),
            original_failure=original_failure, workload_rerun=False,
            reason='zero-I/O popen helper exit counters incorrectly counted as MPI ranks'))
    path = bundle / 'validated-result.json'
    require(not path.exists(), 'qualification replay already exists')
    save(path, value)
    check_three_fs(bundle, case, require_clean=False)
    for receipt in (bundle / 'processes').glob('*.json'):
        pid = json.loads(receipt.read_text())['pid']
        require(not Path(f'/proc/{pid}').exists(), 'owned process remains; no cleanup')
    token = (bundle / '.hf3fs-run-owner').read_text().strip()
    removed = []
    for key, parent in (('volatile', Path('/dev/shm')), ('storage', Path('/tmp'))):
        root = Path(value['runtime_roots'][key])
        require(root == parent / ('hf3fs-' + value['run_id']), 'unexpected cleanup target')
        runtime.safe_remove_owned_root(root, parent, token)
        removed.append(str(root))
    save(bundle / 'validation-cleanup.json', dict(removed=removed, raw_results_preserved=True,
                                                payload_recoverable=False, time=time.time()))
    return check_three_fs(bundle, case)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo', type=Path, required=True)
    p.add_argument('--build', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    repo, out = args.repo.resolve(strict=True), args.output.resolve()
    deploy = repo / 'components/3FS/deploy/giga-native'
    sys.path[:0] = [str(repo / 'components/legofs/scripts'),
                    str(repo / 'target/run/workspace-recovery-validation')]
    import build
    import run_namespace_io500_matrix as matrix
    from native_task_admission import check_owned_idle
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / 'matrix.json'
    state_path.touch(exist_ok=False)
    record = dict(status='preparing', started=time.time(), schedule=schedule(),
                  qualification=[], cases=[], benchmark_unchanged=True, cxl_only_serving=True)

    def state():
        save(state_path, record)

    def host():
        free = {p: shutil.disk_usage(p).free for p in ('/', '/tmp', '/dev/shm')}
        require(all(free[p] >= n * 2**30 for p, n in (('/', 4), ('/tmp', 20), ('/dev/shm', 24))),
                'disk headroom admission failed')
        return dict(boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                    free_bytes=free, admission=check_owned_idle(repo))

    def invoke(case, argv, bundle, entries):
        check_hashes(inputs)
        before = host()
        require(before['boot_id'] == inputs['host']['boot_id'], 'host rebooted')
        require(not bundle.exists(), 'attempt bundle already exists: ' + str(bundle))
        row = dict(case=case, command=argv, bundle=str(bundle), started=time.time(), before=before, status='running')
        entries.append(row)
        state()
        env = {k: v for k, v in os.environ.items() if k != 'LD_PRELOAD' and
               not k.startswith(('HF3FS_', 'BADFS_', 'INTERCEPT_'))}
        with (out / 'logs' / (case['name'] + '.log')).open('xb') as log:
            process = subprocess.Popen(argv, cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, start_new_session=True)
            row.update(pid=process.pid, proc_stat=Path(f'/proc/{process.pid}/stat').read_text())
            state()
            print('START', case['name'], process.pid, flush=True)
            deadline = time.monotonic() + 900
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    print('HARD_TIMEOUT', case['name'], 'sending SIGINT for owned cleanup', flush=True)
                    process.send_signal(signal.SIGINT)
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    raise TimeoutError(case['name'])
                time.sleep(1)
            row['returncode'] = process.returncode
        require(row['returncode'] == 0, case['name'] + ' failed; see retained log')
        row['verification'] = (check_three_fs(bundle, case) if case['system'] != 'legofs' else
                               matrix.verify_bundle(*lego_config(case), bundle))
        check_hashes(inputs)
        row.update(status='passed', finished=time.time(), after=host(), result_sha256=sha(bundle / 'result.json'))
        state()
        print('PASS', case['name'], '22/22', flush=True)

    def three_command(case):
        run_id = TAG + '-' + case['name']
        return ([sys.executable, str(deploy / 'posix_io500.py'), '--repo', str(repo), '--build', str(args.build),
                 '--run-id', run_id, '--topology', case['topology'], '--mode', case['system']],
                repo / 'target/results/giga-native-3fs' / run_id)

    def lego_config(case):
        point = dict(name=case['name'], axis='client' if case['servers'] == 1 else 'server',
                     variant='current', client_cpus=case['client_cpus'], server_cpus=case['server_cpus'], workers=1)
        config = dict(version=1, variants={'current': dict(kind='namespace', **lego_bins)}, cases=[point],
            run_root='/dev/shm/legofs-posix-io500-20260912',
            library_dir=str(repo / 'target/build/giga-native/lib'),
            io500=products['artifacts']['io500']['path'], verifier=products['artifacts']['io500-verify']['path'],
            profile=str(profile), memory_node='1', cpu_node='0', region_bytes=64 * 2**30, timeout=300,
            general_extent_budget=262144, small_segment_budget=4096,
            cq_wait_mode='cooperative_yield', authority_wait_mode='cooperative_yield',
            mpirun='/usr/bin/mpirun.openmpi', sha256=inputs['sha256'])
        matrix.validate(config)
        return config, point

    state()
    try:
        manifest = repo / 'target/results/giga-native-3fs/build-manifest.json'
        profile = deploy / 'io500-all-bounded-1s.ini'
        require(sha(manifest) == MANIFEST_SHA and sha(profile) == PROFILE_SHA, 'unexpected service/profile identity')
        require(sha(args.build / 'libhf3fs_posix.so') == FRONTEND_SHA, 'unexpected frontend identity')
        products = build.verify_manifest(manifest)
        require(products['artifacts']['io500']['sha256'] == IO500_SHA, 'IO500 changed')
        lego_profile = repo / 'fast-paper/artifact/mlperf-storage/profiles/namespace-fast.json'
        lego = json.loads(lego_profile.read_text())
        lego_bins = {key: str(repo / 'target/build/namespace-native' / item['path'])
                     for key, item in lego['binaries'].items()}
        for key, path in lego_bins.items():
            require(sha(path) == lego['binaries'][key]['sha256'], 'LegoFS binary changed')
        sources = repo / 'target/build/giga-native/sources/io500'
        bench_files = [p for p in sources.rglob('*') if p.is_file() and p.suffix in ('.c', '.h', '.ini')]
        require(bool(bench_files), 'IO500 source unavailable')
        paths = [manifest, profile, lego_profile, args.build / 'libhf3fs_posix.so', args.build / 'build.json']
        paths += [Path(v['path']) for v in products['artifacts'].values()] + list(map(Path, lego_bins.values()))
        paths += list(deploy.glob('*.py')) + bench_files
        paths += [repo / 'components/legofs/scripts' / n for n in
                  ('run_namespace_io500_matrix.py', 'run_namespace_io500_bringup.py', 'run_namespace_io500_rank.py')]
        paths += [repo / 'fast-paper/artifact/mlperf-storage/comparison' / n for n in
                  ('run_3fs_current.py', 'run_posix_iov_probe.py')]
        paths += [repo / 'target/build/giga-native-3fs/hf3fs/src/lib/api/libhf3fs_api_shared.so']
        inputs = dict(host=host(), sha256={str(x): sha(x) for x in paths}, products=products,
            lego=lego, schedule=schedule(), io500_source_files=len(bench_files),
            three_fs_source=list(build.source_fingerprint(repo / 'components/3FS/src')),
            fs_policy=dict(read_cache=False, writeback_cache=False, write_buffer_size=0,
                           cxl_region_gib=16, application_buffer='unchanged heap', cxl_bulk_preserved=True))
        save(out / 'inputs.json', inputs)
        (out / 'logs').mkdir()
        shutil.copy2(__file__, out / 'executed-controller.py')
        shutil.copy2(profile, out / 'io500-profile.ini')
        record.update(status='qualifying', inputs_sha256=sha(out / 'inputs.json'))
        state()
        for point in ('1c1s', '3c2s', '3c4s'):
            clients, servers = int(point[0]), int(point[2])
            case = dict(name='qualify-iov-' + point, system='iov', topology=point, clients=clients,
                        servers=servers, client_cpus=['1', '7', '13'][:clients], server_physical_cores=4 + 2*servers)
            argv, bundle = three_command(case)
            if point == '1c1s':
                # This explicitly named pre-controller qualification is never rerun.
                original = json.loads((bundle / 'result.json').read_text())
                verification = (recover_completed_qualification(bundle, case) if original['status'] == 'failed'
                                else check_three_fs(bundle, case))
                record['qualification'].append(dict(case=case, status='passed', bundle=str(bundle),
                    verification=verification, result_sha256=sha(bundle / 'result.json'), reused_qualification=True))
                state()
            else:
                invoke(case, argv, bundle, record['qualification'])
        record['status'] = 'running'
        state()
        for case in inputs['schedule']:
            if case['system'] == 'legofs':
                config, point = lego_config(case)
                save(out / 'logs' / (case['name'] + '-config.json'), config)
                argv = matrix.command(config, point, out / 'legofs')
                bundle = out / 'legofs' / case['name']
            else:
                argv, bundle = three_command(case)
            invoke(case, argv, bundle, record['cases'])
        check_hashes(inputs)
        require(list(build.source_fingerprint(repo / 'components/3FS/src')) == inputs['three_fs_source'],
                '3FS implementation source changed during experiment')
        record.update(status='completed', finished=time.time(), final_host=host())
    except Exception as error:
        record.update(status='failed', error=repr(error), finished=time.time())
        for entries in (record['qualification'], record['cases']):
            if entries and entries[-1]['status'] == 'running':
                entries[-1].update(status='failed', error=repr(error))
        raise
    finally:
        state()


if __name__ == '__main__':
    # Fixed lock inode prevents two controllers using this FS experiment scope.
    with Path('/tmp/posix-cxl-io500-scaling-20260912.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        main()
