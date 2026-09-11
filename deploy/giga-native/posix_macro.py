#!/usr/bin/env python3
"""FS-only launch of the unchanged CPU macrobench; no application hooks."""
import argparse
import array
from dataclasses import replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--mode', choices=('iov', 'fuse'), required=True)
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--bench-lock', type=Path, required=True)
    parser.add_argument('--region-gib', type=int, default=16)
    parser.add_argument('--cases', nargs='+')
    parser.add_argument('--job', type=Path)
    parser.add_argument('--gpu-device', default='', choices=('', '0'))
    args = parser.parse_args()
    if os.environ.get('MACROBENCH_BOUND') != '1':
        os.execvpe('numactl', ['numactl', '--membind=1', '--physcpubind=18-23',
            sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
            dict(os.environ, MACROBENCH_BOUND='1'))
    repo = args.repo.resolve(strict=True)
    base = repo / 'fast-paper/artifact/mlperf-storage'
    comparison = base / 'comparison'
    source_lock = json.loads(args.bench_lock.read_text())
    for relative, expected in source_lock.items():
        if sha(base / relative) != expected:
            raise ValueError('bench hash changed: ' + relative)
    build = args.build.resolve(strict=True)
    build_info = json.loads((build / 'build.json').read_text())
    assert build_info['status'] == 'built'
    manifest = repo / 'target/results/giga-native-3fs/build-manifest.json'
    assert sha(manifest) == build_info['service_manifest_sha256']
    for artifact in build_info['artifacts'].values():
        assert sha(artifact['path']) == artifact['sha256']
    api = repo / 'target/build/giga-native-3fs/hf3fs/src/lib/api/libhf3fs_api_shared.so'
    assert sha(api) == build_info['inputs'][str(api)]
    if not 1 <= args.region_gib <= 16:
        raise ValueError('region size outside qualified bound')
    assert shutil.disk_usage('/dev/shm').free > (args.region_gib + 8) * 2**30
    assert shutil.disk_usage('/tmp').free > 20 * 2**30
    sys.path.insert(0, str(comparison))
    import run_3fs_current as original
    from run_posix_iov_probe import disable_write_buffer
    import cluster
    import runtime

    active = None

    class FilesystemCluster(cluster.NativeCluster):
        def __init__(self, config):
            nonlocal active
            super().__init__(config)
            self.topology = replace(self.topology, region_bytes=args.region_gib * 2**30)
            self.fs_evidence = dict(mode=args.mode, region_bytes=self.topology.region_bytes,
                application_unchanged=args.job is None,
                original_workload_engine=args.job is None, custom_job=str(args.job) if args.job else None,
                workload_engine_note='shared upstream KV engine; custom tier cohort pins its approved race fix' if args.job else 'original pinned engine',
                application_buffers='original heap, NOT shared app IOV',
                benchmark_source_hashes=source_lock, commands=[])
            self.ledger.update(fs_frontend=self.fs_evidence)
            active = self

        def prepare(self):
            super().prepare()
            for directory in self.configs:
                disable_write_buffer(directory)
            shutil.copy2(build / 'build.json', self.roots.bundle / 'posix-build.json')
            shutil.copy2(Path(__file__), self.roots.bundle / 'posix_macro.py')
            shutil.copytree(build / 'sources', self.roots.bundle / 'posix-sources')
            runtime.atomic_json(self.roots.bundle / 'bench-source-lock.json', source_lock)

        def shadows(self):
            fd = os.open(self.mounts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                values = array.array('Q', [0, 0])
                fcntl.ioctl(fd, 0x80106816, values, True)
                return list(values)
            finally:
                os.close(fd)

        def save_evidence(self):
            runtime.atomic_json(self.roots.bundle / 'fs-frontend.json', self.fs_evidence)
            self.ledger.update(fs_frontend=self.fs_evidence)

    cluster.NativeCluster = FilesystemCluster
    saved_popen = subprocess.Popen

    class FilesystemProcess(saved_popen):
        def __init__(self, command, *pargs, **kwargs):
            self.fs_row = None
            targets = {str(comparison / ('adapters/' + name))
                       for name in ('dlio_cpu.py', 'kv_cpu.py', 'kv_tiers.py')}
            if isinstance(command, list) and any(token in targets for token in command):
                env = dict(kwargs['env'])
                if args.mode == 'iov':
                    env.update(LD_PRELOAD=str(build / 'libhf3fs_posix.so'),
                        LD_LIBRARY_PATH=active.library_path, HF3FS_CXL_NATIVE_IOV='1',
                        HF3FS_CXL_POSIX_MODE='iov', HF3FS_CXL_POSIX_REPORT='1',
                        HF3FS_CXL_POSIX_MOUNT=str(active.mounts[0]))
                else:
                    for key in ('LD_PRELOAD', 'HF3FS_CXL_POSIX_MODE', 'HF3FS_CXL_NATIVE_IOV',
                                'HF3FS_CXL_POSIX_MOUNT', 'HF3FS_CXL_POSIX_REPORT'):
                        env.pop(key, None)
                kwargs['env'] = env
                self.fs_row = dict(argv=command, log=kwargs['stdout'].name,
                    environment={k: v for k, v in env.items() if k.startswith(('HF3FS_', 'MACROBENCH_'))
                                 or k in ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'CUDA_VISIBLE_DEVICES')},
                    shadow_before=active.shadows())
                active.fs_evidence['commands'].append(self.fs_row)
            super().__init__(command, *pargs, **kwargs)
            if self.fs_row is not None:
                self.fs_row['pid'] = self.pid
                active.save_evidence()

        def wait(self, *pargs, **kwargs):
            if self.fs_row is not None and 'placement' not in self.fs_row:
                process = Path('/proc') / str(self.pid)
                deadline = time.monotonic() + 3
                while process.exists() and time.monotonic() < deadline:
                    try:
                        maps = (process / 'maps').read_text()
                        status = (process / 'status').read_text()
                        # LD_PRELOAD is inherited by taskset as well as its final
                        # exec. Do not mistake a pre-exec taskset snapshot for
                        # the application's live CPU/NUMA evidence.
                        executable = (process / 'exe').resolve().name
                        if executable.startswith('python') and (args.mode == 'fuse' or
                                str(build / 'libhf3fs_posix.so') in maps):
                            self.fs_row['placement'] = dict(status=status, phase='application-exec',
                                executable=str((process / 'exe').resolve()),
                                numa_maps=(process / 'numa_maps').read_text(), maps=maps)
                            break
                    except FileNotFoundError:
                        break
                    time.sleep(.01)
            code = super().wait(*pargs, **kwargs)
            if self.fs_row is not None and 'returncode' not in self.fs_row:
                self.fs_row.update(returncode=code, shadow_after=active.shadows())
                self.fs_row['shadow_delta'] = [a - b for a, b in zip(
                    self.fs_row['shadow_after'], self.fs_row['shadow_before'])]
                active.save_evidence()
            return code

    subprocess.Popen = FilesystemProcess
    sys.argv = [str(comparison / 'run_3fs_current.py'), '--repo', str(repo), '--run-id', args.run_id,
                '--no-fuse-read-cache']
    if args.cases:
        sys.argv += ['--cases', *args.cases]
    if args.job:
        sys.argv += ['--job', str(args.job)]
    sys.argv += ['--gpu-device', args.gpu_device]
    try:
        return original.main()
    finally:
        for relative, expected in source_lock.items():
            if sha(base / relative) != expected:
                raise ValueError('bench changed during experiment: ' + relative)
        assert sha(manifest) == build_info['service_manifest_sha256']
        assert sha(api) == build_info['inputs'][str(api)]


if __name__ == '__main__':
    raise SystemExit(main())
