#!/usr/bin/env python3
"""Exact-owned process, path and evidence primitives for native runs."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import time
from typing import Mapping, Sequence, TextIO


class OwnershipError(RuntimeError):
    pass


def atomic_json(path: Path, value: object) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as destination:
        json.dump(value, destination, indent=2, sort_keys=True)
        destination.write("\n")
        destination.flush()
        os.fsync(destination.fileno())
    os.replace(temporary, path)


def sha256(path: Path) -> str:
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def validate_run_id(run_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
        raise ValueError("run ID must be a safe nonempty path component")


@dataclass(frozen=True)
class RunRoots:
    run_id: str
    repo: Path
    volatile: Path
    storage: Path
    bundle: Path
    owner_token: str

    @property
    def result(self) -> Path:
        return self.bundle / "result.json"

    @classmethod
    def create(
        cls,
        repo: Path,
        run_id: str,
        *,
        volatile_parent: Path = Path("/dev/shm"),
        storage_parent: Path = Path("/mnt/nvme_test"),
        result_parent: Path | None = None,
    ) -> "RunRoots":
        validate_run_id(run_id)
        repo = Path(repo).resolve()
        result_parent = (
            repo / "target/results/giga-native-3fs" if result_parent is None else Path(result_parent)
        ).resolve()
        parents = (Path(volatile_parent).resolve(), Path(storage_parent).resolve(), result_parent)
        names = (f"hf3fs-{run_id}", f"hf3fs-{run_id}", run_id)
        paths = tuple(parent / name for parent, name in zip(parents, names))
        if any(path.exists() or path.is_symlink() for path in paths):
            raise FileExistsError(f"a run root already exists for {run_id}")
        token = secrets.token_hex(32)
        created: list[Path] = []
        try:
            for path in paths:
                path.mkdir(parents=True, exist_ok=False)
                created.append(path)
                (path / ".hf3fs-run-owner").write_text(token + "\n", encoding="utf-8")
        except Exception:
            for path in reversed(created):
                shutil.rmtree(path)
            raise
        return cls(run_id, repo, paths[0], paths[1], paths[2], token)


class ResultLedger:
    def __init__(self, path: Path, value: dict):
        self.path = Path(path)
        self.value = dict(value)
        atomic_json(self.path, self.value)

    def update(self, **values: object) -> None:
        self.value.update(values)
        atomic_json(self.path, self.value)

    def fail(self, stage: str, error: BaseException) -> None:
        item = {
            "stage": stage,
            "type": type(error).__name__,
            "message": str(error),
            "host_monotonic_ns": time.monotonic_ns(),
        }
        if "first_failure" not in self.value:
            self.value["first_failure"] = item
        else:
            self.value.setdefault("secondary_failures", []).append(item)
        self.value["status"] = "failed"
        atomic_json(self.path, self.value)


def _proc_stat(pid: int) -> tuple[int, int]:
    text = Path(f"/proc/{pid}/stat").read_text()
    suffix = text.rsplit(")", 1)[1].split()
    return int(suffix[19]), os.getpgid(pid)


def _proc_executable(pid: int) -> str:
    return str(Path(f"/proc/{pid}/exe").resolve(strict=True))


@dataclass(frozen=True)
class ProcessReceipt:
    role: str
    pid: int
    pgid: int
    start_ticks: int
    executable: str
    argv: list[str]


class OwnedProcess:
    def __init__(
        self,
        process: subprocess.Popen,
        receipt: ProcessReceipt,
        receipt_path: Path,
        log_stream: TextIO,
    ):
        self.process = process
        self.receipt = receipt
        self.receipt_path = Path(receipt_path)
        self.log_stream = log_stream

    @property
    def pid(self) -> int:
        return self.receipt.pid

    @classmethod
    def start(
        cls,
        role: str,
        argv: Sequence[str],
        log: Path,
        receipt_path: Path,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        expected_executable: Path | None = None,
    ) -> "OwnedProcess":
        if not role or not argv:
            raise ValueError("owned process requires a role and argv")
        executable = Path(expected_executable or argv[0]).resolve(strict=True)
        log = Path(log)
        log.parent.mkdir(parents=True, exist_ok=True)
        log_stream = log.open("a", encoding="utf-8")
        process = subprocess.Popen(
            list(argv),
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            env=None if env is None else dict(env),
            cwd=None if cwd is None else str(cwd),
            start_new_session=True,
            text=True,
        )
        try:
            deadline = time.monotonic() + 2
            actual_executable = _proc_executable(process.pid)
            while actual_executable != str(executable) and time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                time.sleep(0.01)
                actual_executable = _proc_executable(process.pid)
            start_ticks, pgid = _proc_stat(process.pid)
            if actual_executable != str(executable):
                raise OwnershipError(
                    f"spawned executable changed before receipt: {actual_executable} != {executable}"
                )
            receipt = ProcessReceipt(role, process.pid, pgid, start_ticks, actual_executable, list(argv))
            atomic_json(receipt_path, asdict(receipt))
            return cls(process, receipt, receipt_path, log_stream)
        except Exception:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            log_stream.close()
            raise

    def _disk_receipt(self) -> ProcessReceipt:
        value = json.loads(self.receipt_path.read_text())
        return ProcessReceipt(**value)

    def validate(self) -> ProcessReceipt:
        disk = self._disk_receipt()
        if disk != self.receipt:
            raise OwnershipError(f"process receipt changed for {self.receipt.role}")
        try:
            start_ticks, pgid = _proc_stat(disk.pid)
            executable = _proc_executable(disk.pid)
        except (FileNotFoundError, ProcessLookupError):
            if self.process.poll() is not None:
                return disk
            raise OwnershipError(f"owned process disappeared without wait: {disk.role}")
        if (start_ticks, pgid, executable) != (disk.start_ticks, disk.pgid, disk.executable):
            raise OwnershipError(f"process identity changed for {disk.role}")
        return disk

    def stop(self, term_seconds: float = 15.0, kill_seconds: float = 5.0) -> dict:
        receipt = self.validate()
        actions: list[dict] = []
        if self.process.poll() is None:
            os.killpg(receipt.pgid, signal.SIGTERM)
            actions.append({"signal": "SIGTERM", "host_monotonic_ns": time.monotonic_ns()})
            try:
                self.process.wait(timeout=term_seconds)
            except subprocess.TimeoutExpired:
                self.validate()
                os.killpg(receipt.pgid, signal.SIGKILL)
                actions.append({"signal": "SIGKILL", "host_monotonic_ns": time.monotonic_ns()})
                self.process.wait(timeout=kill_seconds)
        else:
            self.process.wait()
        self.log_stream.close()
        return {"role": receipt.role, "pid": receipt.pid, "actions": actions, "returncode": self.process.returncode}


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    if port <= 0 or port > 65535:
        raise ValueError("port is outside the TCP range")
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def numa_mapping(pid: int, region: Path) -> dict:
    region = Path(region).resolve(strict=True)
    maps = Path(f"/proc/{pid}/maps").read_text(errors="replace").splitlines()
    addresses = []
    matching_maps = []
    for line in maps:
        fields = line.split(maxsplit=5)
        if len(fields) == 6 and fields[5].endswith(str(region)):
            addresses.append(fields[0].split("-", 1)[0])
            matching_maps.append(line)
    if not addresses:
        raise ValueError(f"process {pid} does not map {region}")
    raw = []
    nodes: dict[int, int] = {}
    for line in Path(f"/proc/{pid}/numa_maps").read_text(errors="replace").splitlines():
        if line.split(maxsplit=1)[0] not in addresses:
            continue
        raw.append(line)
        for node, pages in re.findall(r"\bN(\d+)=(\d+)\b", line):
            key = int(node)
            nodes[key] = nodes.get(key, 0) + int(pages)
    if not raw or sum(nodes.values()) <= 0:
        raise ValueError(f"no allocated NUMA pages found for {region}")
    if nodes.get(1, 0) <= 0 or nodes.get(0, 0) != 0 or any(
        node not in (0, 1) and pages for node, pages in nodes.items()
    ):
        raise ValueError(f"CXL region pages are not exclusively on NUMA node 1: {nodes}")
    return {"pid": pid, "region": str(region), "pages": nodes, "maps": matching_maps, "numa_maps": raw}


def _mounts_below(path: Path) -> list[str]:
    prefix = str(path) + os.sep
    found = []
    for line in Path("/proc/self/mountinfo").read_text(errors="replace").splitlines():
        fields = line.split()
        if len(fields) > 4 and (fields[4] == str(path) or fields[4].startswith(prefix)):
            found.append(fields[4])
    return found


def safe_remove_owned_root(path: Path, allowed_parent: Path, receipt: str) -> None:
    path = Path(path)
    allowed_parent = Path(allowed_parent).resolve(strict=True)
    if path.is_symlink() or path.parent.resolve(strict=True) != allowed_parent:
        raise OwnershipError(f"refusing to remove path outside exact allowed parent: {path}")
    if path.name in ("", ".", "..") or not path.is_dir():
        raise OwnershipError(f"owned root is not a directory: {path}")
    actual = (path / ".hf3fs-run-owner").read_text().strip()
    if not secrets.compare_digest(actual, receipt):
        raise OwnershipError(f"owner receipt mismatch for {path}")
    mounts = _mounts_below(path)
    if mounts:
        raise OwnershipError(f"owned root still contains mounts: {mounts}")
    shutil.rmtree(path)
