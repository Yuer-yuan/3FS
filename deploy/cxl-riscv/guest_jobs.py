"""Finite workload jobs that leave the guest control shell available."""
import re
import shlex
import time
import uuid


class GuestJobs:
    def __init__(self, consoles, command, records, default_timeout):
        self.consoles = consoles
        self.command = command
        self.records = records
        self.default_timeout = default_timeout

    def run(self, node, text, timeout=None):
        timeout = self.default_timeout if timeout is None else timeout
        start = time.monotonic()
        token = uuid.uuid4().hex
        variable = f'hf3fs_job_{token}'
        console = self.consoles[node]
        since = len(console.output)
        entry = dict(guest=node, token=token, command=text, host_start_ns=time.monotonic_ns(),
                     timeout_seconds=timeout, status='running', variable=variable, output_since=since)
        self.records.append(entry)
        # setsid gives the workload its own session; the shell's owned service
        # variables stay in the control shell. Only bounded probe/dd output is
        # sent here. General service logs use diagnostics.export_file instead.
        body = (f"printf '\\nHF3FS_JOB_%s token={token} pid=%s start=%s\\n' START $$ "
                '"$(/bin/busybox awk \'{print $22}\' /proc/$$/stat)"; '
                f"( {text} ); hf3fs_job_rc=$?; "
                f"printf '\\nHF3FS_JOB_%s token={token} rc=%s\\n' END $hf3fs_job_rc")
        try:
            self.command(node, f'/bin/busybox setsid /bin/sh -c {shlex.quote(body)} '
                         f'</dev/null & {variable}=$!', min(timeout, 30))
            remaining = lambda: max(0, timeout - (time.monotonic() - start))
            identity = console.wait_regex(re.compile(
                rf'(?m)^HF3FS_JOB_START token={token} pid=(\d+) start=(\d+)\r*\n'), remaining(), since=since)
            entry.update(pid=int(identity[1]), process_start=int(identity[2]))
            ended = console.wait_regex(re.compile(
                rf'(?m)^HF3FS_JOB_END token={token} rc=(\d+)\r*\n'), remaining(), since=since)
            output = console.output[identity.end():ended.start()]
            entry['returncode'] = int(ended[1])
            if entry['returncode']:
                raise RuntimeError(f'guest workload exited rc={ended[1]}: {output}')
            entry['status'] = 'passed'
            return output
        except Exception as error:
            entry.update(status='failed', error_type=type(error).__name__, error=str(error))
            raise
        finally:
            entry['host_end_ns'] = time.monotonic_ns()

    def _observe_late_completion(self, entry):
        """Reconcile an owned terminal marker without erasing a prior timeout."""
        if 'returncode' in entry or 'pid' not in entry:
            return
        output = self.consoles[entry['guest']].output[entry.get('output_since', 0):]
        token = re.escape(entry['token'])
        started = re.search(
            rf"(?m)^HF3FS_JOB_START token={token} pid={entry['pid']} start={entry['process_start']}\r*\n",
            output)
        if started is None:
            return
        ended = re.search(rf'(?m)^HF3FS_JOB_END token={token} rc=(\d+)\r*\n', output[started.end():])
        if ended is not None:
            entry.update(returncode=int(ended[1]), completion_observed_after_timeout=True,
                         completion_observed_host_ns=time.monotonic_ns())

    def stop(self, entry):
        """Cancel only a still-live session whose leader identity matches."""
        self._observe_late_completion(entry)
        if 'returncode' in entry:
            return
        if 'pid' not in entry:
            raise RuntimeError('workload session identity was not received; refusing an unverified kill')
        pid, started = entry['pid'], entry['process_start']
        # A live leader must match both session and start ticks. A missing
        # leader cannot be safely authorized from PID alone after reuse.
        text = (f'if [ -r /proc/{pid}/stat ]; then read -r line </proc/{pid}/stat; '
                'fields=${line##*) }; set -- $fields; '
                f'test "$4" = {pid} && {{ shift 19; test "$1" = {started}; }} && '
                f'/bin/busybox kill -TERM -{pid}; else false; fi')
        self.command(entry['guest'], text, 30)
        entry['cancel_requested'] = True

    def verify_stopped(self, entry):
        self._observe_late_completion(entry)
        if 'pid' not in entry or 'returncode' in entry:
            return
        pid = entry['pid']
        self.command(entry['guest'],
            'found=0; for stat in /proc/[0-9]*/stat; do '
            'read -r line 2>/dev/null <"$stat" || continue; '
            'fields=${line##*) }; set -- $fields; '
            'case "$1" in Z|X) continue;; esac; '
            f'[ "$4" != {pid} ] || found=1; done; test "$found" -eq 0', 30)
        entry['stopped_verified'] = True
