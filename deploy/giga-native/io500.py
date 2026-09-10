#!/usr/bin/env python3
"""Run and validate the expected-INVALID 22-phase IO500 workload."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import sys
import time

import topology


EXPECTED_PHASES = topology.EXPECTED_PHASES
RESULT_RE = re.compile(
    r"\[(?:RESULT| +)\]\s+(\S+)\s+(\S+)\s+(\S+)\s+:\s+time\s+(\S+)\s+seconds"
)
FATAL_RE = re.compile(
    r"(?i)(?:^|\n).*(?:ERROR:|FATAL|assertion .* failed|MPI_ABORT|Input/output error|Remote I/O error)"
)


class IO500Error(RuntimeError):
    pass


class PhaseWatchdog:
    def __init__(self, phase_timeout_seconds: float = 120.0, banner_timeout_seconds: float = 60.0):
        self.phase_timeout_ns = int(phase_timeout_seconds * 1e9)
        self.banner_timeout_ns = int(banner_timeout_seconds * 1e9)
        self.created_ns: int | None = None
        self.deadline_ns: int | None = None
        self.buffer = ""
        self.full_text = ""
        self.records: list[dict] = []
        self.banner = False

    def feed(self, text: str, host_ns: int | None = None) -> None:
        host_ns = time.monotonic_ns() if host_ns is None else host_ns
        if self.created_ns is None:
            self.created_ns = host_ns
            self.deadline_ns = host_ns + self.banner_timeout_ns
        self.full_text += text
        if FATAL_RE.search(self.full_text):
            match = FATAL_RE.search(self.full_text)
            assert match is not None
            raise IO500Error("fatal IO500 output: " + match.group(0).strip())
        self.buffer += text
        lines = self.buffer.splitlines(keepends=True)
        self.buffer = ""
        if lines and not lines[-1].endswith(("\n", "\r")):
            self.buffer = lines.pop()
        for line in lines:
            if "IO500 version " in line and not self.banner:
                self.banner = True
                self.deadline_ns = host_ns + self.phase_timeout_ns
            match = RESULT_RE.search(line)
            if match is None or match[1] not in EXPECTED_PHASES:
                continue
            expected = EXPECTED_PHASES[len(self.records)] if len(self.records) < len(EXPECTED_PHASES) else None
            if match[1] != expected:
                raise IO500Error(f"duplicate or out-of-order IO500 phase: {match[1]} expected {expected}")
            value = float(match[2])
            seconds = float(match[4])
            if not math.isfinite(value) or value < 0 or not math.isfinite(seconds) or seconds <= 0:
                raise IO500Error(f"invalid IO500 phase values: {line.strip()}")
            self.records.append({
                "phase": match[1], "value": value, "unit": match[3], "seconds": seconds,
                "host_monotonic_ns": host_ns,
            })
            self.deadline_ns = host_ns + self.phase_timeout_ns
        if self.deadline_ns is not None and host_ns > self.deadline_ns:
            stage = "banner" if not self.banner else EXPECTED_PHASES[len(self.records)]
            raise IO500Error(f"IO500 {stage} exceeded liveness deadline")

    def finish(self) -> None:
        if self.buffer:
            self.feed("\n", self.deadline_ns - 1 if self.deadline_ns else None)
        if not self.banner:
            raise IO500Error("IO500 version banner is absent")
        actual = tuple(row["phase"] for row in self.records)
        if actual != EXPECTED_PHASES:
            raise IO500Error(f"IO500 phases are incomplete: {len(actual)}/22")


def parse_result(path: Path, ranks: int) -> dict:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    sections = re.findall(r"(?m)^\[([^]]+)\]\s*$", text)
    missing = [phase for phase in EXPECTED_PHASES if phase not in sections]
    if missing:
        raise IO500Error(f"IO500 result lacks phases: {missing}")
    procs = re.search(r"(?m)^procs\s*=\s*(\d+)\s*$", text)
    if procs is None or int(procs[1]) != ranks:
        raise IO500Error("IO500 result rank count differs from topology")
    if "INVALID" not in text:
        raise IO500Error("bounded IO500 result unexpectedly lacks INVALID")
    return {"sections": sections, "ranks": ranks, "expected_invalid": True}


def parse_verifier(returncode: int, stdout: str, stderr: str) -> dict:
    combined = stdout + "\n" + stderr
    diagnosis = "[OK] But this is an invalid run!" in combined
    if returncode != 1 or not diagnosis:
        raise IO500Error(
            f"verifier did not confirm the expected-invalid package: rc={returncode}"
        )
    return {"returncode": returncode, "expected_invalid_diagnosis": True, "output": combined}


def validate_rank_receipts(directory: Path, cpus: tuple[int, ...], mounts: list[Path], endpoints: tuple[int, ...]) -> list[dict]:
    records = []
    for rank in range(len(cpus)):
        path = directory / f"rank-{rank}.json"
        if not path.is_file():
            raise IO500Error(f"rank receipt is absent: {rank}")
        value = json.loads(path.read_text())
        expected = {
            "schema": "hf3fs.giga-native-rank.v1",
            "rank": rank,
            "cpu": cpus[rank],
            "allowed_cpus": [cpus[rank]],
            "mount": str(mounts[rank].resolve()),
            "endpoint": endpoints[rank],
            "returncode": 0,
        }
        if any(value.get(key) != item for key, item in expected.items()):
            raise IO500Error(f"rank {rank} placement or exit differs from topology")
        records.append(value)
    return records


def archive_results(source: Path, destination: Path) -> None:
    """Copy the IO500 result directory out of FUSE before cluster teardown."""
    if not source.is_dir():
        return
    shutil.copytree(source, destination, dirs_exist_ok=True)


def archive_results_after_failure(source: Path, destination: Path, note: Path) -> None:
    try:
        archive_results(source, destination)
    except Exception as error:
        note.write_text(f"failed to archive IO500 diagnostics: {error}\n")


def io500_paths(cluster) -> tuple[Path, Path, Path]:
    """Return config, mounted data, and external diagnostic result paths."""
    common_view = cluster.roots.volatile / "io500-view"
    return (
        cluster.roots.bundle / "io500-effective.ini",
        common_view / "io500",
        cluster.roots.bundle / "io500-results",
    )


def run(cluster) -> dict:
    receipts = cluster.roots.bundle / "ranks"
    receipts.mkdir()
    common_view = cluster.roots.volatile / "io500-view"
    common_view.mkdir()
    effective, datadir, resultdir = io500_paths(cluster)
    resultdir.mkdir()
    topology.render_io500_config(cluster.profile, effective, datadir, resultdir, cluster.topology.ranks)
    rank_script = Path(__file__).with_name("rank.py")
    endpoints = tuple(16 + rank for rank in range(cluster.topology.ranks))
    argv = [
        "/usr/bin/mpirun.openmpi", "--allow-run-as-root", "--oversubscribe",
        "--bind-to", "none", "--map-by", "slot", "-np", str(cluster.topology.ranks),
        "/usr/bin/unshare", "--mount", "--propagation", "private",
        sys.executable, str(rank_script),
        "--client-cpus", ",".join(map(str, cluster.topology.client_cpus)),
        "--client-mounts", ",".join(map(str, cluster.mounts)),
        "--client-endpoints", ",".join(map(str, endpoints)),
        "--common-view", str(common_view), "--receipt-dir", str(receipts),
        "--io500", str(cluster.artifacts["io500"]), "--config", str(effective),
        "--library-path", cluster.library_path,
    ]
    log_path = cluster.roots.bundle / "io500.log"
    watchdog = PhaseWatchdog()
    process = subprocess.Popen(
        argv,
        cwd=str(cluster.repo / "target/build/giga-native-3fs"),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    started = time.monotonic_ns()
    try:
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        with log_path.open("w", encoding="utf-8") as log:
            while True:
                now = time.monotonic_ns()
                events = selector.select(timeout=0.1)
                if not events:
                    watchdog.feed("", now)
                    if process.poll() is not None:
                        remainder = process.stdout.read()
                        if remainder:
                            log.write(remainder)
                            log.flush()
                            watchdog.feed(remainder, time.monotonic_ns())
                        break
                    continue
                chunk = os.read(process.stdout.fileno(), 65536).decode(errors="replace")
                if chunk:
                    log.write(chunk)
                    log.flush()
                    watchdog.feed(chunk, now)
                elif process.poll() is not None:
                    break
        selector.close()
        returncode = process.wait()
        if returncode != 0:
            raise IO500Error(f"MPI/IO500 exited with {returncode}")
        watchdog.finish()
    except Exception:
        if process.poll() is None:
            os.killpg(process.pid, 15)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, 9)
                process.wait()
        raise
    result_path = resultdir / "result.txt"
    config_path = resultdir / "config.ini"
    result = parse_result(result_path, cluster.topology.ranks)
    verifier = subprocess.run(
        [str(cluster.artifacts["io500-verify"]), str(config_path), str(result_path), "1"],
        cwd=str(cluster.artifacts["io500"].parent),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
    )
    verified = parse_verifier(verifier.returncode, verifier.stdout, verifier.stderr)
    (cluster.roots.bundle / "io500-verify.log").write_text(verified["output"])
    rank_records = validate_rank_receipts(receipts, cluster.topology.client_cpus, cluster.mounts, endpoints)
    return {
        "status": "passed", "official": False, "expected_invalid": True,
        "command": argv, "returncode": returncode,
        "host_start_ns": started, "host_end_ns": time.monotonic_ns(),
        "phases": watchdog.records, "result": result,
        "verifier": {key: value for key, value in verified.items() if key != "output"},
        "ranks": rank_records,
    }
