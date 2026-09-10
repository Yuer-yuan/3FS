"""Finite serial evidence transfer. Missing bytes are errors, never causal proof."""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import re
import shlex
import time
import uuid

CHUNK_BYTES = 2048  # Base64 plus framing/echo stays below 4 KiB per command.
MAX_BYTES = 1024 * 1024


def export_file(command, node: int, guest_path: str, destination: Path, *, timeout=30,
                max_bytes=MAX_BYTES) -> dict:
    """Export a stable guest file; verify total length and SHA256 before accepting."""
    path = shlex.quote(guest_path)
    info = command(node, f"printf 'HF3FS_DIAG_SIZE '; wc -c <{path}; "
                   f"printf 'HF3FS_DIAG_HASH '; sha256sum {path}", timeout)
    size = re.search(r'(?m)^HF3FS_DIAG_SIZE\s+(\d+)\r*$', info)
    digest = re.search(r'(?m)^HF3FS_DIAG_HASH ([0-9a-f]{64})\s', info)
    if size is None or digest is None:
        raise ValueError('diagnostic file metadata is missing')
    length = int(size[1])
    if length > max_bytes:
        raise ValueError(f'diagnostic capacity exceeded: {length} > {max_bytes}')
    partial = destination.with_suffix(destination.suffix + '.partial')
    hasher = hashlib.sha256()
    received = 0
    with partial.open('wb') as stream:
        for offset in range(0, length, CHUNK_BYTES):
            count = min(CHUNK_BYTES, length - offset)
            output = command(node, f"printf 'HF3FS_DIAG_CHUNK {offset} '; "
                             f"dd if={path} bs={CHUNK_BYTES} skip={offset // CHUNK_BYTES} count=1 "
                             "2>/dev/null | /bin/busybox base64 | tr -d '\\n'; printf '\\n'", timeout)
            match = re.search(rf'(?m)^HF3FS_DIAG_CHUNK {offset} ([A-Za-z0-9+/=]*)\r*$', output)
            if match is None:
                raise ValueError(f'diagnostic chunk missing at {offset}')
            data = base64.b64decode(match[1], validate=True)
            if len(data) != count:
                raise ValueError(f'diagnostic chunk length mismatch at {offset}')
            stream.write(data)
            hasher.update(data)
            received += len(data)
    if received != length or hasher.hexdigest() != digest[1]:
        raise ValueError('diagnostic length/hash mismatch; partial evidence retained')
    partial.replace(destination)
    return dict(path=str(destination), bytes=received, sha256=hasher.hexdigest(), verified=True)


def capture(command, node: int, text: str, destination: Path, *, timeout=30,
            guest_directory='/var/log/3fs', max_bytes=MAX_BYTES) -> dict:
    """Capture once to a capped guest file, then export without streaming service logs."""
    started = time.monotonic_ns()
    token = uuid.uuid4().hex
    guest_path = f'{guest_directory}/diagnostic-{token}.log'
    path = shlex.quote(guest_path)
    status = shlex.quote(guest_path + '.rc')
    record = dict(guest=node, guest_path=guest_path, host_start_ns=started, verified=False)
    try:
        # The extra byte makes overflow explicit. If a producer gets SIGPIPE,
        # missing/nonzero status also prevents accepting the capture.
        command(node, f"( {text}; hf3fs_diag_rc=$?; printf '%s' $hf3fs_diag_rc >{status} ) "
                f"2>&1 | head -c {max_bytes + 1} >{path}", timeout)
        result = export_file(command, node, guest_path, destination, timeout=timeout, max_bytes=max_bytes)
        output = command(node, f"printf 'HF3FS_DIAG_RC '; cat {status}; printf '\\n'", timeout)
        match = re.search(r'(?m)^HF3FS_DIAG_RC (\d+)\r*$', output)
        if match is None or int(match[1]) != 0:
            raise ValueError('diagnostic producer status missing or failed')
        record.update(result, outcome='passed')
        # Only delete this capture after successful export. Failure files stay
        # on the guest disk for recovery, independent of the business failure.
        command(node, f'rm -f {path} {status}', timeout)
    except Exception as error:
        record.update(outcome='failed', error_type=type(error).__name__, error=str(error))
    record.update(host_end_ns=time.monotonic_ns())
    record['duration_ms'] = (record['host_end_ns'] - started) / 1e6
    return record


def validate_rpc_trace(path: Path) -> dict:
    data = path.read_bytes()
    header = data[:256].decode('ascii')
    match = re.fullmatch(
        r'HF3FS_RPC_TRACE_V1 attempted=(\d+) committed=(\d+) dropped=(\d+) errors=(\d+) bytes=(\d+) capacity=(\d+) *\n',
        header)
    if match is None:
        raise ValueError('missing or torn request trace header')
    attempted, committed, dropped, errors, size, capacity = map(int, match.groups())
    lines = data[256:].splitlines(keepends=True)
    sequences = [re.match(rb'seq=(\d+) mono_ns=(\d+) pid=(\d+) tid=(\d+) ', line) for line in lines]
    if size != len(data) - 256 or size > capacity or len(lines) != committed or any(
        m is None or not line.endswith(b'\n') for m, line in zip(sequences, lines)):
        raise ValueError('request trace length/record count mismatch')
    if dropped or errors or attempted != committed:
        raise ValueError(f'request trace incomplete: attempted={attempted} committed={committed} dropped={dropped} errors={errors}')
    if [int(m[1]) for m in sequences] != list(range(1, committed + 1)):
        raise ValueError('request trace has missing or reordered sequences')
    times = [int(m[2]) for m in sequences]
    if times != sorted(times) or len({m[3] for m in sequences}) > 1:
        raise ValueError('request trace process/monotonic clock mismatch')
    return dict(records=committed, dropped=dropped, errors=errors, bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(), complete=True)
