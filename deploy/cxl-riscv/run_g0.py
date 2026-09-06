#!/usr/bin/env python3
"""Run and validate the two-guest 3FS CXL RISC-V G0-A gate."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any


DEPLOY_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DEPLOY_DIR.parents[1]
RESULT_SCHEMA = "hf3fs.cxl-riscv-g0.v1"
CXL_HPA_BASE = 0x1000000000
CXL_DPA_BASE = 0
CXL_CAPACITY = 256 * 1024 * 1024
LSA_CAPACITY = 2 * 1024 * 1024
DAX_PROBE_PAYLOAD_BYTES = 64 * 1024 - 64
FDB_READY_ATTEMPTS = 480
FDB_READY_SLEEP_SECONDS = 0.5
FDB_CONSOLE_LOG = "/var/log/fdb/fdbserver-console.log"
FDB_MEMORY_LIMIT_MIB = 1024
FDB_CACHE_MEMORY_MIB = 128
FDB_STORAGE_MEMORY_MIB = 128
# FDB 7.3.63 advances 1e6 versions/second and defaults to a five-second
# transaction window plus a twenty-second commit-proxy progress deadline.
# The ten-client TCG cohort exceeded both during otherwise live startup.
# Keep finite bounds below 3FS's 180-second lease and retain TCP/ACID behavior.
# Periodic metrics and signal-based stack sampling are expensive under TCG.
# Retain metrics every 30 seconds and ordinary warning/error trace events;
# disable only the optional run-loop stack profiler for the ten-client cohort.
FDB_COHORT_KNOBS = dict(max_read_transaction_life_versions=60_000_000,
                        max_write_transaction_life_versions=60_000_000,
                        commit_proxy_liveness_timeout=120,
                        system_monitor_interval=30,
                        worker_logging_interval=30,
                        storage_logging_delay=30,
                        run_loop_profiling_interval=0)
HOST_DECODER = (
    "CXL host decoder0: HPA 0000001000000000 "
    "size 0000000010000000 target 0 ctrl 00000600"
)
TYPE3_DECODER = (
    "41.00.0 Type 3 decoder0: HPA 0000001000000000 "
    "size 0000000010000000 target 0 ctrl 00001600"
)
PLATFORM_SMOKE_BINARIES = (
    "cxl_dax_smoke",
    "fuse_mount_smoke",
    "fdb_client_smoke",
    "fdbserver",
    "fdbcli",
)

if str(DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(DEPLOY_DIR))
import guest_image


class G0Error(RuntimeError):
    pass


@dataclass(frozen=True)
class PlatformPaths:
    qemu: Path
    cxlmemsim_server: Path
    topology: Path
    opensbi: Path
    uboot: Path
    kernel: Path


@dataclass(frozen=True)
class RuntimeConfig:
    guest_memory: str = "2G"
    timeout_seconds: int = 300
    smp: int = 5


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_qemu_command(
    paths: PlatformPaths,
    config: RuntimeConfig,
    *,
    node: int,
    coherence_port: int,
    root_image: Path,
    endpoint_memory: Path,
    lsa: Path,
    guest_count: int = 2,
    capacity: int = CXL_CAPACITY,
    root_snapshot: bool = False,
    config_image: Path | None = None,
) -> list[str]:
    if not 2 <= guest_count <= 32 or not 0 <= node < guest_count:
        raise ValueError("node must be in the declared 2..32 guest cohort")
    if capacity < CXL_CAPACITY or capacity > 4 * 1024**3 or capacity % CXL_CAPACITY:
        raise ValueError("CXL capacity must be a multiple of 256 MiB within the 4 GiB HPA window")
    if not 0 < coherence_port <= 65535:
        raise ValueError("coherence port must be in 1..65535")
    prefix = f"g0-node{node}"
    command = [
        str(paths.qemu),
        "-name",
        prefix,
        "-M",
        "sifive_u",
        "-cpu",
        "rv64,h=false,sstc=false,svadu=false,zicboz=false,zicbom=true,cbom_blocksize=64",
        "-machine",
        "cxl=on",
        "-machine",
        f"cxl-fmw.0.targets.0=cxl-{prefix},cxl-fmw.0.size=4G,cxl-fmw.0.restrictions=0x29",
        "-smp",
        str(config.smp),
        "-m",
        config.guest_memory,
        "-display",
        "none",
        "-serial",
        "stdio",
        "-monitor",
        "none",
        "-no-reboot",
        "-bios",
        str(paths.opensbi),
        "-kernel",
        str(paths.uboot),
        "-device",
        f"loader,file={paths.kernel},addr=0x90000000,force-raw=on",
        "-object",
        f"memory-backend-file,id=t3pmem-{prefix},mem-path={endpoint_memory},size={capacity // 1024**2}M,share=on",
        "-object",
        f"memory-backend-file,id=t3lsa-{prefix},mem-path={lsa},size=2M,share=on",
        "-device",
        f"pxb-cxl,bus=pcie.0,bus_nr=64,id=cxl-{prefix},hdm_for_passthrough=on",
        "-device",
        f"cxl-rp,bus=cxl-{prefix},port=0,id=rp-{prefix},chassis=0,slot=0,x-256b-flit=on",
        "-device",
        (
            f"cxl-type3,bus=rp-{prefix},persistent-memdev=t3pmem-{prefix},"
            f"lsa=t3lsa-{prefix},id=t3-{prefix},coherence-v2=on,x-256b-flit=on,"
            "hdm-db=on,cxlmemsim-addr=127.0.0.1,"
            f"cxlmemsim-port={coherence_port},coherence-v2-host-id={node},"
            "coherence-v2-cache-capacity=8388608,coherence-v2-cache-ways=4,"
            "coherence-v2-timeout-ms=5000,coherence-v2-write-through=off,"
            f"coherence-v2-read-exclusive={'on' if node == 0 else 'off'}"
        ),
        "-drive",
        f"file={root_image},if=none,format=raw,id=root-{prefix}"
        + (",snapshot=on" if root_snapshot else ""),
        "-device",
        f"virtio-blk-pci,drive=root-{prefix},bus=pcie.0,id=rootdev-{prefix}",
    ]
    if config_image is not None:
        command.extend([
            "-drive",
            f"file={config_image},if=none,format=raw,readonly=on,id=config-{prefix}",
            "-device",
            f"virtio-blk-pci,drive=config-{prefix},bus=pcie.0,id=configdev-{prefix}",
        ])
    return command


def build_server_command(
    paths: PlatformPaths,
    *,
    coherence_port: int,
    trace: Path | None,
    backing: Path,
    capacity: int = CXL_CAPACITY,
) -> list[str]:
    if capacity < CXL_CAPACITY or capacity > 4 * 1024**3 or capacity % CXL_CAPACITY:
        raise ValueError("invalid CXL capacity")
    if not 0 < coherence_port <= 65535:
        raise ValueError("coherence port must be in 1..65535")
    command = [
        str(paths.cxlmemsim_server),
        "--comm-mode=tcp",
        f"--port={coherence_port}",
        f"--capacity={capacity // 1024**2}",
        "--default_latency=100",
        f"--topology={paths.topology}",
        "--coherence-v2=true",
        "--coherence-v2-snoop-timeout-ms=5000",
        "--backing-mode=ssd-stream",
        f"--ssd-backing-file={backing}",
        "--ssd-page-size=4096",
        "--ssd-io-chunk-size=65536",
        "--ssd-cache-mb=16",
        "--ssd-read-ahead-pages=16",
        "--ssd-io-uring=false",
        "--ssd-odirect=false",
    ]
    if trace is not None:
        command.insert(8, f"--coherence-v2-trace={trace}")
    return command


def parse_coherence_stats(path: Path, expected_registrations: int) -> dict[str, Any]:
    matches = re.findall(r"(?m)^COHERENCE_V2_STATS_JSON (\{[^\r\n]+\})\r?$",
                         path.read_text(encoding="utf-8", errors="replace"))
    if len(matches) != 1:
        raise G0Error("CXLMemSim final coherence statistics are absent or duplicated")
    record = json.loads(matches[0])
    required = {
        "registrations", "gets", "getm", "upgrade", "puts", "putm", "request_fence",
        "snp_inv", "snp_downgrade", "snp_data_inv", "snp_data_downgrade", "host_fence",
        "model_acks", "native_acks", "dirty_data_completions", "persistence_fence_completions",
        "timeouts", "protocol_errors", "delivery_failures", "server_copy_failures", "active_bindings",
    }
    if set(record) != required or any(type(record[name]) is not int or record[name] < 0 for name in required):
        raise G0Error("CXLMemSim final coherence statistics have an invalid schema")
    error_events = sum(record[name] for name in
                       ("timeouts", "protocol_errors", "delivery_failures", "server_copy_failures"))
    complete = (record["registrations"] == expected_registrations and record["gets"] > 0 and
                record["getm"] > 0 and record["model_acks"] > 0 and
                record["dirty_data_completions"] > 0 and error_events == 0 and
                record["active_bindings"] == 0)
    return {
        "complete": complete,
        "mode": "final-aggregate-stats-plus-bidirectional-dax-records",
        "stats": record,
        "error_events": error_events,
        "log": str(path),
        "log_sha256": sha256_file(path),
    }


def validate_result(result: Mapping[str, Any], profile: str = "platform-smoke") -> list[str]:
    if profile not in ("platform-smoke", "full-3fs"):
        return ["unsupported G0 profile"]
    errors: list[str] = []
    if result.get("schema") != RESULT_SCHEMA:
        errors.append("G0 result schema is unsupported")
    binaries = result.get("binaries")
    if not isinstance(binaries, Mapping):
        errors.append("G0 binary evidence is absent")
        binaries = {}
    required = PLATFORM_SMOKE_BINARIES
    if profile == "full-3fs":
        import full_stack
        required += full_stack.APPLICATIONS
        errors.extend(full_stack.validate_evidence(result.get("full_stack", {})))
        if result.get("gate") != "G0-B" or not result.get("build_manifest_sha256") or not result.get("source_closure_sha256"):
            errors.append("G0-B build/source identity is incomplete")
    for name in required:
        record = binaries.get(name)
        if not isinstance(record, Mapping):
            errors.append(f"{name} evidence is absent")
            continue
        if record.get("machine") != "RISC-V":
            errors.append(f"{name} is not RISC-V")
        needed = record.get("needed")
        if not isinstance(needed, list):
            errors.append(f"{name} dependency evidence is absent")
        else:
            for library in needed:
                if isinstance(library, str) and (
                    library == "libibverbs.so" or library.startswith("libibverbs.so.")
                ):
                    errors.append(f"{name} links {library}")

    dax = result.get("dax")
    if not isinstance(dax, Mapping):
        errors.append("DAX evidence is absent")
        dax = {}
    guests = dax.get("guests")
    if not isinstance(guests, list) or len(guests) != 2:
        errors.append("DAX evidence does not contain exactly two guests")
        guests = []
    else:
        host_ids = [guest.get("host_id") for guest in guests if isinstance(guest, Mapping)]
        inodes = [tuple(guest.get("backing_inode", ())) for guest in guests if isinstance(guest, Mapping)]
        if len(set(host_ids)) != 2:
            errors.append("DAX guests do not have distinct host IDs")
        if len(set(inodes)) != 2:
            errors.append("DAX guests do not have distinct backing inodes")
    if dax.get("qemu_lifetimes_overlap") is not True:
        errors.append("DAX guest lifetimes did not overlap")
    directions = dax.get("directions")
    if not isinstance(directions, list):
        directions = []
    observed_directions: set[tuple[Any, Any]] = set()
    for direction in directions:
        if not isinstance(direction, Mapping):
            continue
        observed_directions.add((direction.get("writer_host"), direction.get("reader_host")))
        writer = direction.get("writer")
        reader = direction.get("reader")
        if not isinstance(writer, Mapping) or not isinstance(reader, Mapping):
            errors.append("DAX transfer record is incomplete")
            continue
        if (
            writer.get("status") != "passed"
            or reader.get("status") != "passed"
            or writer.get("generation") != reader.get("generation")
            or writer.get("checksum") != reader.get("checksum")
        ):
            errors.append("DAX peer publication does not reproduce generation/checksum")
        for endpoint in (writer, reader):
            if endpoint.get("atomic_u32_lock_free") is not True or endpoint.get(
                "atomic_u64_lock_free"
            ) is not True:
                errors.append("DAX 32/64-bit atomics are not lock-free")
            if endpoint.get("payload_bytes") != DAX_PROBE_PAYLOAD_BYTES:
                errors.append("DAX bounded payload evidence is absent or changed")
    if observed_directions != {(0, 1), (1, 0)}:
        errors.append("DAX peer publication is not bidirectional")
    coherence = dax.get("coherence")
    if not isinstance(coherence, Mapping) or coherence.get("complete") is not True:
        errors.append("DAX range has no authoritative CXLMemSim coherence evidence")
    else:
        if coherence.get("error_events") != 0:
            errors.append("CXLMemSim coherence trace contains error events")
        if coherence.get("mode") == "final-aggregate-stats-plus-bidirectional-dax-records":
            stats = coherence.get("stats")
            if not isinstance(stats, Mapping) or stats.get("dirty_data_completions", 0) <= 0:
                errors.append("CXLMemSim dirty BI hand-off completion is absent")
        else:
            handoffs = coherence.get("dirty_handoffs")
            if not isinstance(handoffs, list) or {
                (item.get("owner_host"), item.get("requester_host"))
                for item in handoffs
                if isinstance(item, Mapping)
            } != {(0, 1), (1, 0)}:
                errors.append("CXLMemSim dirty BI hand-off is not bidirectional")

    fuse = result.get("fuse")
    if not isinstance(fuse, Mapping) or any(
        fuse.get(field) is not True
        for field in ("lookup_seen", "open_seen", "read_seen", "unmounted")
    ):
        errors.append("FUSE lookup/open/read/unmount proof is incomplete")
    fdb = result.get("fdb")
    if not isinstance(fdb, Mapping) or any(
        fdb.get(field) is not True for field in ("guest_server", "set_ok", "get_ok", "value_match")
    ):
        errors.append("guest FoundationDB transaction proof is incomplete")
    elif fdb.get("api_version") != 710:
        errors.append("guest FoundationDB API version is not 710")
    teardown = result.get("teardown")
    if not isinstance(teardown, Mapping) or teardown.get("all_owned_processes_stopped") is not True:
        errors.append("G0 runner cleanup proof is incomplete")
    return list(dict.fromkeys(errors))


def _strict_json_records(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{") or not stripped.endswith("}"):
            continue
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _require_probe_record(output: str, description: str) -> dict[str, Any]:
    records = _strict_json_records(output)
    if len(records) != 1:
        raise G0Error(f"{description} emitted {len(records)} JSON records")
    record = records[0]
    if record.get("status") != "passed":
        raise G0Error(f"{description} did not pass: {record}")
    return record


def fdb_readiness_command() -> str:
    """Build a bounded readiness probe that leaves useful guest-side evidence."""
    return (
        "attempt=0; ready=0; fdb_pid=$(cat /run/fdbserver.pid); "
        f"while [ $attempt -lt {FDB_READY_ATTEMPTS} ]; do "
        "if nc -z -w 1 127.0.0.1 4500; then ready=1; break; fi; "
        "if ! kill -0 $fdb_pid 2>/dev/null; then break; fi; "
        f"attempt=$((attempt + 1)); sleep {FDB_READY_SLEEP_SECONDS}; done; "
        "if [ $ready -ne 1 ]; then "
        "echo HF3FS_G0_FDB_START_FAILED attempts=$attempt; "
        "if [ -r /run/fdbserver.pid ]; then "
        "fdb_pid=$(cat /run/fdbserver.pid); echo HF3FS_G0_FDB_PID pid=$fdb_pid; "
        "if kill -0 $fdb_pid 2>/dev/null; then echo HF3FS_G0_FDB_ALIVE true; "
        "else echo HF3FS_G0_FDB_ALIVE false; fi; fi; "
        "ps; "
        f"if [ -r {FDB_CONSOLE_LOG} ]; then "
        f"echo HF3FS_G0_FDB_LOG_BEGIN; sed -n '1,240p' {FDB_CONSOLE_LOG}; "
        "echo HF3FS_G0_FDB_LOG_END; fi; false; else true; fi"
    )


def fdb_server_command(*, clients: int = 1) -> str:
    """Build the resource-bounded machine-local TCP FDB command."""
    if clients not in (1, 10):
        raise ValueError("unsupported FoundationDB guest cohort")
    knobs = FDB_COHORT_KNOBS if clients == 10 else {}
    options = "".join(f"--knob-{name} {value} " for name, value in knobs.items())
    affinity = "/bin/busybox taskset 1 " if clients == 10 else ""
    return (
        f"{affinity}/opt/foundationdb/bin/fdbserver -C /opt/3fs/etc/fdb.cluster "
        "-p 127.0.0.1:4500 -d /var/lib/fdb -L /var/log/fdb "
        "--locality-machineid g0 --locality-zoneid g0 "
        f"--memory {FDB_MEMORY_LIMIT_MIB}MiB "
        f"--cache-memory {FDB_CACHE_MEMORY_MIB}MiB "
        f"--storage-memory {FDB_STORAGE_MEMORY_MIB}MiB "
        f"{options}"
        f">{FDB_CONSOLE_LOG} 2>&1 & echo $! >/run/fdbserver.pid"
    )


def analyze_trace(path: Path, length: int, *, expected_directions=None) -> dict[str, Any]:
    if expected_directions is None:
        expected_directions = {(0, 1), (1, 0)}
    event_counts: Counter[str] = Counter()
    statistics = {"error_events": 0}
    trace_digest = hashlib.sha256()

    def records():
        previous = -1
        count = 0
        decoder = None
        if path.suffix == ".zst":
            decoder = subprocess.Popen(
                ["/usr/bin/zstd", "-q", "-d", "-c", str(path)],
                stdout=subprocess.PIPE,
            )
            source = decoder.stdout
        else:
            source = path.open("rb")
        if source is None:
            raise G0Error("cannot read compressed coherence trace")
        try:
            for count, line in enumerate(source, 1):
                trace_digest.update(line)
                if not line.endswith(b"\n"):
                    raise G0Error("CXLMemSim coherence trace is empty or truncated")
                try:
                    record = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise G0Error(f"invalid coherence trace record {count}: {error}") from error
                if not isinstance(record, dict) or record.get("schema_version") != 1:
                    raise G0Error(f"unsupported coherence trace record {count}")
                monotonic = record.get("monotonic_ns")
                if isinstance(monotonic, bool) or not isinstance(monotonic, int) or monotonic < previous:
                    raise G0Error("coherence trace timestamps are invalid")
                previous = monotonic
                event_counts[str(record.get("event"))] += 1
                if (record.get("event") in {"timeout", "protocol_error", "delivery_failure", "server_copy_failure"}
                        or ("status" in record and record["status"] not in {"OK", "REGISTERED"})):
                    statistics["error_events"] += 1
                yield record
        finally:
            source.close()
            if decoder is not None:
                returncode = decoder.wait()
                if returncode:
                    raise G0Error(f"coherence trace decoder exited with {returncode}")
        if not count:
            raise G0Error("CXLMemSim coherence trace is empty or truncated")
    # CXLMemSim's coherence-v2 protocol reports device-relative DPA offsets.
    # The guest maps this interval at CXL_HPA_BASE, but the trace never carries
    # that HPA bias.
    lower = CXL_DPA_BASE
    upper = CXL_DPA_BASE + length
    latest_requests: dict[int, dict[str, Any]] = {}
    pending: dict[int, dict[str, Any]] = {}
    handoff_counts: Counter[tuple[Any, Any]] = Counter()
    handoff_samples: dict[tuple[Any, Any], dict[str, Any]] = {}
    for record in records():
        event = record.get("event")
        line = record.get("line_address")
        if not isinstance(line, int) or not lower <= line < upper:
            continue
        if event == "request" and record.get("opcode") in {"GETS", "GETM", "UPGRADE"}:
            latest_requests[line] = record
            continue
        snoop_id = record.get("snoop_id")
        if isinstance(snoop_id, bool) or not isinstance(snoop_id, int):
            continue
        if event == "snoop_send" and record.get("opcode") in {
            "SNP_DATA_INV",
            "SNP_DATA_DOWNGRADE",
        }:
            request = latest_requests.get(line)
            if request is not None:
                pending[snoop_id] = {
                    "line_address": line,
                    "snoop_id": snoop_id,
                    "owner_host": record.get("dst_host"),
                    "requester_host": request.get("src_host"),
                    "snoop_opcode": record.get("opcode"),
                    "ack": False,
                    "completion": False,
                }
            continue
        handoff = pending.get(snoop_id)
        if handoff is None or handoff["line_address"] != line:
            continue
        if (
            event == "snoop_ack"
            and record.get("src_host") == handoff["owner_host"]
            and record.get("dirty_data") is True
            and record.get("payload_len") == 64
            and record.get("status") == "OK"
        ):
            handoff["ack"] = True
            continue
        if (
            event == "dirty_completion"
            and record.get("src_host") == handoff["owner_host"]
            and record.get("dirty_data") is True
            and record.get("payload_len") == 64
            and record.get("status") == "OK"
        ):
            handoff["completion"] = True
            if handoff["ack"]:
                direction = (handoff["owner_host"], handoff["requester_host"])
                handoff_counts[direction] += 1
                handoff_samples.setdefault(
                    direction,
                    {
                        key: value
                        for key, value in handoff.items()
                        if key not in {"ack", "completion"}
                    }
                    | {"payload_len": 64},
                )
                del pending[snoop_id]
    handoffs = list(handoff_samples.values())
    directions = set(handoff_counts)
    error_events = statistics["error_events"]
    evidence = {
        "complete": directions == expected_directions and error_events == 0,
        "range": {
            "dpa_start": lower,
            "guest_hpa_start": CXL_HPA_BASE,
            "length": length,
        },
        "trace": str(path),
        "trace_sha256": trace_digest.hexdigest(),
        "event_counts": dict(event_counts),
        "error_events": error_events,
        "dirty_handoffs": handoffs,
        "dirty_handoff_counts": {
            f"{owner}->{requester}": count
            for (owner, requester), count in sorted(handoff_counts.items())
        },
    }
    if path.suffix == ".zst":
        evidence.update(
            trace_encoding="zstd",
            trace_archive_bytes=path.stat().st_size,
            trace_archive_sha256=sha256_file(path),
        )
    return evidence


class CompressedTrace:
    """Drain the simulator trace through a FIFO into a complete zstd archive."""

    def __init__(self, fifo: Path, archive: Path):
        self.fifo = fifo
        self.archive = archive
        self.log_path = archive.with_suffix(archive.suffix + ".log")
        if fifo.exists() or archive.exists() or self.log_path.exists():
            raise G0Error("trace capture paths must be fresh")
        os.mkfifo(fifo, 0o600)
        self.guard = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        reader = fifo.open("rb", buffering=0)
        output = archive.open("xb")
        self.log = self.log_path.open("xb")
        try:
            self.process = subprocess.Popen(
                ["/usr/bin/zstd", "-1", "-q", "-c"],
                stdin=reader,
                stdout=output,
                stderr=self.log,
                start_new_session=True,
            )
        finally:
            reader.close()
            output.close()

    def release_guard(self) -> None:
        if self.guard is not None:
            os.close(self.guard)
            self.guard = None

    def finish(self, timeout: float = 120) -> int:
        self.release_guard()
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)
        finally:
            self.log.close()
            self.fifo.unlink(missing_ok=True)
        return self.process.returncode


class PortReservation:
    def __init__(self) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.port = int(self.socket.getsockname()[1])

    def release(self) -> None:
        if self.socket.fileno() >= 0:
            self.socket.close()


class GuestConsole:
    def __init__(
        self, command: Sequence[str], log: Path, environment: Mapping[str, str]
    ) -> None:
        self.command = list(command)
        self.log_path = log
        self.log = log.open("wb")
        self.start_ns = time.monotonic_ns()
        self.stop_ns: int | None = None
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            start_new_session=True,
            env=dict(environment),
        )
        self.output = ""
        self.output_chunks: list[tuple[int, int]] = []
        self.condition = threading.Condition()
        self.reader = threading.Thread(target=self._read, name=f"qemu-console-{self.process.pid}")
        self.reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        while True:
            chunk = self.process.stdout.read(4096)
            if not chunk:
                break
            self.log.write(chunk)
            self.log.flush()
            decoded = chunk.decode("utf-8", errors="replace")
            with self.condition:
                self.output += decoded
                self.output_chunks.append((len(self.output), time.monotonic_ns()))
                self.condition.notify_all()
        with self.condition:
            self.condition.notify_all()

    def wait(self, marker: str, timeout: float, *, since: int = 0) -> str:
        deadline = time.monotonic() + timeout
        with self.condition:
            while marker not in self.output[since:]:
                if self.process.poll() is not None:
                    raise G0Error(
                        f"QEMU exited with {self.process.returncode} while waiting for {marker!r}"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise G0Error(f"timed out waiting for guest marker {marker!r}")
                self.condition.wait(min(remaining, 0.25))
            return self.output[since:]

    def wait_regex(
        self, pattern: re.Pattern[str], timeout: float, *, since: int = 0
    ) -> re.Match[str]:
        deadline = time.monotonic() + timeout
        with self.condition:
            match = pattern.search(self.output, since)
            while match is None:
                if self.process.poll() is not None:
                    raise G0Error(
                        f"QEMU exited with {self.process.returncode} while waiting for "
                        f"pattern {pattern.pattern!r}"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"timed out waiting for guest pattern {pattern.pattern!r}"
                    )
                self.condition.wait(min(remaining, 0.25))
                match = pattern.search(self.output, since)
            return match

    def send(self, line: str) -> None:
        if self.process.poll() is not None or self.process.stdin is None:
            raise G0Error("cannot write to a stopped QEMU console")
        self.process.stdin.write((line + "\n").encode())
        self.process.stdin.flush()

    def uboot_command(self, command: str, timeout: float) -> str:
        start = len(self.output)
        self.send(command)
        return self.wait("=> ", timeout, since=start)

    def shell_command(self, command: str, timeout: float) -> str:
        token = uuid.uuid4().hex
        marker = f"HF3FS_G0_COMMAND token={token} rc="
        start = len(self.output)
        self.send(
            f"{command}; hf3fs_command_rc=$?; "
            f"printf 'HF3FS_G0_%s token=%s rc=%s\\n' COMMAND {token} $hf3fs_command_rc"
        )
        match = self.wait_regex(
            re.compile(re.escape(marker) + r"([0-9]+)"), timeout, since=start
        )
        with self.condition:
            captured = self.output[start : match.end()]
        if int(match.group(1)) != 0:
            raise G0Error(f"guest command failed with rc={match.group(1)}: {command}\n{captured}")
        return captured[: match.start() - start]

    def stop(self, timeout: float = 15) -> None:
        if self.process.poll() is None:
            try:
                self.send("sync; poweroff -f")
                self.process.wait(timeout=timeout)
            except (G0Error, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                    self.process.wait(timeout=5)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    if self.process.poll() is None:
                        os.killpg(self.process.pid, signal.SIGKILL)
                        self.process.wait(timeout=5)
        self.stop_ns = time.monotonic_ns()
        if self.process.stdin is not None:
            self.process.stdin.close()
        self.reader.join(timeout=5)
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self.reader.is_alive():
            self.reader.join(timeout=5)
        self.log.close()

    def record(self, host_id: int, backing: Path, readiness: Mapping[str, Any]) -> dict[str, Any]:
        status = backing.stat()
        return {
            "host_id": host_id,
            "backing": str(backing),
            "backing_inode": [status.st_dev, status.st_ino],
            "backing_sha256": sha256_file(backing),
            "qemu_start_ns": self.start_ns,
            "qemu_stop_ns": self.stop_ns,
            "qemu_returncode": self.process.returncode,
            "readiness": dict(readiness),
            "console_log": str(self.log_path),
        }


def _require_paths(paths: PlatformPaths) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name, path in vars(paths).items():
        if not path.is_absolute():
            raise G0Error(f"platform path is not absolute: {name}")
        actual = path.resolve(strict=True)
        if not actual.is_file():
            raise G0Error(f"platform artifact is not a file: {name}")
        if name in {"qemu", "cxlmemsim_server"} and not os.access(actual, os.X_OK):
            raise G0Error(f"platform artifact is not executable: {name}")
        result[name] = {"path": str(actual), "sha256": sha256_file(actual)}
    return result


def _require_output_root(path: Path, profile: str = "platform-smoke") -> Path:
    if not path.is_absolute():
        raise G0Error("G0 output root must be absolute")
    project = PROJECT_ROOT.resolve(strict=True)
    output = path.resolve(strict=False)
    approved = project / "out/cxl-riscv" / ("g0-b" if profile == "full-3fs" else "g0-a")
    try:
        output.relative_to(approved)
    except ValueError as error:
        raise G0Error(f"G0 output root escapes {approved}") from error
    output.mkdir(parents=True, exist_ok=True)
    return output


def _create_sparse(path: Path, size: int, poison: int) -> None:
    with path.open("xb") as output:
        output.truncate(size)
        output.seek(0)
        output.write(bytes([poison]) * 4096)
        output.flush()
        os.fsync(output.fileno())


def _wait_for_log(path: Path, marker: str, process: subprocess.Popen[Any], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise G0Error(f"CXLMemSim exited before readiness with {process.returncode}")
        try:
            if marker in path.read_text(encoding="utf-8", errors="replace"):
                return
        except FileNotFoundError:
            pass
        time.sleep(0.05)
    raise G0Error(f"timed out waiting for CXLMemSim marker {marker!r}")


def _boot_guest(console: GuestConsole, paths: PlatformPaths, host_id: int, timeout: float,
                capacity: int = CXL_CAPACITY, require_config_disk: bool = False) -> dict[str, Any]:
    console.wait("Hit any key to stop autoboot", timeout)
    console.send("")
    console.wait("=> ", timeout)
    listing = console.uboot_command("cxl list", timeout)
    if "41.00.0" not in listing or "Type 3" not in listing:
        raise G0Error(f"guest {host_id} U-Boot cxl list is incomplete")
    information = console.uboot_command("cxl info 41.00.0", timeout)
    if f"{capacity:016x}" not in information:
        raise G0Error(f"guest {host_id} U-Boot cxl info is incomplete")
    initialized = console.uboot_command("cxl init", timeout)
    host_decoder = HOST_DECODER.replace(f"{CXL_CAPACITY:016x}", f"{capacity:016x}")
    type3_decoder = TYPE3_DECODER.replace(f"{CXL_CAPACITY:016x}", f"{capacity:016x}")
    if host_decoder not in initialized or type3_decoder not in initialized:
        raise G0Error(f"guest {host_id} U-Boot cxl init is incomplete")
    bootargs = (
        "earlycon=sbi console=hvc0 loglevel=6 panic=-1 cxl_core.pmem_as_dax=1 "
        "root=/dev/vda rw rootwait init=/init "
        f"hf3fs.host_id={host_id}"
        + (" hf3fs.config_disk=required" if require_config_disk else "")
    )
    console.uboot_command(f"setenv bootargs '{bootargs}'", timeout)
    boot_start = len(console.output)
    console.send(f"bootefi 90000000:{paths.kernel.stat().st_size:x} ${{fdtcontroladdr}}")
    marker = f"HF3FS_G0_GUEST_READY host_id={host_id}"
    match = console.wait_regex(
        re.compile(
            re.escape(marker)
            + r" dax=([^ \r\n]+) size=([0-9]+) align=([0-9]+) fuse=(true|false)"
        ),
        timeout,
        since=boot_start,
    )
    device, size, alignment, fuse = match.groups()
    record = {
        "device": device,
        "size": int(size),
        "alignment": int(alignment),
        "fuse_device": fuse == "true",
    }
    if record["size"] != capacity or record["alignment"] <= 0 or not record["fuse_device"]:
        raise G0Error(f"guest {host_id} DAX/FUSE readiness is incomplete")
    return record


def _load_rootfs_manifest(path: Path) -> tuple[Path, dict[str, Any]]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise G0Error(f"cannot read rootfs manifest: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("schema") != "hf3fs.cxl-riscv-rootfs.v1":
        raise G0Error("rootfs manifest schema is unsupported")
    root = Path(str(manifest.get("root", ""))).resolve(strict=True)
    if not root.is_dir():
        raise G0Error("rootfs manifest does not name a directory")
    binaries = manifest.get("binaries")
    if not isinstance(binaries, dict):
        raise G0Error("rootfs manifest binary evidence is absent")
    for name in PLATFORM_SMOKE_BINARIES:
        record = binaries.get(name)
        if not isinstance(record, dict):
            raise G0Error(f"rootfs manifest lacks {name}")
        artifact = Path(str(record.get("path", ""))).resolve(strict=True)
        try:
            artifact.relative_to(root)
        except ValueError as error:
            raise G0Error(f"rootfs binary escapes the root: {name}") from error
        if sha256_file(artifact) != record.get("sha256"):
            raise G0Error(f"rootfs binary hash differs: {name}")
    for relative, digest in manifest.get("runtime_files", {}).items():
        artifact = (root / relative).resolve(strict=True)
        if not artifact.is_relative_to(root) or sha256_file(artifact) != digest:
            raise G0Error(f"rootfs runtime file changed: {relative}")
    return root, manifest


def _qemu_environment(paths: PlatformPaths) -> dict[str, str]:
    environment = dict(os.environ)
    for name in ("CXL_TRANSPORT_MODE", "CXL_PGAS_SHM", "CXL_MEMSIM_SERVER"):
        environment.pop(name, None)
    environment["PATH"] = str(paths.qemu.parent) + os.pathsep + environment.get("PATH", "")
    return environment


def _atomic_write_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("x", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def write_full_handoff(result_path: Path, handoff_path: Path) -> None:
    """Publish a passing immutable G0-B reference without replacing evidence."""
    result_path = result_path.resolve(strict=True)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if result.get("status") != "passed" or result.get("first_failure") or validate_result(result, "full-3fs"):
        raise G0Error("cannot publish a handoff for a failed or incomplete G0-B result")
    reference = {
        "schema": "hf3fs.cxl-riscv-g0-b-handoff.v1",
        "result_path": str(result_path),
        "result_sha256": sha256_file(result_path),
        "platform_contract_sha256": result["platform_contract_sha256"],
        "build_manifest_sha256": result["build_manifest_sha256"],
        "source_closure_sha256": result["source_closure_sha256"],
    }
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = handoff_path.with_name(f".{handoff_path.name}.new-{os.getpid()}")
    _atomic_write_json(temporary, reference)
    try:
        try:
            os.link(temporary, handoff_path)
        except FileExistsError:
            if json.loads(handoff_path.read_text(encoding="utf-8")) != reference:
                raise G0Error(f"refusing to replace a different existing G0-B handoff: {handoff_path}")
    finally:
        temporary.unlink()


def execute(
    *,
    paths: PlatformPaths,
    rootfs_manifest: Path,
    kernel_config: Path,
    output_root: Path,
    config: RuntimeConfig,
    image_bytes: int,
    profile: str = "platform-smoke",
) -> Path:
    platform_artifacts = _require_paths(paths)
    rootfs, rootfs_record = _load_rootfs_manifest(rootfs_manifest)
    if profile == "full-3fs":
        import full_stack
        import stage_full_rootfs
        if rootfs_record.get("profile") != "full-3fs":
            raise G0Error("full-3fs requires a staged full application rootfs")
        build_manifest = Path(rootfs_record["build_manifest"])
        source_closure = Path(rootfs_record["source_closure"])
        if (sha256_file(build_manifest) != rootfs_record["build_manifest_sha256"] or
                sha256_file(source_closure) != rootfs_record["source_closure_sha256"]):
            raise G0Error("staged full application build/source identities changed")
        stage_full_rootfs.verify_build(build_manifest, source_closure)
        for name in full_stack.APPLICATIONS:
            artifact = rootfs_record["binaries"][name]
            path = Path(artifact["path"]).resolve(strict=True)
            if not path.is_relative_to(rootfs) or sha256_file(path) != artifact["sha256"]:
                raise G0Error(f"staged full application changed: {name}")
    kernel_errors = guest_image.validate_kernel_config(kernel_config)
    if kernel_errors:
        raise G0Error("guest kernel config failed validation: " + "; ".join(kernel_errors))
    output = _require_output_root(output_root, profile)
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{os.getpid()}"
    run = output / run_id
    run.mkdir(mode=0o700)
    result_path = run / "result.json"
    result: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "gate": "G0-B" if profile == "full-3fs" else "G0-A",
        "profile": profile,
        "status": "failed",
        "run_id": run_id,
        "first_failure": None,
        "platform_artifacts": platform_artifacts,
        "binaries": rootfs_record["binaries"],
        "qemu_commands": [],
        "teardown": {"all_owned_processes_stopped": False},
    }
    consoles: list[GuestConsole] = []
    cxl_process: subprocess.Popen[Any] | None = None
    cxl_log_handle: Any = None
    reservation = PortReservation()
    network_reservation = PortReservation() if profile == "full-3fs" else None
    endpoint_paths = [run / f"node{node}-cxl.raw" for node in (0, 1)]
    lsa_paths = [run / f"node{node}-lsa.raw" for node in (0, 1)]
    config_images = [run / f"node{node}-config.ext4" for node in (0, 1)]
    trace_enabled = profile == "platform-smoke"
    trace = run / "coherence.jsonl.zst"
    trace_fifo = Path("/dev/shm") / f"hf3fs-cxl-{run_id}.fifo"
    trace_capture: CompressedTrace | None = None
    central = run / "cxl-authority.raw"
    cxl_log = run / "cxlmemsim.log"
    try:
        guest_roots = [rootfs, rootfs]
        if profile == "full-3fs":
            result["build_manifest_sha256"] = rootfs_record["build_manifest_sha256"]
            result["source_closure_sha256"] = rootfs_record["source_closure_sha256"]
            guest_roots, result["full_stack_configuration"] = full_stack.stage_roots(rootfs, run, time.time_ns())
        cache_key, cache_content = guest_image.rootfs_image_identity(rootfs, rootfs_record, image_bytes)
        cache_directory = PROJECT_ROOT.resolve() / "out/cxl-riscv/image-cache"
        base_image = cache_directory / f"{cache_key}.ext4"
        base_record, cache_hit = guest_image.ensure_cached_image(
            rootfs=rootfs, output=base_image, image_bytes=image_bytes,
            truncate=Path("/usr/bin/truncate"), mkfs_ext4=Path("/usr/sbin/mkfs.ext4"),
            e2fsck=Path("/usr/sbin/e2fsck"),
        )
        result["image_cache"] = {
            "schema": "hf3fs.cxl-riscv-image-cache.v1", "key": cache_key,
            "cache_hit": cache_hit, "image": str(base_image), "sha256": base_record["sha256"],
            "bytes": base_record["bytes"], "content": cache_content,
            "base_receipt_before": guest_image.immutable_image_receipt(base_image),
            "root_policy": "immutable-shared-base-with-qemu-temporary-snapshot",
        }
        for node in (0, 1):
            if profile == "full-3fs":
                patches = {
                    f"{full_stack.CONFIG}/{name}":
                    guest_roots[node] / full_stack.CONFIG.removeprefix("/") / name
                    for name in result["full_stack_configuration"]["files"]
                }
                guest_image.create_config_image(
                    output=config_images[node], patches=patches, image_bytes=16 * 1024**2,
                    truncate=Path("/usr/bin/truncate"), mkfs_ext4=Path("/usr/sbin/mkfs.ext4"),
                    e2fsck=Path("/usr/sbin/e2fsck"),
                )
            _create_sparse(endpoint_paths[node], CXL_CAPACITY, 0x31 + node)
            _create_sparse(lsa_paths[node], LSA_CAPACITY, 0x71 + node)
        if profile == "full-3fs":
            result["image_cache"]["config_images"] = [
                str(path.with_suffix(path.suffix + ".manifest.json")) for path in config_images]
        _create_sparse(central, CXL_CAPACITY, 0xA5)

        if trace_enabled:
            trace_capture = CompressedTrace(trace_fifo, trace)
        server_command = build_server_command(
            paths,
            coherence_port=reservation.port,
            trace=trace_fifo if trace_enabled else None,
            backing=central,
        )
        platform_contract = {
            "server_command": server_command,
            "hpa_base": CXL_HPA_BASE,
            "capacity": CXL_CAPACITY,
            "endpoint_identity_policy": "distinct-st_dev-st_ino-and-distinct-poison",
            "root_disk_policy": "shared-immutable-base-qemu-snapshot",
            "trace_policy": (
                "complete-fifo-zstd"
                if trace_enabled
                else "final-aggregate-stats-plus-bidirectional-dax-records"
            ),
        }
        result["platform_contract"] = platform_contract
        result["platform_contract_sha256"] = canonical_json_hash(platform_contract)
        cxl_log_handle = cxl_log.open("w", encoding="utf-8")
        reservation.release()
        cxl_process = subprocess.Popen(
            server_command,
            stdout=cxl_log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        _wait_for_log(cxl_log, "Server listening on TCP port", cxl_process, config.timeout_seconds)
        if trace_capture is not None:
            trace_capture.release_guard()

        qemu_commands = [
            build_qemu_command(
                paths,
                config,
                node=node,
                coherence_port=int(server_command[2].split("=", 1)[1]),
                root_image=base_image,
                root_snapshot=True,
                config_image=config_images[node] if profile == "full-3fs" else None,
                endpoint_memory=endpoint_paths[node],
                lsa=lsa_paths[node],
            )
            for node in (0, 1)
        ]
        result["qemu_commands"] = qemu_commands
        if network_reservation is not None:
            for node in (0, 1):
                qemu_commands[node].extend(full_stack.network_args(node, network_reservation.port))
            network_reservation.release()
        environment = _qemu_environment(paths)
        for node in (0, 1):
            console = GuestConsole(
                qemu_commands[node], run / f"node{node}-console.log", environment
            )
            consoles.append(console)
        with ThreadPoolExecutor(max_workers=2) as pool:
            readiness = list(pool.map(
                lambda item: _boot_guest(item[1], paths, item[0], config.timeout_seconds,
                                         require_config_disk=profile == "full-3fs"),
                enumerate(consoles),
            ))
        if any(console.process.poll() is not None for console in consoles):
            raise G0Error("both QEMU guests must remain alive before the DAX test")
        if readiness[0]["size"] != readiness[1]["size"] or readiness[0]["alignment"] != readiness[1]["alignment"]:
            raise G0Error("guest DAX geometry differs")
        dax_length = readiness[0]["alignment"]
        if dax_length <= 0 or dax_length > readiness[0]["size"]:
            raise G0Error("guest DAX aligned smoke length is invalid")

        directions: list[dict[str, Any]] = []
        for writer_host, reader_host, generation in ((0, 1, 1), (1, 0, 2)):
            command = (
                "/opt/3fs/bin/cxl_dax_smoke "
                f"--device {readiness[writer_host]['device']} --offset 0 --length {dax_length} "
                f"--payload-bytes {DAX_PROBE_PAYLOAD_BYTES} "
                f"--role writer --generation {generation} --timeout-ms 30000"
            )
            writer = _require_probe_record(
                consoles[writer_host].shell_command(command, config.timeout_seconds),
                f"DAX writer host {writer_host}",
            )
            command = (
                "/opt/3fs/bin/cxl_dax_smoke "
                f"--device {readiness[reader_host]['device']} --offset 0 --length {dax_length} "
                f"--payload-bytes {DAX_PROBE_PAYLOAD_BYTES} "
                f"--role reader --generation {generation} --timeout-ms 30000"
            )
            reader = _require_probe_record(
                consoles[reader_host].shell_command(command, config.timeout_seconds),
                f"DAX reader host {reader_host}",
            )
            directions.append(
                {
                    "writer_host": writer_host,
                    "reader_host": reader_host,
                    "writer": writer,
                    "reader": reader,
                }
            )

        # Persist each completed gate immediately so a later failure cannot
        # erase already-proven DAX or FUSE evidence from the result artifact.
        result["dax"] = {
            "length": dax_length,
            "directions": directions,
            "qemu_lifetimes_overlap": True,
        }

        fuse = _require_probe_record(
            consoles[0].shell_command(
                "mkdir -p /tmp/hf3fs-g0-fuse && /opt/3fs/bin/fuse_mount_smoke --mountpoint /tmp/hf3fs-g0-fuse",
                config.timeout_seconds,
            ),
            "FUSE guest probe",
        )
        result["fuse"] = fuse

        cluster = "hf3fsg0:hf3fsg0@127.0.0.1:4500"
        consoles[0].shell_command(
            "mkdir -p /opt/3fs/etc /var/lib/fdb /var/log/fdb && "
            f"printf '%s\\n' '{cluster}' > /opt/3fs/etc/fdb.cluster",
            config.timeout_seconds,
        )
        version_output = consoles[0].shell_command(
            "/opt/foundationdb/bin/fdbserver --version", config.timeout_seconds
        )
        # Full-stack startup is CPU-heavy under TCG.  Give its machine-local
        # FDB the same finite transaction/liveness window and dedicated guest
        # vCPU already proven by the ten-client cohort.
        consoles[0].shell_command(
            fdb_server_command(clients=10 if profile == "full-3fs" else 1),
            config.timeout_seconds,
        )
        consoles[0].shell_command(
            fdb_readiness_command(),
            config.timeout_seconds,
        )
        configure_output = consoles[0].shell_command(
            "attempt=0; while [ $attempt -lt 120 ]; do "
            "/opt/foundationdb/bin/fdbcli -C /opt/3fs/etc/fdb.cluster "
            "--exec 'configure new single memory' && break; "
            "attempt=$((attempt + 1)); sleep 1; done; [ $attempt -lt 120 ]",
            config.timeout_seconds,
        )
        fdb = _require_probe_record(
            consoles[0].shell_command(
                "/opt/3fs/bin/fdb_client_smoke --cluster-file /opt/3fs/etc/fdb.cluster "
                f"--key {run_id} --value riscv-fdb-ok",
                60,
            ),
            "FoundationDB guest transaction",
        )
        fdb.update(
            {
                "guest_server": True,
                "server_version_output": version_output.strip(),
                "configure_output": configure_output.strip(),
                "cluster_file_sha256": hashlib.sha256((cluster + "\n").encode()).hexdigest(),
                "bind_address": "127.0.0.1:4500",
            }
        )
        result["fdb"] = fdb
        if profile == "full-3fs":
            result["full_stack"] = full_stack.execute(consoles, run, config.timeout_seconds)
            if result["full_stack"].get("first_failure"):
                result["first_failure"] = result["full_stack"]["first_failure"]
        if 0 not in result.get("full_stack", {}).get("unresponsive_guests", []):
            consoles[0].shell_command(
                "kill $(cat /run/fdbserver.pid); attempt=0; while kill -0 $(cat /run/fdbserver.pid) 2>/dev/null; "
                "do attempt=$((attempt + 1)); [ $attempt -lt 100 ] || exit 1; sleep 0.1; done",
                20,
            )

        result["fdb"] = fdb
    except Exception as error:
        result["first_failure"] = str(error)
    finally:
        reservation.release()
        if network_reservation is not None:
            network_reservation.release()
        def stop_console(console: GuestConsole) -> None:
            try:
                console.stop()
            except Exception as error:
                if result["first_failure"] is None:
                    result["first_failure"] = f"guest teardown failed: {error}"
        with ThreadPoolExecutor(max_workers=max(1, len(consoles))) as pool:
            list(pool.map(stop_console, reversed(consoles)))
        if cxl_process is not None and cxl_process.poll() is None:
            try:
                os.killpg(cxl_process.pid, signal.SIGINT)
                cxl_process.wait(timeout=15)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if cxl_process.poll() is None:
                    os.killpg(cxl_process.pid, signal.SIGKILL)
                    cxl_process.wait(timeout=5)
        if cxl_log_handle is not None:
            cxl_log_handle.close()
        if trace_capture is not None:
            trace_returncode = trace_capture.finish()
            result["trace_capture"] = {
                "mode": "fifo-zstd-complete", "archive": str(trace),
                "compressor_returncode": trace_returncode, "fifo_removed": not trace_fifo.exists(),
            }
            if trace_returncode and result["first_failure"] is None:
                result["first_failure"] = f"trace compressor exited {trace_returncode}"
        if "image_cache" in result:
            result["image_cache"]["base_receipt_after"] = guest_image.immutable_image_receipt(base_image)
            if result["image_cache"]["base_receipt_before"] != result["image_cache"]["base_receipt_after"]:
                result["first_failure"] = result["first_failure"] or "immutable base image changed during QEMU snapshots"
        result["teardown"] = {
            "all_owned_processes_stopped": all(
                console.process.poll() is not None for console in consoles
            )
            and (cxl_process is None or cxl_process.poll() is not None),
            "qemu_returncodes": [console.process.returncode for console in consoles],
            "cxlmemsim_returncode": None if cxl_process is None else cxl_process.returncode,
        }

    if "dax" in result:
        result["dax"]["guests"] = [
            consoles[node].record(node, endpoint_paths[node], readiness[node])
            for node in range(len(consoles))
        ]
        lifetimes = result["dax"]["guests"]
        result["dax"]["qemu_lifetimes_overlap"] = (
            len(lifetimes) == 2
            and max(item["qemu_start_ns"] for item in lifetimes)
            < min(item["qemu_stop_ns"] for item in lifetimes)
        )
        try:
            if trace_enabled:
                result["dax"]["coherence"] = analyze_trace(
                    trace, int(result["dax"]["length"])
                )
            else:
                result["dax"]["coherence"] = parse_coherence_stats(
                    cxl_log, expected_registrations=2
                )
        except Exception as error:
            result["dax"]["coherence"] = {"complete": False, "error": str(error)}
            if result["first_failure"] is None:
                result["first_failure"] = str(error)
    if cxl_log.is_file():
        result["cxlmemsim_log"] = {"path": str(cxl_log), "sha256": sha256_file(cxl_log)}
    errors = validate_result(result, profile)
    result["validation_errors"] = errors
    if not errors and result["first_failure"] is None:
        result["status"] = "passed"
    _atomic_write_json(result_path, result)
    return result_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-result", type=Path)
    parser.add_argument("--profile", choices=("platform-smoke", "full-3fs"), default="platform-smoke")
    parser.add_argument("--qemu", type=Path)
    parser.add_argument("--cxlmemsim-server", type=Path)
    parser.add_argument("--topology", type=Path)
    parser.add_argument("--opensbi", type=Path)
    parser.add_argument("--uboot", type=Path)
    parser.add_argument("--kernel", type=Path)
    parser.add_argument("--kernel-config", type=Path)
    parser.add_argument("--rootfs-manifest", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--guest-memory", default="2G")
    parser.add_argument("--smp", type=int, default=5)
    parser.add_argument("--image-bytes", type=int, default=3 * 1024**3)
    arguments = parser.parse_args(argv)
    if arguments.validate_result is not None:
        try:
            result = json.loads(arguments.validate_result.read_text(encoding="utf-8"))
            errors = validate_result(result, arguments.profile)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            print(f"cannot validate G0 result: {error}", file=sys.stderr)
            return 2
        print(json.dumps({"status": "passed" if not errors else "failed", "errors": errors}, indent=2))
        return 0 if not errors else 2

    required = (
        "qemu",
        "cxlmemsim_server",
        "topology",
        "opensbi",
        "uboot",
        "kernel",
        "kernel_config",
        "rootfs_manifest",
        "output_root",
    )
    missing = [name for name in required if getattr(arguments, name) is None]
    if missing:
        parser.error("missing runtime arguments: " + ", ".join(missing))
    if arguments.timeout <= 0 or arguments.smp <= 0 or arguments.image_bytes <= 0:
        parser.error("timeout, smp and image-bytes must be positive")
    try:
        result_path = execute(
            paths=PlatformPaths(
                qemu=arguments.qemu,
                cxlmemsim_server=arguments.cxlmemsim_server,
                topology=arguments.topology,
                opensbi=arguments.opensbi,
                uboot=arguments.uboot,
                kernel=arguments.kernel,
            ),
            rootfs_manifest=arguments.rootfs_manifest,
            kernel_config=arguments.kernel_config,
            output_root=arguments.output_root,
            config=RuntimeConfig(
                guest_memory=arguments.guest_memory,
                timeout_seconds=arguments.timeout,
                smp=arguments.smp,
            ),
            image_bytes=arguments.image_bytes,
            profile=arguments.profile,
        )
    except (G0Error, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"G0 execution failed before result creation: {error}", file=sys.stderr)
        return 2
    print(result_path)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if result.get("status") == "passed" and arguments.profile == "full-3fs":
        try:
            handoff = Path(__file__).resolve().parents[2] / "out/cxl-riscv/g0-b/g0-b-handoff.json"
            write_full_handoff(result_path, handoff)
            print(handoff)
        except (G0Error, OSError, ValueError) as error:
            print(f"G0-B passed but handoff publication failed: {error}", file=sys.stderr)
            return 2
    return 0 if result.get("status") == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
