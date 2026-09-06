#!/usr/bin/env python3
"""Run bounded P checks and require the deliberately unsafe cases to fail."""

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time

SAFE = (
    "PingPong", "QueueFull", "TimeoutOwnership", "CoalescedRequestDisposition",
    "RestartGeneration", "OneWay", "TwoWay", "CorruptDeliveredRecord",
    "CrashAfterDelivery", "StaleSubrange",
)
UNSAFE = {
    "UnsafeOverwrite": "published over a live slot",
    "UnsafePartialReplay": "partially delivered request classified as rejected",
    "UnsafeUnknownReplay": "unknown request automatically replayed",
    "UnsafeLeaseRelease": "lease released while old handle remains accessible",
    "UnsafeStaleHandle": "stale allocation handle accepted",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolchain", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--schedules", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20260905)
    args = parser.parse_args()
    if args.schedules <= 0 or args.seed < 0:
        parser.error("schedules must be positive and seed nonnegative")
    toolchain = args.toolchain.resolve(strict=True)
    contract = json.loads(toolchain.read_text())
    if contract["p_version"] != "2.3.2" or contract["sdk_version"] != "8.0.408":
        parser.error("expected the repository's P 2.3.2 and private SDK 8.0.408")
    env = dict(os.environ, **contract["environment"])
    executable = toolchain.parent / "p-2.3.2" / "p"
    env["PATH"] = os.pathsep.join([env["DOTNET_ROOT"], str(executable.parent), env["PATH"]])
    name = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{os.getpid()}"
    run = args.output.resolve() / name
    run.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parent
    model = run / "model"
    identities = {}
    files = [source / "CXLTransport.pproj"]
    for folder in ("PSrc", "PSpec", "PTst"):
        files.extend(sorted((source / folder).glob("*.p")))
    for path in files:
        relative = path.relative_to(source)
        destination = model / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        identities[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    result = dict(schema="hf3fs.cxl-formal-check.v1", status="failed", sources=identities,
                  toolchain_sha256=hashlib.sha256(toolchain.read_bytes()).hexdigest(),
                  schedules=args.schedules, seed=args.seed, checks=[], first_failure=None)

    def persist():
        temporary = run / "result.tmp"
        temporary.write_text(json.dumps(result, indent=2) + "\n")
        temporary.replace(run / "result.json")

    def command(name, arguments, timeout):
        log = run / f"{name}.log"
        started = time.monotonic()
        with log.open("w") as output:
            process = subprocess.Popen([str(executable), *arguments], cwd=model, env=env,
                                       stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                rc = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise RuntimeError(f"{name} exceeded its owned process deadline")
        text = log.read_text()
        return dict(name=name, argv=arguments, rc=rc, seconds=time.monotonic()-started,
                    log=str(log), log_sha256=hashlib.sha256(log.read_bytes()).hexdigest()), text

    persist()
    try:
        result["compile"], output = command("compile", ["compile"], 300)
        if result["compile"]["rc"] != 0:
            raise RuntimeError("P compilation failed")
        for name in (*UNSAFE, *SAFE):
            schedules = 10 if name in UNSAFE else args.schedules
            arguments = ["check", "--testcase", "tc" + name, "--schedules", str(schedules),
                         "--seed", str(args.seed), "--max-steps", "2000", "--fail-on-maxsteps",
                         "--timeout", "240", "--outdir", str(run / ("traces-" + name))]
            check, output = command(name, arguments, 270)
            expected = UNSAFE.get(name)
            check["expected_failure"] = expected
            if expected:
                traces = list((run / ("traces-" + name)).rglob("CXLTransport_*.txt"))
                check["bug_traces"] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in traces}
                found = any(re.search(r"<ErrorLog>[^\n]*" + re.escape(expected), p.read_text()) for p in traces)
                check["passed"] = check["rc"] != 0 and found
            else:
                explored = re.findall(r"Explored (\d+) schedules?", output)
                check["explored_schedules"] = int(explored[-1]) if explored else 0
                check["passed"] = (check["rc"] == 0 and bool(re.search(r"Found 0 bugs", output, re.I))
                                   and check["explored_schedules"] >= schedules)
            result["checks"].append(check)
            persist()
            print(name, "passed" if check["passed"] else "FAILED", flush=True)
            if not check["passed"]:
                raise RuntimeError(f"unexpected result for tc{name}")
        result["status"] = "passed"
    except Exception as error:
        result["first_failure"] = str(error)
    persist()
    print(run / "result.json", flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
