#!/usr/bin/env python3
"""Local controller for unmodified macrobench with filesystem-only variants."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import time


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo', type=Path, required=True)
    p.add_argument('--host', default='giga')
    p.add_argument('--remote-repo', default='/root/cxlmemsim-riscv-io500')
    p.add_argument('--control-path', required=True)
    p.add_argument('--build', required=True)
    p.add_argument('--tag', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--bench-lock', type=Path, required=True)
    p.add_argument('--repeats', type=int, choices=(1, 3), default=3)
    p.add_argument('--resume', action='store_true', help='resume only between completed, archived runs')
    args = p.parse_args()
    assert args.tag and all(c in 'abcdefghijklmnopqrstuvwxyz0123456789-_' for c in args.tag)
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=args.resume)
    expected = json.loads(args.bench_lock.read_text())
    local_base = args.repo.resolve() / 'fast-paper/artifact/mlperf-storage'
    remote = args.remote_repo + '/fast-paper/artifact/mlperf-storage'
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=15',
           '-o', 'ServerAliveCountMax=4', '-o', 'ControlPath=' + args.control_path,
           '-o', 'ControlMaster=auto', '-o', 'ControlPersist=3600',
           '-o', 'ProxyCommand=nc -X connect -x 127.0.0.1:10808 %h %p']
    order = [(1, ('iov', 'legofs', 'fuse')), (2, ('legofs', 'fuse', 'iov')), (3, ('fuse', 'iov', 'legofs'))]
    state = dict(status='running', tag=args.tag, started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 application_unchanged=True, benchmark_source_hashes=expected, attempts=[],
                 order=order[:args.repeats], frontend_build=args.build,
                 fs_regions_gib={'3fs': 16, 'legofs': 64}, source_file_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    finished = set()
    if args.resume:
        previous = json.loads((out / 'controller.json').read_text())
        assert previous['tag'] == args.tag and previous['benchmark_source_hashes'] == expected
        assert previous['frontend_build'] == args.build
        expected_order = [(r, s) for r, systems in order[:args.repeats] for s in systems]
        completed = [(r['repeat'], r['system']) for r in previous['attempts']]
        assert completed == expected_order[:len(completed)], 'not a completed prefix'
        for row in previous['attempts']:
            assert row['status'] == 'completed' and row['returncode'] == row['collection_returncode'] == 0, 'cannot resume an active/failed attempt'
            assert (out / row['run_id'] / 'result.json').is_file(), 'missing archived result'
            finished.add((row['repeat'], row['system']))
        previous.setdefault('resumptions', []).append(dict(
            time_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            reason='SSH banner timeout at read-only source_check between completed runs; no workload failed or retried',
            new_controller_sha256=state['source_file_sha256']))
        state = previous
        state['status'] = 'running'

    def save():
        (out / 'controller.json').write_text(json.dumps(state, indent=2) + '\n')

    def source_check():
        for relative, digest in expected.items():
            assert hashlib.sha256((local_base / relative).read_bytes()).hexdigest() == digest, relative
        script = ('import hashlib,json,pathlib,sys; base=pathlib.Path(sys.argv[1]); '
                  'expected=json.loads(sys.argv[2]); '
                  'actual={p:hashlib.sha256((base/p).read_bytes()).hexdigest() for p in expected}; '
                  'print(json.dumps(actual)); assert actual==expected, "remote bench changed"')
        checked = subprocess.run([*ssh, args.host, shlex.join(['python3', '-c', script, remote, json.dumps(expected)])],
                                 capture_output=True, text=True, timeout=90)
        assert checked.returncode == 0, checked.stdout + checked.stderr

    save()
    source_check()
    for repeat, systems in order[:args.repeats]:
        for system in systems:
            if (repeat, system) in finished:
                continue
            source_check()
            run_id = f'{args.tag}-{system}-r{repeat}'
            if system == 'legofs':
                source = remote + '/results/' + run_id
                command = ['python3', remote + '/run_namespace_fast.py', '--job',
                           remote + f'/jobs/explore-r{repeat}.json', '--output', source,
                           '--run-root', '/dev/shm/legofs-mlperf-' + run_id, '--region-gib', '64']
            else:
                source = args.remote_repo + '/target/results/giga-native-3fs/' + run_id
                command = ['python3', args.remote_repo + '/components/3FS/deploy/giga-native/posix_macro.py',
                    '--repo', args.remote_repo, '--run-id', run_id, '--mode', system,
                    '--build', args.build, '--bench-lock', '/tmp/macro-fs-only-20260912-bench-lock.json',
                    '--region-gib', '16']
            row = dict(system=system, repeat=repeat, run_id=run_id, command=command, remote_bundle=source,
                       status='running', started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
            state['attempts'].append(row)
            save()
            print('FS_MACRO_START ' + run_id, flush=True)
            started = time.monotonic()
            with (out / (run_id + '.log')).open('wb') as log:
                result = subprocess.run([*ssh, args.host, shlex.join([
                    'timeout', '--signal=TERM', '--kill-after=30', '1800', *command])],
                    stdout=log, stderr=subprocess.STDOUT, timeout=1900)
            row.update(returncode=result.returncode, seconds=time.monotonic() - started,
                       status='completed' if result.returncode == 0 else 'failed')
            save()
            copied = subprocess.run(['rsync', '-a', '--mkpath', '-e', shlex.join(ssh),
                                     args.host + ':' + source + '/', str(out / run_id) + '/'],
                                    capture_output=True, text=True, timeout=180)
            row['collection_returncode'] = copied.returncode
            if copied.returncode:
                row['collection_error'] = copied.stderr
            save()
            print('FS_MACRO_END ' + run_id + ' ' + row['status'], flush=True)
            if result.returncode or copied.returncode:
                state['status'] = 'failed'
                save()
                return 2
            source_check()
    state.update(status='completed', finished_utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
    save()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
