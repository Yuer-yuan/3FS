#!/usr/bin/env python3
"""CXL FS-only FUSE/IOV deployment for the original full22 IO500 workload."""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import shutil
import sys

from posix_frontend import sha, validate_counters


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--topology', required=True)
    parser.add_argument('--mode', choices=('fuse', 'iov'), required=True)
    parser.add_argument('--build', type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get('HF3FS_POSIX_IO500_BOUND') != '1':
        os.execvpe('/usr/bin/numactl', ['numactl', '--membind=1', '--physcpubind=18-23',
            sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
            dict(os.environ, HF3FS_POSIX_IO500_BOUND='1'))
    repo, build = args.repo.resolve(strict=True), args.build.resolve(strict=True)
    manifest = repo / 'target/results/giga-native-3fs/build-manifest.json'
    build_info = json.loads((build / 'build.json').read_text())
    if build_info['status'] != 'built' or sha(manifest) != build_info['service_manifest_sha256']:
        raise ValueError('POSIX frontend/service build mismatch')
    for entry in build_info['artifacts'].values():
        if sha(entry['path']) != entry['sha256']:
            raise ValueError('POSIX frontend artifact changed')
    api = repo / 'target/build/giga-native-3fs/hf3fs/src/lib/api/libhf3fs_api_shared.so'
    if sha(api) != build_info['inputs'][str(api)]:
        raise ValueError('IOV API changed')
    if any(shutil.disk_usage(p).free < n * 2**30 for p, n in (('/', 4), ('/tmp', 20), ('/dev/shm', 24))):
        raise ValueError('insufficient disk headroom')
    sys.path.insert(0, str(repo / 'fast-paper/artifact/mlperf-storage/comparison'))
    from run_3fs_current import configure_fuse_cache_set, verify_fuse_read_cache
    from run_posix_iov_probe import disable_write_buffer
    import cluster
    import io500
    import runtime
    os.environ.pop('LD_PRELOAD', None)
    for key in list(os.environ):
        if key.startswith(('HF3FS_CXL_POSIX_', 'HF3FS_CXL_NATIVE_IOV', 'BADFS_', 'INTERCEPT_')):
            os.environ.pop(key)

    class FilesystemCluster(cluster.NativeCluster):
        def __init__(self, config):
            super().__init__(config)
            self.topology = replace(self.topology, region_bytes=16 * 2**30)
            self.posix_mode = args.mode
            self.posix_library = build / 'libhf3fs_posix.so'
            self.ledger.update(fs_frontend=dict(mode=args.mode, benchmark_unchanged=True,
                application_buffers='ordinary heap; FS staging, not app same-buffer zero-copy',
                read_cache=False, writeback_cache=False, write_buffer_size=0,
                region_bytes=self.topology.region_bytes, cxl_bulk_copy_preserved=True))

        def prepare(self):
            super().prepare()
            configure_fuse_cache_set(self.configs, False)
            for directory in self.configs:
                disable_write_buffer(directory)
            runtime.atomic_json(self.roots.bundle / 'rendered-config.json', {
                str(p.relative_to(self.roots.bundle / 'config')): sha(p)
                for p in (self.roots.bundle / 'config').rglob('*') if p.is_file()})
            shutil.copy2(manifest, self.roots.bundle / 'build-manifest.json')
            shutil.copy2(build / 'build.json', self.roots.bundle / 'posix-build.json')
            sources = self.roots.bundle / 'fs-deployment-sources'
            sources.mkdir()
            for name in ('rank.py', 'io500.py', 'posix_frontend.py', 'posix_io500.py'):
                shutil.copy2(Path(__file__).with_name(name), sources / name)

        def mount_clients(self):
            super().mount_clients()
            self.ledger.update(effective_fuse_read_cache=[verify_fuse_read_cache(m, False) for m in self.mounts])

    cluster.NativeCluster = FilesystemCluster

    def workload(active):
        value = io500.run(active)
        records = [r['fs_frontend'] for r in value['ranks']]
        if any(r['shadow_delta'] != [0, 0] for r in records):
            raise ValueError('unexpected StorageClient shadow copy')
        text = (active.roots.bundle / 'io500.log').read_text()
        counters = [json.loads(x) for x in re.findall(r'(?m)^HF3FS_POSIX_COUNTERS (\{[^\r\n]+\})', text)]
        if args.mode == 'iov':
            selected = validate_counters(counters, [r['pid'] for r in records])
            value['auxiliary_posix_counters'] = [r for r in counters if r not in selected]
            counters = selected
        elif counters:
            raise ValueError('FUSE control loaded IOV adapter')
        value['posix_counters'] = counters
        value['fs_mode'] = args.mode
        return value

    result = cluster.execute_cluster(cluster.ClusterConfig(repo, manifest, args.topology, args.run_id), workload)
    print(json.dumps(dict(status=result['status'], first_failure=result.get('first_failure'),
                          run_id=args.run_id, topology=args.topology, mode=args.mode)), flush=True)
    return 0 if result['status'] == 'passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
