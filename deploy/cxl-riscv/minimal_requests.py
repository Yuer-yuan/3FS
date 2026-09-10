"""Named diagnostic cohort on ten live FUSE clients; never acceptance evidence."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import time
import uuid
import full_stack


def execute(consoles, command, run: Path) -> dict:
    record = dict(status='failed', result_class='diagnostic', acceptance_evidence=False, operations=[])
    token = uuid.uuid4().hex
    try:
        for sequence, action in enumerate(('testRpc', 'stat-existing', 'stat-missing', 'create-fsync-stat-unlink'), 1):
            groups = ([1], list(range(1, 11))) if sequence <= 3 else (list(range(1, 11)),)
            for group_index, nodes in enumerate(groups):
                def probe(node):
                    start = time.monotonic_ns()
                    entry = dict(guest=node, action=action, concurrency=len(nodes), host_start_ns=start)
                    try:
                        if action == 'testRpc':
                            identity = sequence * 10 + group_index
                            text = (f"printf '{identity}\\n' >/run/hf3fs-diagnostic/testRpc.command.tmp && "
                                    "/bin/busybox mv /run/hf3fs-diagnostic/testRpc.command.tmp /run/hf3fs-diagnostic/testRpc.command && "
                                    f"{{ while ! grep -q 'sequence={identity} ' /run/hf3fs-diagnostic/testRpc.result 2>/dev/null; do "
                                    "sleep 0.1; done; cat /run/hf3fs-diagnostic/testRpc.result; }")
                            output = getattr(command, "workload", command)(node, text, 600)
                            if not re.search(rf'(?m)^HF3FS_DIAGNOSTIC_TEST_RPC sequence={identity} status=0\r*$', output):
                                raise ValueError('testRpc status missing or unsuccessful')
                        else:
                            path = '/mnt/3fs/test' if action == 'stat-existing' else f'/mnt/3fs/test/diagnostic-{token}-{node}-{group_index}-{sequence}'
                            output = getattr(command, "workload", command)(node, f'{full_stack.BIN}/fuse_mount_smoke --diagnostic {action} {path}', 600)
                            if not re.search(rf'(?m)^HF3FS_DIAGNOSTIC_IO action={action} errno=0\r*$', output):
                                raise ValueError('diagnostic syscall result missing')
                        entry.update(status='passed', output=output)
                    except Exception as error:
                        entry.update(status='failed', error=str(error))
                    entry['host_end_ns'] = time.monotonic_ns()
                    return entry
                with ThreadPoolExecutor(max_workers=len(nodes)) as pool:
                    results = list(pool.map(probe, nodes))
                record['operations'].extend(results)
                failures = [r for r in results if r['status'] != 'passed']
                if failures:
                    raise RuntimeError(f"{action}: {failures[0]['error']}")
        record['clients'] = full_stack.concurrent_io(consoles, command, 10)
        record['status'] = 'passed'
    except Exception as error:
        record['first_failure'] = str(error)
    (run / 'minimal-requests.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def execute_and_retire(consoles, command, run: Path) -> dict:
    """Exercise failure cleanup only after the complete finite I/O proof."""
    result = execute(consoles, command, run)
    if result['status'] != 'passed':
        return result
    injection = dict(result_class='diagnostic', acceptance_evidence=False,
                     business_passed=True, injected=True, host_monotonic_ns=time.monotonic_ns())
    command(0, "printf 'HF3FS_DIAGNOSTIC_%s\\n' FAILURE_INJECTED", 30)
    (run / 'retirement-probe.json').write_text(json.dumps(injection, indent=2) + '\n')
    return {**result, 'status': 'failed', 'failure_injection': injection,
            'first_failure': 'intentional diagnostic failure after successful finite I/O'}
