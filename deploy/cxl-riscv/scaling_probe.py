"""Diagnostic 1/2/... client runs with an in-place FDB CPU0 / CPU0-1 / CPU0 comparison."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time
import uuid

import diagnostics
import full_stack
import run_g0


def affinity_command(mask: str) -> str:
    if mask not in ('1', '3'):
        raise ValueError('only CPU0 and CPU0-1 are diagnostic choices')
    expected = "0" if mask == "1" else "0-1"
    return ("( p=$(cat /run/fdbserver.pid); [ -d /proc/$p/task ] && "
            "{ for t in /proc/$p/task/*; do "
            f"/bin/busybox taskset -p {mask} ${{t##*/}} || exit 1; "
            f"test \"$(/bin/busybox awk '/^Cpus_allowed_list:/{{print $2}}' $t/status)\" = {expected} || exit 1; "
            "done; } )")


def io_command(path: str, cells: int) -> str:
    if cells not in (1, 32):
        raise ValueError('diagnostic I/O is one cell or the original 2MiB')
    mark = "mark() { read up rest </proc/uptime; printf 'HF3FS_SCALE_STAGE name=%s uptime=%s\\n' $1 $up; }; "
    # Sample the actual dd output descriptor. Advancing positions distinguish
    # slow throughput from a request stuck at its first write, without signals.
    mark += ("run_dd() { dd \"$@\" & dp=$!; while kill -0 $dp 2>/dev/null; do "
             "read up rest </proc/uptime; target=$(/bin/busybox readlink /proc/$dp/fd/1 2>/dev/null); "
             "if [ -r /proc/$dp/fdinfo/1 ]; then "
             "while read key value rest; do case $key in pos:) "
             "printf 'HF3FS_SCALE_PROGRESS uptime=%s pid=%s pos=%s target=%s\\n' $up $dp $value \"$target\";; esac; "
             "done </proc/$dp/fdinfo/1; fi; sleep 1; done; wait $dp; }; ")
    if cells == 32:
        # Preserve the original 2MiB write+fsync invocation for comparison.
        return (mark + f'mark write_fsync_close_begin && run_dd if=/dev/zero of={path} bs=65536 count=32 oflag=direct conv=fsync && '
                f'mark write_fsync_close_end && mark read_close_begin && run_dd if={path} of=/var/lib/3fs/scale-output bs=65536 iflag=direct && '
                'mark read_close_end && test $(stat -c %s /var/lib/3fs/scale-output) -eq 2097152 && '
                'dd if=/dev/zero of=/var/lib/3fs/scale-expected bs=65536 count=32 && '
                'cmp /var/lib/3fs/scale-expected /var/lib/3fs/scale-output && '
                f'rm {path} && printf "HF3FS_SCALE_IO_OK\\n"')
    return (mark + f'mark write_close_begin && run_dd if=/dev/zero of={path} bs=65536 count={cells} oflag=direct && '
            f'mark write_close_end && mark fsync_close_begin && run_dd if=/dev/null of={path} conv=notrunc,fsync && '
            f'mark fsync_close_end && mark read_close_begin && run_dd if={path} of=/var/lib/3fs/scale-output bs=65536 iflag=direct && '
            f'mark read_close_end && test $(stat -c %s /var/lib/3fs/scale-output) -eq {cells * 65536} && '
            f'dd if=/dev/zero of=/var/lib/3fs/scale-expected bs=65536 count={cells} && '
            'cmp /var/lib/3fs/scale-expected /var/lib/3fs/scale-output && '
            f'rm {path} && printf "HF3FS_SCALE_IO_OK\\n"')


def execute(consoles, command, run: Path, *, first_mask: str = '1') -> dict:
    affinity_command(first_mask)  # Validate before touching the cohort.
    clients = len(consoles) - 1
    record = dict(status='failed', result_class='diagnostic', acceptance_evidence=False,
                  client_count=clients, rounds=[], restoration_errors=[],
                  first_failure='diagnostic CPU comparison; not qualification evidence')
    token = uuid.uuid4().hex

    def probe(node, name, text, label):
        # A regular file separates producer data from the interactive prompt and
        # other serial commands. Export verifies length/hash after the job ends.
        remote = f'/var/log/3fs/scale-{token}-{label}-{node}-{name}.log'
        start = time.monotonic_ns()
        item = dict(guest=node, operation=name, host_start_ns=start)
        try:
            command.workload(node, f'( {text} ) >{remote} 2>&1', 120)
            item['host_business_end_ns'] = time.monotonic_ns()
            target = run / Path(remote).name
            receipt = diagnostics.export_file(command.diagnostic, node, remote, target, max_bytes=65536)
            output = target.read_text()
            if name == 'fdb':
                item['fdb'] = run_g0._require_probe_record(output, 'isolated local FDB transaction')
            elif name in ('io', 'io-cell'):
                if output.count('HF3FS_SCALE_IO_OK') != 1:
                    raise ValueError('write/read comparison marker missing')
            elif f'HF3FS_DIAGNOSTIC_IO action={name} errno=0' not in output:
                raise ValueError('syscall success marker missing')
            item.update(status='passed', export=receipt)
        except Exception as error:
            item.update(status='failed', error=str(error))
            # A bounded failed-producer snapshot is diagnostic evidence only;
            # retain collection errors without replacing the original failure.
            try:
                target = run / Path(remote).name
                item['failed_output'] = diagnostics.export_file(command.diagnostic, node, remote, target, max_bytes=65536)
                item['failed_output']['producer_completion_verified'] = False
            except Exception as collection_error:
                item['collection_error'] = str(collection_error)
        item['host_end_ns'] = time.monotonic_ns()
        return item

    try:
        rounds = (('one-core-before', '1'), ('two-cores', '3'), ('one-core-after', '1')) if first_mask == '1' else (
            ('two-cores-before', '3'), ('one-core', '1'), ('two-cores-after', '3'))
        for label, mask in rounds:
            entry = dict(label=label, allowed_cpus='0' if mask == '1' else '0-1', operations=[])
            record['rounds'].append(entry)
            entry['affinity'] = command(0, affinity_command(mask), 30)
            # Progress through layers. Stop at the first failing stage rather
            # than repeating the same long timeout at the next workload size.
            for action in ('stat-existing', 'create-fsync-stat-unlink', 'io-cell', 'io'):
                def client(node):
                    path = f'/mnt/3fs/test/scale-{token}-{label}-{node}'
                    if action in ('io', 'io-cell'):
                        text = io_command(path, 1 if action == 'io-cell' else 32)
                    else:
                        target = '/mnt/3fs/test' if action == 'stat-existing' else path
                        text = f'{full_stack.BIN}/fuse_mount_smoke --diagnostic {action} {target}'
                    return probe(node, action, text, label)
                with ThreadPoolExecutor(max_workers=clients + 1) as pool:
                    fdb = pool.submit(probe, 0, 'fdb',
                        f'{full_stack.BIN}/fdb_client_smoke --cluster-file /opt/3fs/etc/fdb.cluster '
                        f'--key scale-{token}-{label}-{action} --value local-fdb', label + '-' + action)
                    results = list(pool.map(client, range(1, clients + 1))) + [fdb.result()]
                entry['operations'].extend(results)
                failures = [r for r in results if r['status'] != 'passed']
                if failures:
                    record['first_failure'] = f"{label}/{action}: {failures[0]['error']}"
                    return record
            entry['completed'] = True
        record['comparison_completed'] = True
    except Exception as error:
        record['first_failure'] = str(error)
    finally:
        try:
            record['restored_affinity'] = command.diagnostic(0, affinity_command('1'), 30)
        except Exception as error:
            record['restoration_errors'].append(str(error))
        (run / 'scaling-probe.json').write_text(json.dumps(record, indent=2) + '\n')
    return record
