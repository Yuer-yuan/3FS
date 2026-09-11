#!/usr/bin/env python3
"""Read-only final FS experiment audit, writing compact verification evidence."""
import argparse
import json
import os
from pathlib import Path
import shutil
import time

from posix_frontend import sha


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    state = json.loads((root / 'matrix.json').read_text())
    inputs = json.loads((root / 'inputs.json').read_text())
    if state['status'] != 'completed' or len(state['cases']) != 45:
        raise ValueError('matrix not completed')
    receipt = json.loads((root / 'receipts/scaling-controller.json').read_text())
    if receipt['status'] != 'passed':
        raise ValueError('controller supervisor has not completed successfully')
    if any(sha(p) != expected for p, expected in inputs['sha256'].items()):
        raise ValueError('pinned runtime/bench/deployment input changed')
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if boot != inputs['host']['boot_id']:
        raise ValueError('host rebooted')
    roots, owned_alive, reused_pids = [], [], []
    for row in state['qualification'] + state['cases']:
        if row['status'] != 'passed':
            raise ValueError('case not passed')
        bundle = Path(row['bundle'])
        if sha(bundle / 'result.json') != row['result_sha256']:
            raise ValueError('raw result modified')
        value = json.loads((bundle / 'result.json').read_text())
        if row['case']['system'] != 'legofs':
            for key in ('volatile', 'storage'):
                path = Path(value['runtime_roots'][key])
                if path.exists() or path.is_symlink():
                    raise ValueError('owned runtime remains: ' + str(path))
                roots.append(str(path))
            for path in (bundle / 'processes').glob('*.json'):
                process = json.loads(path.read_text())
                proc = Path('/proc') / str(process['pid'])
                try:
                    suffix = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
                    if int(suffix[19]) == process['start_ticks']:
                        owned_alive.append(process['pid'])
                    else:
                        reused_pids.append(process['pid'])
                except (FileNotFoundError, ProcessLookupError):
                    pass
    scratch = Path('/dev/shm/legofs-posix-io500-20260912')
    if scratch.exists() or scratch.is_symlink() or owned_alive:
        raise ValueError('owned filesystem runtime remains')
    mounts = Path('/proc/self/mountinfo').read_text()
    if any(path in mounts for path in roots + [str(scratch)]):
        raise ValueError('owned mount remains')
    value = dict(status='passed', time=time.time(), boot_id=boot, input_files=len(inputs['sha256']),
        all_pinned_inputs_unchanged=True, filesystem_payload_roots_absent=roots + [str(scratch)],
        owned_processes_alive=owned_alive, reused_unrelated_pids=reused_pids,
        completed_measurements=45, completed_qualifications=3,
        free_bytes={p: shutil.disk_usage(p).free for p in ('/', '/tmp', '/dev/shm')},
        raw_result_files_unchanged=True, audit_source_sha256=sha(__file__))
    (root / 'post-run-verification.json').write_text(json.dumps(value, indent=2) + '\n')
    shutil.copy2(__file__, root / 'executed-audit.py')
    print(json.dumps({k: v for k, v in value.items() if k != 'filesystem_payload_roots_absent'}))


if __name__ == '__main__':
    main()
