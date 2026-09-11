#!/usr/bin/env python3
"""Bind one MPI rank to its client CPU and private view of one FUSE mount."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


SCHEMA = "hf3fs.giga-native-rank.v1"
RANK_KEYS = ("OMPI_COMM_WORLD_RANK", "PMI_RANK", "PMIX_RANK")


class RankError(RuntimeError):
    pass


def resolve_rank(environment: dict[str, str]) -> int:
    values = []
    for key in RANK_KEYS:
        value = environment.get(key)
        if value is not None:
            if not re.fullmatch(r"\d+", value):
                raise RankError(f"invalid {key}")
            values.append(int(value))
    if not values or len(set(values)) != 1:
        raise RankError("MPI rank identity is missing or inconsistent")
    return values[0]


def parse_csv(value: str, cast=str) -> tuple:
    fields = value.split(",") if value else []
    if not fields or any(not field for field in fields):
        raise RankError("rank mapping is empty or malformed")
    try:
        return tuple(cast(field) for field in fields)
    except ValueError as error:
        raise RankError("rank mapping contains an invalid value") from error


def atomic_record(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        payload = (json.dumps(value, sort_keys=True) + "\n").encode()
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def is_mountpoint(path: Path) -> bool:
    resolved = str(path.resolve(strict=True))
    for line in Path("/proc/self/mountinfo").read_text(errors="replace").splitlines():
        fields = line.split()
        if len(fields) > 4 and fields[4].replace("\\040", " ") == resolved:
            return True
    return False


def execute(args: argparse.Namespace, environment: dict[str, str]) -> int:
    rank = resolve_rank(environment)
    cpus = parse_csv(args.client_cpus, int)
    mounts = parse_csv(args.client_mounts, Path)
    endpoints = parse_csv(args.client_endpoints, int)
    if not (len(cpus) == len(mounts) == len(endpoints)) or rank >= len(cpus):
        raise RankError("rank is outside the client mapping")
    if len(set(cpus)) != len(cpus) or len(set(mounts)) != len(mounts) or len(set(endpoints)) != len(endpoints):
        raise RankError("client CPU, mount, or endpoint mapping contains duplicates")
    mount = mounts[rank].resolve(strict=True)
    if not is_mountpoint(mount):
        raise RankError(f"client mount is not mounted: {mount}")
    view = args.common_view.resolve(strict=True)
    if is_mountpoint(view):
        raise RankError("common view was mounted before entering the private namespace")
    os.sched_setaffinity(0, {cpus[rank]})
    subprocess.run([args.mount_bin, "--bind", str(mount), str(view)], check=True)
    subprocess.run([args.mount_bin, "--make-private", str(view)], check=True)
    if not is_mountpoint(view):
        raise RankError("private FUSE view bind did not appear")
    receipt = {
        "schema": SCHEMA,
        "rank": rank,
        "pid": os.getpid(),
        "cpu": cpus[rank],
        "allowed_cpus": sorted(os.sched_getaffinity(0)),
        "mount": str(mount),
        "common_view": str(view),
        "endpoint": endpoints[rank],
        "host_monotonic_start_ns": time.monotonic_ns(),
        "host_monotonic_end_ns": None,
        "returncode": None,
    }
    receipt_path = args.receipt_dir / f"rank-{rank}.json"
    atomic_record(receipt_path, receipt)
    child_env = dict(environment)
    child_env.pop("LD_PRELOAD", None)
    if args.library_path:
        child_env["LD_LIBRARY_PATH"] = args.library_path
    if getattr(args, 'posix_mode', None) is not None:
        from posix_frontend import run_rank
        if args.posix_library is None:
            raise RankError('POSIX frontend requires its exact library path')
        returncode = run_rank(args, child_env, view, receipt, receipt_path, atomic_record)
    else:
        completed = subprocess.run(
            [str(args.io500), str(args.config), "--mode=extended"],
            env=child_env,
            check=False,
        )
        returncode = completed.returncode
    receipt["host_monotonic_end_ns"] = time.monotonic_ns()
    receipt["returncode"] = returncode
    atomic_record(receipt_path, receipt)
    return returncode


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--client-cpus", required=True)
    result.add_argument("--client-mounts", required=True)
    result.add_argument("--client-endpoints", required=True)
    result.add_argument("--common-view", type=Path, required=True)
    result.add_argument("--receipt-dir", type=Path, required=True)
    result.add_argument("--io500", type=Path, required=True)
    result.add_argument("--config", type=Path, required=True)
    result.add_argument("--library-path", default="")
    result.add_argument("--mount-bin", default="/usr/bin/mount")
    result.add_argument("--posix-mode", choices=('fuse', 'iov'))
    result.add_argument("--posix-library", type=Path)
    return result


def main() -> int:
    try:
        return execute(parser().parse_args(), dict(os.environ))
    except (RankError, OSError, subprocess.SubprocessError) as error:
        print(f"hf3fs-giga-rank=FAIL reason={error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
