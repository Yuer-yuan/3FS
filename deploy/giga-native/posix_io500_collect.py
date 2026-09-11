#!/usr/bin/env python3
"""Collect only this experiment's compact evidence from giga; never run I/O."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--inventory', action='store_true', help='final remote/local per-file hash replay')
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    remote = '/root/cxlmemsim-riscv-io500/target/results'
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20', '-o', 'ServerAliveInterval=15',
           '-o', 'ServerAliveCountMax=2', '-o', 'ControlMaster=auto', '-o', 'ControlPersist=7200',
           '-o', 'ControlPath=/tmp/hf3fs-posix-cxl-20260912-%C',
           '-o', 'ProxyCommand=nc -X connect -x 127.0.0.1:10808 %h %p']
    rsync = ['rsync', '-a', '-e', shlex.join(ssh)]
    subprocess.run([*rsync, '--exclude=/legofs/', '--exclude=*.tmp',
                    'giga:' + remote + '/posix-cxl-io500-scaling-20260912/',
                    str(out / 'cohort') + '/'], check=True, timeout=180)
    state = json.loads((out / 'cohort/matrix.json').read_text())
    completed = [r for r in state['cases'] if r['status'] == 'passed']
    three = [r for r in state['qualification'] + completed if r['status'] == 'passed' and
             r['case']['system'] != 'legofs']
    names = [Path(r['bundle']).name for r in three]
    filters = [option for name in names for option in
               ('--include=/' + name + '/', '--include=/' + name + '/***')]
    subprocess.run([*rsync, *filters, '--exclude=*',
                    'giga:' + remote + '/giga-native-3fs/', str(out / 'threefs') + '/'],
                   check=True, timeout=180)
    names = [r['case']['name'] for r in completed if r['case']['system'] == 'legofs']
    if names:
        filters = [option for name in names for option in
                   ('--include=/' + name + '/', '--include=/' + name + '/***')]
        subprocess.run([*rsync, *filters, '--exclude=*',
                        'giga:' + remote + '/posix-cxl-io500-scaling-20260912/legofs/',
                        str(out / 'cohort/legofs') + '/'], check=True, timeout=180)
    print(json.dumps(dict(status=state['status'], completed=len(completed),
        current=state['cases'][-1]['case']['name'] if state['cases'] else None)), flush=True)
    if args.inventory:
        if state['status'] != 'completed' or len(completed) != 45:
            raise ValueError('final inventory requires all45 completed')
        script = '''import hashlib,json,pathlib
base=pathlib.Path('/root/cxlmemsim-riscv-io500/target/results')
roots=[('cohort',base/'posix-cxl-io500-scaling-20260912')]
roots += [('threefs/'+p.name,p) for p in sorted((base/'giga-native-3fs').glob('posix-io500-20260912-*')) if p.is_dir()]
result={}
for label,root in roots:
 for path in sorted(root.rglob('*')):
  if path.is_file():
   with path.open('rb') as f: digest=hashlib.file_digest(f,'sha256').hexdigest()
   result[label+'/'+str(path.relative_to(root))]={'sha256':digest,'bytes':path.stat().st_size}
print(json.dumps(result))
'''
        result = subprocess.run([*ssh, 'giga', shlex.join(['python3', '-c', script])],
                                capture_output=True, text=True, check=True, timeout=180)
        inventory = json.loads(result.stdout)
        for relative, record in inventory.items():
            path = out / relative
            with path.open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != record['sha256'] or path.stat().st_size != record['bytes']:
                raise ValueError('remote/local archive mismatch: ' + relative)
        (out / 'raw-inventory.json').write_text(json.dumps(inventory, indent=2) + '\n')
        print(json.dumps(dict(inventory_files=len(inventory),
                              inventory_bytes=sum(r['bytes'] for r in inventory.values()))))


if __name__ == '__main__':
    main()
