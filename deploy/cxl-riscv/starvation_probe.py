"""Short, explicitly perturbing diagnosis; never qualification evidence.

Keep all VMs and connections. Compare an independent local FDB transaction with
CXL-backed stat requests while nine FUSE processes briefly stop and resume.
The remaining client (6), FDB, services and guest clocks continue running.
"""
from concurrent.futures import ThreadPoolExecutor, wait
import hashlib
import json
import re
from pathlib import Path
import time
import uuid

import full_stack
import run_g0

CLIENTS_TO_PAUSE = tuple(n for n in range(1, 11) if n != 6)
# These bound this diagnostic observation, not any application/RPC timeout.
OBSERVATION_SECONDS = 120
BASELINE_SECONDS = 20
PAUSED_SECONDS = 10


def pause_command() -> str:
    return ("sp=$hf3fs_pid_hf3fs_fuse_main; "
            "ss=$(/bin/busybox awk '{print $22}' /proc/$sp/stat); "
            "[ -n \"$ss\" ] && kill -0 $sp && "
            "{ (sleep 30; ns=$(/bin/busybox awk '{print $22}' /proc/$sp/stat); "
            "[ \"$ns\" = \"$ss\" ] && kill -CONT $sp; "
            "rc=$?; read up rest </proc/uptime; "
            "printf 'HF3FS_STARVE_AUTO_RESUME pid=%s rc=%s uptime=%s\\n' $sp $rc $up) </dev/null & "
            "kill -STOP $sp && printf 'HF3FS_STARVE_STOP pid=%s start=%s\\n' $sp $ss; }")


def restore_command() -> str:
    return ("ns=$(/bin/busybox awk '{print $22}' /proc/$sp/stat); "
            "[ -n \"$ss\" ] && [ \"$ns\" = \"$ss\" ] && kill -CONT $sp && "
            "/bin/busybox awk '/^State:/{if ($2 == \"T\" || $2 == \"t\") exit 1; print}' /proc/$sp/status")


def scheduler_command() -> str:
    # Shell builtins avoid spawning a dozen slow helpers per sample. Individual
    # process counters have their own guest monotonic anchors, not a fake atomic
    # snapshot or a host/guest time subtraction.
    return ("p=$(cat /run/fdbserver.pid); for t in /proc/$p/task/*; do "
            "read up rest </proc/uptime; read name <$t/comm; "
            "if read runtime queued slices <$t/schedstat; then "
            "printf 'HF3FS_STARVE_THREAD uptime=%s tid=%s name=%s runtime_ns=%s queued_ns=%s slices=%s\\n' "
            "$up ${t##*/} \"$name\" $runtime $queued $slices; else exit 1; fi; done")


def execute(consoles, command, run: Path) -> dict:
    record = dict(status='failed', result_class='diagnostic', acceptance_evidence=False,
                  operations=[], samples=[], intervention_errors=[],
                  observation_seconds=OBSERVATION_SECONDS,
                  first_failure='intentional diagnostic intervention; not acceptance evidence')
    token = uuid.uuid4().hex
    operations = []
    attempted = []

    def probe(node, kind):
        entry = dict(guest=node, kind=kind, host_start_ns=time.monotonic_ns())
        try:
            if kind == 'local-fdb':
                text = (f'{full_stack.BIN}/fdb_client_smoke --cluster-file /opt/3fs/etc/fdb.cluster '
                        f'--key starvation-{token} --value isolated-local-fdb')
            else:
                text = f'{full_stack.BIN}/fuse_mount_smoke --diagnostic stat-existing /mnt/3fs/test'
            output = command.workload(node, text, OBSERVATION_SECONDS)
            entry['raw_output'] = output
            if kind == 'local-fdb':
                output = re.sub(r'(?m)^hf3fs-g0-0# (?=\{)', '', output)
                entry['fdb'] = run_g0._require_probe_record(output, 'local FDB under CXL activity')
            elif 'HF3FS_DIAGNOSTIC_IO action=stat-existing errno=0' not in output:
                raise ValueError('stat success marker missing')
            entry.update(status='passed', output=output)
        except Exception as error:
            entry.update(status='failed', error=str(error))
        entry['host_end_ns'] = time.monotonic_ns()
        return entry

    def sample(label):
        entry = dict(label=label, guest=0, host_start_ns=time.monotonic_ns())
        try:
            output = command.diagnostic(0, scheduler_command(), 30)
            if not any(line.startswith('HF3FS_STARVE_THREAD ') for line in output.splitlines()):
                raise ValueError('scheduler producer returned no records')
            path = run / f'starvation-{label}.log'
            path.write_text(output)
            entry.update(path=str(path), bytes=path.stat().st_size,
                         sha256=hashlib.sha256(path.read_bytes()).hexdigest(), status='passed')
        except Exception as error:
            entry.update(status='failed', error=str(error))
        entry['host_end_ns'] = time.monotonic_ns()
        record['samples'].append(entry)

    def intervention(node, restoring=False):
        start = time.monotonic_ns()
        if not restoring:
            attempted.append(node)  # Restore even if STOP succeeded but its ACK was lost.
        try:
            output = command(node, restore_command() if restoring else pause_command(), 15)
            return dict(guest=node, restoring=restoring, output=output, status='passed',
                        host_start_ns=start, host_end_ns=time.monotonic_ns())
        except Exception as error:
            return dict(guest=node, restoring=restoring, status='failed', error=str(error),
                        host_start_ns=start, host_end_ns=time.monotonic_ns())

    with ThreadPoolExecutor(max_workers=11) as probes:
        operations = [probes.submit(probe, 0, 'local-fdb')]
        operations += [probes.submit(probe, n, 'cxl-stat') for n in range(1, 11)]
        try:
            wait(operations, timeout=BASELINE_SECONDS)
            sample('before')
            record['pending_before_pause'] = [i for i, f in enumerate(operations) if not f.done()]
            if record['pending_before_pause']:
                with ThreadPoolExecutor(max_workers=9) as controls:
                    changes = list(controls.map(intervention, CLIENTS_TO_PAUSE))
                record['pause'] = changes
                if any(c['status'] != 'passed' for c in changes):
                    raise RuntimeError('partial pause; restore all attempted participants')
                # Independent guest watchdogs cap STOP even on a host exception.
                time.sleep(PAUSED_SECONDS)
                # /proc/status begins with Name, so inspect its explicit State field.
                state = "/bin/busybox awk '/^State:/{print; if ($2 != \"T\") exit 1}' /proc/$hf3fs_pid_hf3fs_fuse_main/status"
                with ThreadPoolExecutor(max_workers=9) as controls:
                    list(controls.map(lambda n: command(n, state, 10), CLIENTS_TO_PAUSE))
                sample('during')
                with ThreadPoolExecutor(max_workers=9) as controls:
                    list(controls.map(lambda n: command(n, state, 10), CLIENTS_TO_PAUSE))
                record['pause_window_verified'] = True
            else:
                record['intervention_skipped'] = 'all requests completed before baseline ended; no stall reproduced'
        except Exception as error:
            record['intervention_errors'].append(str(error))
        finally:
            with ThreadPoolExecutor(max_workers=9) as controls:
                restored = list(controls.map(lambda n: intervention(n, True), sorted(attempted)))
            record['restore'] = restored
            record['intervention_errors'].extend(c['error'] for c in restored if c['status'] != 'passed')
            sample('after')
        record['operations'] = [f.result() for f in operations]
    # Real probe failure has precedence over the intentional diagnostic status.
    failures = [p for p in record['operations'] if p['status'] != 'passed']
    if failures:
        record['first_failure'] = f"{failures[0]['kind']}: {failures[0]['error']}"
    (run / 'starvation-probe.json').write_text(json.dumps(record, indent=2) + '\n')
    return record
