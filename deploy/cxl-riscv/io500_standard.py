"""Standard IO500 workload, timing evidence and independent result validation."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import configparser
import hashlib
import json
import math
from pathlib import Path
import re
import shlex
import time

import full_stack
import prepare_io500

SCHEMA = 'hf3fs.cxl-io500-standard-10c1s.v1'
PHASES = ('ior-easy-write', 'mdtest-easy-write', 'ior-hard-write', 'mdtest-hard-write', 'find',
          'ior-easy-read', 'mdtest-easy-stat', 'ior-hard-read', 'mdtest-hard-stat',
          'mdtest-easy-delete', 'mdtest-hard-read', 'mdtest-hard-delete', 'ior-rnd4K-easy-read')
ENV = 'LD_PRELOAD= LD_LIBRARY_PATH=/opt/io500/lib'
BIN = '/opt/io500/bin'
RESULT_DIR = '/mnt/3fs/io500-standard-results'
BANNER_TIMEOUT_SECONDS = 300
CLOCK_RE = re.compile(r'(?m)^HF3FS_IO500_CLOCK host=(\S+) realtime_ns=(\d+) monotonic_before_ns=(\d+) monotonic_after_ns=(\d+)\s*$')
RANK_RE = re.compile(r'HF3FS_IO500_RANK rank=(\d+) guest=(\d+) host=([^\s]+) pid=(\d+) endpoint=(\d+)')
PROBE_RE = re.compile(r'HF3FS_IO500_PROBE rank=(\d+) size=(\d+) host=([^\s]+) pid=(\d+) begin_ns=(\d+) end_ns=(\d+) monotonic_begin_ns=(\d+) monotonic_end_ns=(\d+)')
HEARTBEAT_RE = re.compile(
    r'HF3FS_IO500_HEARTBEAT rank=(\d+) pid=(\d+) elapsed_ms=(\d+) output_bytes=(\d+) '
    r'state=(\S+) wchan=(\S+) utime_ticks=(\d+) stime_ticks=(\d+) voluntary_ctxt=(\d+) '
    r'nonvoluntary_ctxt=(\d+) rchar=(\d+) wchar=(\d+) syscr=(\d+) syscw=(\d+) '
    r'read_bytes=(\d+) write_bytes=(\d+)')


def clean_console(text: str) -> str:
    # Background Hydra/rank output can start immediately after an idle shell
    # prompt. Remove only this runner's known prompt, never arbitrary prefixes.
    return re.sub(r'(?m)^hf3fs-g0-(?:[0-9]|10)# ', '', text)


def stage_roots(roots: list[Path]) -> dict:
    if len(roots) != 11:
        raise ValueError('standard requires eleven guest roots')
    hosts = '127.0.0.1 localhost\n10.73.0.2 server0\n' + ''.join(
        f'10.73.0.{n + 3} client{n}\n' for n in range(10))
    files = {}
    for node, root in enumerate(roots):
        (root / 'etc').mkdir(exist_ok=True)
        (root / 'etc/hosts').write_text(hosts)
        directory = root / 'opt/io500/etc'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'guest-id').write_text(str(node) + '\n')
        (directory / 'clients').write_text(''.join(f'10.73.0.{n + 2}:1\n' for n in range(1, 11)))
        files[node] = {relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
                       for relative in ('etc/hosts', 'opt/io500/etc/guest-id', 'opt/io500/etc/clients')}
    return dict(hosts=hosts, guest_files=files)


def clocks(consoles: list, *, synchronize: bool = False) -> list[dict]:
    def probe(node: int) -> dict:
        # Clock changes are only called before FDB and the 3FS services start.
        epoch = int(time.time())
        action = f'set-clock {epoch}' if synchronize else 'clock'
        name = 'server0' if node == 0 else f'client{node - 1}'
        prefix = f'hostname {name} && ' if synchronize else ''
        before = time.monotonic_ns()
        host_realtime_before = time.time_ns()
        output = consoles[node].shell_command(f'{prefix}{ENV} {BIN}/io500-probe {action}', 60)
        host_realtime_after = time.time_ns()
        after = time.monotonic_ns()
        matches = list(CLOCK_RE.finditer(output))
        if len(matches) != 1 or matches[0][1] != name:
            raise ValueError('missing or wrong guest clock receipt')
        match = matches[0]
        return dict(guest=node, host=name, realtime_ns=int(match[2]), monotonic_before_ns=int(match[3]),
                    monotonic_after_ns=int(match[4]), host_send_ns=before, host_receive_ns=after,
                    host_realtime_before_ns=host_realtime_before, host_realtime_after_ns=host_realtime_after,
                    set_epoch=epoch if synchronize else None)
    with ThreadPoolExecutor(max_workers=11) as pool:
        return list(pool.map(probe, range(11)))


def clock_validation(before: list[dict], after: list[dict]) -> list[str]:
    errors = []
    if len(before) != 11 or len(after) != 11 or any(
        {r.get('guest') for r in records} != set(range(11)) for records in (before, after)
    ):
        return ['clock evidence does not cover eleven guests']
    for a, b in zip(sorted(before, key=lambda r: r['guest']), sorted(after, key=lambda r: r['guest'])):
        try:
            for r in (a, b):
                if (r['monotonic_before_ns'] > r['monotonic_after_ns'] or
                    r['host_receive_ns'] < r['host_send_ns'] or r.get('set_epoch') is not None):
                    raise ValueError('invalid read-only clock sample')
            guest_elapsed = b['monotonic_before_ns'] - a['monotonic_after_ns']
            wall_elapsed = b['realtime_ns'] - a['realtime_ns']
            host_min = b['host_send_ns'] - a['host_receive_ns']
            host_max = b['host_receive_ns'] - a['host_send_ns']
            sampling = sum(r['monotonic_after_ns'] - r['monotonic_before_ns'] for r in (a, b))
            # A coarse 1-second tolerance includes console/sample scheduling;
            # actual samples and tighter observable bounds remain in the record.
            if not (guest_elapsed > 0 and host_min - 1e9 <= guest_elapsed <= host_max + 1e9 and
                    abs(wall_elapsed - guest_elapsed) <= sampling + 1e9):
                raise ValueError('guest clocks or host elapsed window disagree')
        except (KeyError, TypeError, ValueError) as error:
            errors.append(f'clock guest {a.get("guest")}: {error}')
    return errors


def parse_proxy_commands(text: str) -> list[list[str]]:
    matches = re.findall(r'(?m)^HYDRA_LAUNCH: ([^\r\n]+)', clean_console(text))
    by_id = {}
    for text in matches:
        args = shlex.split(text)
        if not args or args[0] != BIN + '/hydra_pmi_proxy' or args.count('--proxy-id') != 1:
            raise ValueError('unexpected Hydra proxy executable or identity')
        try:
            index = int(args[args.index('--proxy-id') + 1])
        except (ValueError, IndexError) as error:
            raise ValueError('invalid Hydra proxy ID') from error
        if index in by_id:
            raise ValueError('duplicate Hydra proxy ID')
        by_id[index] = args
    if set(by_id) != set(range(10)):
        raise ValueError('Hydra did not emit exactly ten independent proxies')
    return [by_id[n] for n in range(10)]


class PhaseWatchdog:
    pattern = re.compile(r'\b(' + '|'.join(re.escape(p) for p in PHASES) +
                         r')\s+\S+\s+(?:GiB/s|kIOPS)\s*:\s*time\s+(\S+)\s+seconds')
    fatal = re.compile(r'\bWARNING: (?:read|write)\([^\r\n]*\) failed\b|\bAssertion failed:')

    def __init__(self):
        self.partial = ''
        self.window = None
        self.started_ns = None
        self.banner_ns = None
        self.records = []
        self.failure = None
        self.heartbeats = []
        self.rank_receipts = []
        self._rank_receipts = set()
        self.last_progress_ns = None

    @property
    def current_phase(self) -> str | None:
        return PHASES[len(self.records)] if len(self.records) < len(PHASES) else None

    def observe(self, chunk: str, now_ns: int) -> None:
        if self.started_ns is None:
            self.started_ns = now_ns
        lines = (self.partial + chunk).split('\n')
        self.partial = lines.pop()
        for line in lines:
            heartbeat = HEARTBEAT_RE.search(line)
            if heartbeat:
                names = ('rank', 'pid', 'elapsed_ms', 'output_bytes', 'state', 'wchan',
                         'utime_ticks', 'stime_ticks', 'voluntary_ctxt', 'nonvoluntary_ctxt',
                         'rchar', 'wchar', 'syscr', 'syscw', 'read_bytes', 'write_bytes')
                values = heartbeat.groups()
                sample = {name: value if name in ('state', 'wchan') else int(value)
                          for name, value in zip(names, values)}
                sample.update(observed_ns=now_ns, phase=self.current_phase)
                self.heartbeats.append(sample)
                self.last_progress_ns = now_ns
            rank = RANK_RE.search(line)
            if rank and int(rank[1]) not in self._rank_receipts:
                self._rank_receipts.add(int(rank[1]))
                self.rank_receipts.append(dict(rank=int(rank[1]), guest=int(rank[2]), host=rank[3],
                                               pid=int(rank[4]), endpoint=int(rank[5]),
                                               observed_ns=now_ns))
                self.last_progress_ns = now_ns
            if self.fatal.search(line):
                self.failure = dict(line=line.strip(), observed_ns=now_ns,
                                    phase=self.current_phase,
                                    completed_phases=len(self.records))
                raise RuntimeError('fatal IO500 output: ' + line.strip())
            if '[INVALID]' in line:
                raise ValueError('IO500 reported INVALID: ' + line.strip())
            if 'IO500 version ' in line and self.window is None:
                self.window = self.banner_ns = now_ns
                self.last_progress_ns = now_ns
            match = self.pattern.search(line)
            if not match:
                continue
            name, seconds = match[1], float(match[2])
            if self.window is None:
                raise ValueError('phase result preceded the IO500 banner')
            if len(self.records) >= len(PHASES) or name != PHASES[len(self.records)]:
                raise ValueError('duplicate or out-of-order phase: ' + name)
            host_seconds = (now_ns - self.window) / 1e9
            self.records.append(dict(name=name, seconds=seconds, observed_ns=now_ns, host_window_seconds=host_seconds))
            self.last_progress_ns = now_ns
            if not math.isfinite(seconds) or not 0 < seconds <= 600 or not 0 <= host_seconds <= 600:
                raise TimeoutError(f'IO500 phase {name} exceeded the standard time gate: reported={seconds}, host={host_seconds}')
            self.window = now_ns
        if self.window is None and now_ns - self.started_ns > BANNER_TIMEOUT_SECONDS * 1e9:
            raise TimeoutError(f'IO500 banner was not observed within {BANNER_TIMEOUT_SECONDS} seconds')
        if self.window is not None and len(self.records) < 13 and now_ns - self.window > 600e9:
            raise TimeoutError(f'IO500 phase window exceeded 600 seconds after {len(self.records)} completed phases')


def capture_live_diagnostics(consoles: list, command, run: Path, phase: str) -> dict:
    """Capture process and service state on every live guest before MPI cleanup."""
    started = time.monotonic_ns()
    snapshots = []

    def capture(node: int) -> dict:
        path = run / f'io500-live-{phase}-node{node}.log'
        node_started = time.monotonic_ns()
        sections = []
        chunks = []
        for name, diagnostic in full_stack.live_diagnostic_commands(node, phase):
            section_started = time.monotonic_ns()
            section = dict(name=name, command_bytes=len(diagnostic.encode()),
                           host_start_ns=section_started, outcome='running')
            captured = full_stack.diagnostics.capture(
                getattr(command, 'diagnostic', command), node, diagnostic,
                run / f'io500-live-{phase}-node{node}-{name}.log', timeout=60)
            section.update(captured)
            if captured['outcome'] == 'passed':
                output = Path(captured['path']).read_text(errors='replace')
                chunks.append(f'\nHF3FS_LIVE_SECTION {name}\n{output}')
            else:
                chunks.append(f"\nHF3FS_LIVE_SECTION {name}\n{captured['error']}\n")
                sections.append(section)
                break
            sections.append(section)
        path.write_text(''.join(chunks))
        ended = time.monotonic_ns()
        passed = len(sections) == len(full_stack.live_diagnostic_commands(node, phase)) and all(
            section['outcome'] == 'passed' for section in sections)
        snapshot = dict(guest=node, path=str(path), outcome='passed' if passed else 'failed',
                        bytes=sum(section.get('bytes', 0) for section in sections), sections=sections,
                        host_start_ns=node_started, host_end_ns=ended)
        if not passed and sections:
            snapshot.update(error_type=sections[-1].get('error_type'), error=sections[-1].get('error'))
        return snapshot

    with ThreadPoolExecutor(max_workers=len(consoles)) as pool:
        snapshots.extend(pool.map(capture, range(len(consoles))))
    ended = time.monotonic_ns()
    for snapshot in snapshots:
        snapshot['duration_ms'] = (snapshot['host_end_ns'] - snapshot['host_start_ns']) / 1e6
    return dict(host_start_ns=started, host_end_ns=ended, duration_ms=(ended - started) / 1e6,
                snapshots=snapshots)


def parse_metrics(text: str) -> dict:
    if '[INVALID]' in text:
        raise ValueError('INVALID in official result')
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read_string(text)
    run = parser['run']
    if (run.getint('procs') != 10 or run.get('mode') != 'standard' or
        run.get('result-dir') != RESULT_DIR or not run.get('version', '').endswith('a69cf60cf765')):
        raise ValueError('official result has a different rank count, profile, path or version')
    phases = []
    for section in parser.sections():
        values = parser[section]
        if section in PHASES:
            seconds, score = float(values['t_delta']), float(values['score'])
            if not math.isfinite(seconds) or not 0 < seconds <= 600 or not math.isfinite(score) or score <= 0:
                raise ValueError('invalid phase duration or score: ' + section)
            phases.append(dict(name=section, seconds=seconds, score=score,
                               unit='GiB/s' if section.startswith('ior-') else 'kIOPS',
                               t_start=values['t_start'], t_end=values['t_end']))
        elif section not in ('SCORE', 'SCOREX', 'timestamp') and 'score' in values:
            raise ValueError('unexpected scored phase: ' + section)
    if len(phases) != 13 or {p['name'] for p in phases} != set(PHASES):
        raise ValueError('official result does not contain all thirteen phases')
    score = parser['SCORE']
    official = {name: float(score[key]) for name, key in (('md_kiops', 'MD'), ('bw_gib_s', 'BW'), ('score', 'SCORE'))}
    if any(not math.isfinite(v) or v <= 0 for v in official.values()):
        raise ValueError('invalid official score')
    official['hash'] = score['hash']
    return dict(phases=phases, official=official)


def parse_ranks(text: str) -> list[dict]:
    records = [dict(rank=int(m[1]), guest=int(m[2]), host=m[3], pid=int(m[4]), endpoint=int(m[5]))
               for m in RANK_RE.finditer(clean_console(text))]
    if len(records) != 10 or {r['rank'] for r in records} != set(range(10)) or any(
        r['guest'] != r['rank'] + 1 or r['host'] != f'client{r["rank"]}' or
        r['endpoint'] != r['guest'] + 15 or r['pid'] <= 0 for r in records
    ):
        raise ValueError('IO500 rank placement is not one rank per independent client guest')
    return sorted(records, key=lambda r: r['rank'])


def parse_probe(text: str) -> dict:
    keys = ('rank', 'size', 'host', 'pid', 'begin_ns', 'end_ns', 'monotonic_begin_ns', 'monotonic_end_ns')
    records = [dict(zip(keys, (v if n == 2 else int(v) for n, v in enumerate(m.groups()))))
               for m in PROBE_RE.finditer(clean_console(text))]
    if len(records) != 10 or {r['rank'] for r in records} != set(range(10)) or any(
        r['size'] != 10 or r['host'] != f'client{r["rank"]}' or
        r['end_ns'] <= r['begin_ns'] or r['monotonic_end_ns'] <= r['monotonic_begin_ns'] for r in records
    ):
        raise ValueError('MPI probe failed rank placement or positive intervals')
    overlap = min(r['end_ns'] for r in records) - max(r['begin_ns'] for r in records)
    if overlap <= 0:
        raise ValueError('MPI probe intervals do not overlap')
    return dict(records=sorted(records, key=lambda r: r['rank']), realtime_overlap_ns=overlap)


def launch(consoles: list, command, run: Path, phase: str, record: dict) -> str:
    coordinator = consoles[1]
    start = len(coordinator.output)
    starts = [len(c.output) for c in consoles]
    executable = BIN + ('/io500-probe ranks' if phase == 'probe' else '/run-io500-rank')
    marker = f'HF3FS_IO500_MPI_EXIT phase={phase} rc='
    launcher = (f'{ENV} {BIN}/mpiexec.hydra -iface eth0 -launcher manual '
                f'-f /opt/io500/etc/clients -ppn 1 -n 10 {executable}')
    entry = record[phase] = dict(launcher=launcher, host_start_ns=time.monotonic_ns(), proxy_commands=[], returncode=None)
    owned = []
    watchdog = PhaseWatchdog() if phase == 'standard' else None
    try:
        command(1, f'/bin/busybox setsid {BIN}/io500-job {phase} mpi /bin/busybox env {launcher} '
                f'</dev/null & hf3fs_mpi_{phase}=$!', 30)
        entry['mpiexec_submitted_ns'] = time.monotonic_ns()
        owned.append((1, f'hf3fs_mpi_{phase}', 'mpi'))
        coordinator.wait('HYDRA_LAUNCH_END', 120, since=start)
        entry['hydra_launch_end_ns'] = time.monotonic_ns()
        proxies = parse_proxy_commands(coordinator.output[start:])
        entry['proxy_commands'] = proxies
        entry['proxy_submissions'] = []
        for index, argv in enumerate(proxies):
            node = index + 1
            # shlex.join prevents Hydra argv from being interpreted as shell code.
            proxy = f'{ENV} {shlex.join(argv)}'
            command(node, f'/bin/busybox setsid {BIN}/io500-job {phase} {index} /bin/busybox env {proxy} '
                    f'</dev/null & hf3fs_proxy_{phase}=$!', 30)
            entry['proxy_submissions'].append(dict(proxy=index, guest=node,
                                                   submitted_ns=time.monotonic_ns()))
            owned.append((node, f'hf3fs_proxy_{phase}', str(index)))
        offset = start
        wait_started_ns = time.monotonic_ns()
        entry['wait_started_ns'] = wait_started_ns
        deadline = time.monotonic() + (13 * 600 + 120 if watchdog else 180)
        pattern = re.compile(re.escape(marker) + r'(\d+)')
        while True:
            now = time.monotonic_ns()
            with coordinator.condition:
                output = coordinator.output
                chunk = output[offset:]
                offset = len(output)
            if watchdog:
                # Use the launch loop's own monotonic epoch. This does not
                # depend on receiving another complete output line.
                if watchdog.banner_ns is None and now - wait_started_ns >= BANNER_TIMEOUT_SECONDS * 10**9:
                    raise TimeoutError(
                        f'IO500 banner was not observed within {BANNER_TIMEOUT_SECONDS} seconds')
                watchdog.observe(chunk, now)
                entry.update(banner_observed_ns=watchdog.banner_ns, phases=watchdog.records,
                             current_phase=watchdog.current_phase, heartbeats=watchdog.heartbeats,
                             rank_receipts=watchdog.rank_receipts,
                             last_progress_ns=watchdog.last_progress_ns)
            (run / 'io500-workload.json').write_text(json.dumps(record, indent=2) + '\n')
            match = pattern.search(output, start)
            if match:
                entry.update(returncode=int(match[1]), host_end_ns=now)
                if entry['returncode']:
                    raise RuntimeError(f'MPI {phase} exited {entry["returncode"]}')
                if watchdog and len(watchdog.records) != 13:
                    raise ValueError('MPI exited without thirteen phase receipts')
                break
            if any(c.process.poll() is not None for c in consoles):
                raise RuntimeError('a guest exited during MPI')
            for c, since in zip(consoles[1:], starts[1:]):
                if re.search(r'HF3FS_IO500_PROXY_EXIT phase=' + phase + r' proxy=\d+ rc=[1-9]\d*', c.output[since:]):
                    raise RuntimeError('a Hydra proxy failed during MPI')
            if time.monotonic() > deadline:
                raise TimeoutError('MPI completion deadline exceeded: ' + phase)
            with coordinator.condition:
                coordinator.condition.wait(0.5)
        exits = []
        for index, c in enumerate(consoles[1:]):
            match = c.wait_regex(re.compile(f'HF3FS_IO500_PROXY_EXIT phase={phase} proxy={index} rc=(\\d+)'),
                                 60, since=starts[index + 1])
            exits.append(int(match[1]))
        entry['proxy_returncodes'] = exits
        if exits != [0] * 10:
            raise ValueError('Hydra proxy exit is nonzero')
        entry['clean_exit'] = True
        return coordinator.output[start:]
    except Exception as error:
        entry.update(outcome='failed', error_type=type(error).__name__, error=str(error))
        if phase == 'standard':
            entry['live_diagnostics'] = capture_live_diagnostics(consoles, command, run, phase)
        raise
    finally:
        # Preserve the failed workload's output before cleanup can add signals
        # or shutdown diagnostics to the same serial console.
        (run / ('mpi-probe.log' if phase == 'probe' else 'io500-console.log')).write_text(
            coordinator.output[start:])
        if watchdog and watchdog.failure:
            entry['fatal_output'] = watchdog.failure
        # Every MPI process is a descendant of a run-owned console variable.
        # On failure kill only those recorded jobs and their guest descendants.
        if not entry.get('clean_exit'):
            entry['forced_cleanup'] = True
            by_node = {}
            for item in reversed(owned):
                by_node.setdefault(item[0], []).append(item)
            # The coordinator owns both proxy 0 and mpiexec. Stop mpiexec
            # first so proxy 0 can leave PMI and be reaped without timing out
            # the console used for failure evidence.
            if 1 in by_node:
                by_node[1].sort(key=lambda item: item[2] != 'mpi')

            def cleanup_node(items) -> list[str]:
                errors = []
                for node, variable, index in items:
                    try:
                        command(node, f'/opt/io500/bin/stop-owned-job {phase} {index} && {{ wait ${variable}; true; }}', 30)
                    except Exception as error:
                        errors.append(str(error))
                return errors

            with ThreadPoolExecutor(max_workers=len(by_node)) as pool:
                for errors in pool.map(cleanup_node, by_node.values()):
                    entry.setdefault('cleanup_errors', []).extend(errors)
            if not entry.get('cleanup_errors'):
                entry.pop('cleanup_errors', None)
        else:
            for node, variable, index in reversed(owned):
                command(node, f'wait ${variable}', 30)
        entry.setdefault('host_end_ns', time.monotonic_ns())
        entry['duration_ms'] = (entry['host_end_ns'] - entry['host_start_ns']) / 1e6
        entry.setdefault('outcome', 'passed' if entry.get('clean_exit') else 'failed')
        (run / 'io500-workload.json').write_text(json.dumps(record, indent=2) + '\n')


def export_results(command, run: Path) -> dict:
    # Copy through FUSE before retirement. Local copies retain a readable failure
    # artifact even if a later console export or verifier command fails.
    command(1, f'mkdir -p /var/lib/3fs/io500-results && cp {RESULT_DIR}/* /var/lib/3fs/io500-results/', 600)
    output = command(1, 'for f in /var/lib/3fs/io500-results/*; do test -f "$f" || continue; '
                     'printf "HF3FS_IO500_FILE %s " "${f##*/}"; sha256sum "$f"; done', 60)
    entries = re.findall(r'(?m)^HF3FS_IO500_FILE ([a-zA-Z0-9_.-]+) ([a-f0-9]{64})\s+', output)
    if not {'config.ini', 'result.txt'} <= {e[0] for e in entries} or len(entries) != len(set(e[0] for e in entries)):
        raise ValueError('IO500 export lacks config/result or duplicates a file')
    destination = run / 'io500-results'
    destination.mkdir(exist_ok=True)
    records = {}
    for name, digest in entries:
        output = command(1, f'printf "HF3FS_IO500_BASE64_BEGIN\\n"; /bin/busybox base64 /var/lib/3fs/io500-results/{name}; '
                            'printf "HF3FS_IO500_BASE64_END\\n"', 120)
        match = re.search(r'(?m)^HF3FS_IO500_BASE64_BEGIN\s*\n([A-Za-z0-9+/=\s]+)^HF3FS_IO500_BASE64_END', output)
        if match is None:
            raise ValueError('missing base64 result export: ' + name)
        data = base64.b64decode(''.join(match[1].split()), validate=True)
        (destination / name).write_bytes(data)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('exported result checksum mismatch: ' + name)
        records[name] = dict(path=str(destination / name), bytes=len(data), sha256=digest)
    return records


def preflight(consoles: list, command, run: Path) -> dict:
    record = dict(status='failed')
    try:
        text = launch(consoles, command, run, 'probe', record)
        (run / 'mpi-probe.log').write_text(text)
        record['placement_probe'] = parse_probe(text)
        record['status'] = 'passed'
    except Exception as error:
        record['first_failure'] = str(error)
    (run / 'io500-preflight.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def execute(consoles: list, command, run: Path) -> dict:
    record = dict(status='failed', profile='standard', stonewall_seconds=300, clients=10, ranks=10,
                  clock_mutations_during_workload=False)
    try:
        prepared = json.loads((run / 'io500-preflight.json').read_text())
        if prepared.get('status') != 'passed':
            raise ValueError('MPI preflight did not pass before service startup')
        record.update(probe=prepared['probe'], placement_probe=prepared['placement_probe'])
        record['clocks_before'] = clocks(consoles)
        text = launch(consoles, command, run, 'standard', record)
        (run / 'io500-console.log').write_text(text)
        record['rank_placement'] = parse_ranks(text)
        record['clocks_after'] = clocks(consoles)
        errors = clock_validation(record['clocks_before'], record['clocks_after'])
        if errors:
            raise ValueError('; '.join(errors))
        record['files'] = export_results(command, run)
        result_text = (run / 'io500-results/result.txt').read_text()
        record['metrics'] = parse_metrics(result_text)
        verifier = command(1, f'{ENV} {BIN}/io500-verify /var/lib/3fs/io500-results/config.ini '
                              '/var/lib/3fs/io500-results/result.txt 1; rc=$?; '
                              'printf "HF3FS_IO500_VERIFY_EXIT rc=%s\\n" "$rc"', 600)
        (run / 'io500-verify.log').write_text(verifier)
        matches = re.findall(r'(?m)^HF3FS_IO500_VERIFY_EXIT rc=(\d+)\s*$', verifier)
        record['verifier'] = dict(returncode=int(matches[0]) if len(matches) == 1 else None,
            ok='[OK]' in verifier, invalid='[INVALID]' in verifier, sha256=hashlib.sha256(verifier.encode()).hexdigest())
        if record['verifier']['returncode'] != 0 or not record['verifier']['ok'] or record['verifier']['invalid']:
            raise ValueError('official IO500 verifier did not accept the standard result')
        record['status'] = 'passed'
    except Exception as error:
        record['first_failure'] = str(error)
    finally:
        (run / 'io500-workload.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def validate_result(result: dict) -> list[str]:
    """Independent measured-workload gates, in addition to shared cohort gates."""
    errors = []
    work = result.get('applications', {}).get('workload', {})
    bundle = result.get('io500_bundle', {})
    if (result.get('workload') != 'io500-standard' or bundle.get('record', {}).get('schema') != prepare_io500.SCHEMA
        or len(bundle.get('manifest_sha256', '')) != 64):
        errors.append('standard benchmark profile or bundle binding is absent')
    initial = result.get('clock_initialization', [])
    if len(initial) != 11 or {r.get('guest') for r in initial} != set(range(11)) or any(
        type(r.get('set_epoch')) is not int or r['set_epoch'] <= 0 for r in initial
    ):
        errors.append('pre-FDB clock initialization is incomplete')
    if (work.get('status') != 'passed' or work.get('first_failure') or work.get('profile') != 'standard' or
        work.get('stonewall_seconds') != 300 or work.get('clients') != 10 or work.get('ranks') != 10 or
        work.get('clock_mutations_during_workload') is not False):
        errors.append('standard workload failed or parameters changed')
    for phase in ('probe', 'standard'):
        mpi = work.get(phase, {})
        if (mpi.get('returncode') != 0 or mpi.get('proxy_returncodes') != [0] * 10 or
            mpi.get('clean_exit') is not True or mpi.get('forced_cleanup') or mpi.get('cleanup_errors') or
            len(mpi.get('proxy_commands', [])) != 10 or mpi.get('host_end_ns', 0) <= mpi.get('host_start_ns', 0)):
            errors.append(f'{phase} MPI completion, placement or cleanup failed')
    ranks = work.get('rank_placement', [])
    if (len(ranks) != 10 or {r.get('rank') for r in ranks} != set(range(10)) or any(
        r.get('guest') != r['rank'] + 1 or r.get('host') != f'client{r["rank"]}' or
        r.get('endpoint') != r['rank'] + 16 for r in ranks)):
        errors.append('standard ranks are not one per independent client')
    probe = work.get('placement_probe', {})
    if (len(probe.get('records', [])) != 10 or probe.get('realtime_overlap_ns', 0) <= 0):
        errors.append('MPI concurrency probe is incomplete')
    errors.extend(clock_validation(work.get('clocks_before', []), work.get('clocks_after', [])))
    phases = work.get('metrics', {}).get('phases', [])
    windows = work.get('standard', {}).get('phases', [])
    for label, records in (('official', phases), ('observed', windows)):
        if len(records) != 13 or {p.get('name') for p in records} != set(PHASES):
            errors.append(label + ' result does not contain thirteen distinct phases')
        for p in records:
            value = p.get('seconds', 0)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 600:
                errors.append(label + ' phase elapsed time violates the standard gate')
    if len(windows) == 13:
        if [p['name'] for p in windows] != list(PHASES):
            errors.append('observed standard phases are out of order')
        previous = work.get('standard', {}).get('banner_observed_ns', 0)
        for window in windows:
            now = window.get('observed_ns', 0)
            seconds = window.get('host_window_seconds', -1)
            if (not 0 <= seconds <= 600 or now < previous or previous <= 0 or
                abs((now - previous) / 1e9 - seconds) > 0.001):
                errors.append('host phase timing window is absent, invalid or too long')
            previous = now
        by_name = {p['name']: p for p in phases}
        if any(w['name'] not in by_name or abs(w['seconds'] - by_name[w['name']]['seconds']) > 0.01 for w in windows):
            errors.append('console and official phase durations disagree')
    official = work.get('metrics', {}).get('official', {})
    if any(type(official.get(k)) not in (float, int) or not math.isfinite(official[k]) or official[k] <= 0
           for k in ('score', 'bw_gib_s', 'md_kiops')) or not official.get('hash'):
        errors.append('official IO500 score is missing or invalid')
    files = work.get('files', {})
    if not {'config.ini', 'result.txt'} <= set(files) or any(
        f.get('bytes', 0) <= 0 or len(f.get('sha256', '')) != 64 for f in files.values()
    ):
        errors.append('raw IO500 result/config export is incomplete')
    verifier = work.get('verifier', {})
    if (verifier.get('returncode') != 0 or verifier.get('ok') is not True or verifier.get('invalid') is not False or
        len(verifier.get('sha256', '')) != 64):
        errors.append('official IO500 verifier did not pass')
    return errors
