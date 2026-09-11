#!/usr/bin/env python3
"""Opt-in FS deployment at the final application exec; no benchmark changes."""
import array
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def rank_environment(environment, mode, view, library):
    if mode not in ('fuse', 'iov'):
        raise ValueError('unknown POSIX frontend')
    env = {k: v for k, v in environment.items()
           if k != 'LD_PRELOAD' and not k.startswith(('HF3FS_CXL_POSIX_', 'HF3FS_CXL_NATIVE_IOV'))}
    if mode == 'iov':
        env.update(LD_PRELOAD=str(library), HF3FS_CXL_NATIVE_IOV='1',
                   HF3FS_CXL_POSIX_MODE='iov', HF3FS_CXL_POSIX_REPORT='1',
                   HF3FS_CXL_POSIX_MOUNT=str(view))
    return env


def shadow_snapshot(mount):
    fd = os.open(mount, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        values = array.array('Q', [0, 0])
        fcntl.ioctl(fd, 0x80106816, values, True)
        return list(values)
    finally:
        os.close(fd)


def validate_counters(rows, pids):
    # IO500 popen helpers inherit LD_PRELOAD. Keep their zero-I/O reports as
    # auxiliary evidence, but never count them as MPI ranks or discard data I/O.
    expected = [row for row in rows if row['pid'] in pids]
    extra = [row for row in rows if row['pid'] not in pids]
    if (len({row['pid'] for row in rows}) != len(rows) or len(expected) != len(pids) or
            {row['pid'] for row in expected} != set(pids) or
            any(any(v != 0 for k, v in row.items() if k != 'pid') for row in extra)):
        raise ValueError('missing/duplicate/unexpected POSIX process counter')
    for row in expected:
        if any(row[k] != 0 for k in ('direct_read_bytes', 'direct_write_bytes', 'rejected_calls')):
            raise ValueError('unexpected direct allocation or rejected POSIX I/O')
        if not (row['staged_read_bytes'] == row['staging_copy_out_bytes'] > 0 and
                row['staged_write_bytes'] == row['staging_copy_in_bytes'] > 0):
            raise ValueError('FS staging copy accounting mismatch')
    return expected


def run_rank(args, environment, view, receipt, receipt_path, save):
    library = args.posix_library.resolve(strict=True)
    env = rank_environment(environment, args.posix_mode, view, library)
    evidence = dict(mode=args.posix_mode, library=str(library), library_sha256=sha(library),
        environment={k: v for k, v in env.items() if k.startswith('HF3FS_CXL_') or
                     k in ('LD_PRELOAD', 'LD_LIBRARY_PATH')},
        shadow_before=shadow_snapshot(view), placement=None)
    receipt['fs_frontend'] = evidence
    command = [str(args.io500), str(args.config), '--mode=extended']
    with subprocess.Popen(command, env=env) as process:
        evidence['pid'] = process.pid
        save(receipt_path, receipt)
        proc = Path('/proc') / str(process.pid)
        deadline = time.monotonic() + 3
        while process.poll() is None and time.monotonic() < deadline:
            try:
                exe = (proc / 'exe').resolve(strict=True)
                maps = (proc / 'maps').read_text()
                if exe == args.io500.resolve() and (args.posix_mode == 'fuse' or str(library) in maps):
                    evidence['placement'] = dict(executable=str(exe), phase='application-exec',
                        maps=maps, status=(proc / 'status').read_text(),
                        numa_maps=(proc / 'numa_maps').read_text(),
                        allowed_cpus=sorted(os.sched_getaffinity(process.pid)))
                    save(receipt_path, receipt)
                    break
            except (FileNotFoundError, ProcessLookupError):
                pass
            time.sleep(.01)
        code = process.wait()
    evidence.update(returncode=code, shadow_after=shadow_snapshot(view))
    evidence['shadow_delta'] = [a-b for a, b in zip(evidence['shadow_after'], evidence['shadow_before'])]
    save(receipt_path, receipt)
    if code == 0 and (evidence['placement'] is None or
                     evidence['placement']['allowed_cpus'] != receipt['allowed_cpus']):
        raise ValueError('missing or incorrect live application placement')
    return code
