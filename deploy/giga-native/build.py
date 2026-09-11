#!/usr/bin/env python3
"""Build and attest the native CXL-only 3FS/IO500 bundle on giga."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request


SCHEMA = "hf3fs.giga-native-build.v1"
FDB_VERSION = "7.3.63"
FDB_RELEASE = f"https://github.com/apple/foundationdb/releases/download/{FDB_VERSION}"
IO500_COMMIT = "a69cf60cf76538a34c1332bc448838cf9a560a9b"
IOR_COMMIT = "5fcf0ba995fd92164d50e344597e2d8203298c08"
PFIND_COMMIT = "d08501f9976caf1adabdebfb883d4701dd98fe35"
PRODUCTS = (
    "cxl-fabricd", "mgmtd_main", "meta_main", "storage_main",
    "hf3fs_fuse_main", "admin_cli", "fdb_client_smoke",
)
TEST_TARGETS = ("test_transport_planes", "test_cxl_layout", "test_cxl_transport_e2e", "test_fuse_piov_read")


class BuildError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def run(
    command: list[str], *, log: Path | None = None, cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> str:
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as destination:
            destination.write("COMMAND " + json.dumps(command) + "\n")
    process = subprocess.run(
        command,
        cwd=None if cwd is None else str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if log is not None:
        with log.open("a", encoding="utf-8") as destination:
            destination.write(process.stdout)
            if process.stdout and not process.stdout.endswith("\n"):
                destination.write("\n")
            destination.write(f"EXIT {process.returncode}\n")
    if process.returncode:
        raise BuildError(f"command failed with {process.returncode}: {' '.join(command)}")
    return process.stdout.rstrip()


def hf3fs_cmake_command(source: Path, build: Path, fdb: Path) -> list[str]:
    return [
        "/usr/bin/cmake", "-S", str(source), "-B", str(build), "-G", "Ninja",
        "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
        "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
        "-DCMAKE_C_COMPILER=/usr/bin/clang-18",
        "-DCMAKE_CXX_COMPILER=/usr/bin/clang++-18",
        "-DCMAKE_CXX_FLAGS=--gcc-install-dir=/usr/lib/gcc/x86_64-linux-gnu/13",
        "-DSHUFFLE_METHOD=g++11",
        "-DHF3FS_ENABLE_CXL=ON",
        "-DHF3FS_ENABLE_RDMA=OFF",
        "-DENABLE_FUSE_APPLICATION=ON",
        f"-DHF3FS_FDB_ROOT={fdb}",
    ]


def native_compiler_environment(base: dict[str, str], jobs: int) -> dict[str, str]:
    if jobs <= 0:
        raise ValueError("jobs must be positive")
    # The Rust librocksdb-sys build does not inherit RocksDB's CMake feature
    # detection. Use its existing Linux core-ID implementation: clang-18's
    # optimized CPUID fallback can corrupt callee-saved RBX under contention.
    # This is a native-platform build setting, not an engine/IO policy change.
    return dict(
        base, CC="/usr/bin/clang-18", CXX="/usr/bin/clang++-18",
        CXXFLAGS=("--gcc-install-dir=/usr/lib/gcc/x86_64-linux-gnu/13 "
                  "-DROCKSDB_SCHED_GETCPU_PRESENT=1"),
        CARGO_BUILD_JOBS=str(jobs), CMAKE_POLICY_VERSION_MINIMUM="3.5",
    )


def source_fingerprint(root: Path) -> tuple[str, int]:
    root = Path(root).resolve(strict=True)
    digest = hashlib.sha256()
    count = 0
    excluded = {".git", "build", "out", "__pycache__", ".pytest_cache", "target"}
    for current, directories, files in os.walk(root):
        directories[:] = sorted(name for name in directories if name not in excluded)
        base = Path(current)
        for name in sorted(files):
            path = base / name
            if path.is_symlink() or name.endswith((".pyc", ".o", ".a", ".so")):
                continue
            relative = path.relative_to(root).as_posix().encode()
            digest.update(len(relative).to_bytes(8, "little"))
            digest.update(relative)
            digest.update(bytes.fromhex(sha256(path)))
            count += 1
    return digest.hexdigest(), count


def _download_verified(url: str, destination: Path) -> dict:
    checksum_url = url + ".sha256"
    with urllib.request.urlopen(checksum_url, timeout=60) as response:
        checksum_text = response.read().decode()
    match = re.search(r"\b([0-9a-fA-F]{64})\b", checksum_text)
    if match is None:
        raise BuildError(f"release checksum is malformed: {checksum_url}")
    expected = match.group(1).lower()
    temporary = destination.with_suffix(destination.suffix + ".part")
    with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as output:
        shutil.copyfileobj(response, output)
    actual = sha256(temporary)
    if actual != expected:
        temporary.unlink()
        raise BuildError(f"download checksum mismatch: {destination.name}")
    temporary.replace(destination)
    return {"url": url, "checksum_url": checksum_url, "sha256": actual}


def prepare_fdb(output: Path, log: Path) -> tuple[Path, dict]:
    prefix = output / "fdb"
    marker = prefix / "fdb-native.json"
    if marker.exists():
        record = json.loads(marker.read_text())
        for item in record["artifacts"].values():
            path = Path(item["path"])
            if not path.is_file() or sha256(path) != item["sha256"]:
                raise BuildError(f"cached FoundationDB artifact changed: {path}")
        return prefix, record
    if prefix.exists():
        raise BuildError(f"incomplete FoundationDB prefix exists: {prefix}")
    cache = output / "downloads"
    cache.mkdir(parents=True, exist_ok=True)
    downloads = {}
    for kind in ("clients", "server"):
        name = f"foundationdb-{kind}_{FDB_VERSION}-1_amd64.deb"
        package = cache / name
        if package.exists():
            with urllib.request.urlopen(f"{FDB_RELEASE}/{name}.sha256", timeout=60) as response:
                expected = re.search(r"\b([0-9a-fA-F]{64})\b", response.read().decode())
            if expected is None or sha256(package) != expected.group(1).lower():
                raise BuildError(f"cached FoundationDB package changed: {package}")
            downloads[kind] = {"url": f"{FDB_RELEASE}/{name}", "sha256": sha256(package)}
        else:
            downloads[kind] = _download_verified(f"{FDB_RELEASE}/{name}", package)
        run(["/usr/bin/dpkg-deb", "-x", str(package), str(prefix)], log=log)
    candidates = {
        "fdbserver": (prefix / "usr/sbin/fdbserver", prefix / "usr/bin/fdbserver"),
        "fdbcli": (prefix / "usr/bin/fdbcli",),
        "libfdb_c": (prefix / "usr/lib/libfdb_c.so", prefix / "usr/lib/x86_64-linux-gnu/libfdb_c.so"),
    }
    artifacts = {}
    for name, paths in candidates.items():
        path = next((value for value in paths if value.is_file()), None)
        if path is None:
            raise BuildError(f"FoundationDB package lacks {name}")
        artifacts[name] = {"path": str(path.resolve()), "sha256": sha256(path), "size": path.stat().st_size}
    record = {"version": FDB_VERSION, "downloads": downloads, "artifacts": artifacts}
    marker.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return prefix, record


def inspect_elf(path: Path) -> dict:
    file_text = run(["/usr/bin/file", "-b", str(path)])
    if "ELF 64-bit" not in file_text or "x86-64" not in file_text:
        raise BuildError(f"artifact is not native x86-64 ELF: {path}")
    dynamic = run(["/usr/bin/readelf", "-d", str(path)])
    needed = re.findall(r"Shared library: \[([^]]+)\]", dynamic)
    interpreter = None
    program = run(["/usr/bin/readelf", "-l", str(path)])
    match = re.search(r"Requesting program interpreter: ([^]]+)", program)
    if match:
        interpreter = match.group(1)
    ldd = run(["/usr/bin/ldd", str(path)])
    if "not found" in ldd:
        raise BuildError(f"artifact has unresolved libraries: {path}")
    return {
        "path": str(path.resolve()), "sha256": sha256(path), "size": path.stat().st_size,
        "file": file_text, "interpreter": interpreter, "needed": needed, "ldd": ldd,
    }


def _find_product(build: Path, name: str) -> Path:
    direct = build / "bin" / name
    if direct.is_file():
        return direct
    matches = [path for path in build.rglob(name) if path.is_file() and os.access(path, os.X_OK)]
    if len(matches) != 1:
        raise BuildError(f"expected one built {name}, found {matches}")
    return matches[0]


def adopt_io500(repo: Path) -> dict:
    legacy_manifest = repo / "target/results/giga-native/build-manifest.json"
    if not legacy_manifest.is_file():
        raise BuildError("pinned native IO500 manifest is absent; run scripts/build_giga_native_io500.sh")
    record = json.loads(legacy_manifest.read_text())
    if record.get("schema_version") != "giga.native-build.v1":
        raise BuildError("native IO500 manifest schema changed")
    artifacts = {}
    for source_name, name in (("io500", "io500"), ("io500_verify", "io500-verify")):
        source = record.get("artifacts", {}).get(source_name, {})
        path = Path(str(source.get("path", "")))
        if not path.is_file() or sha256(path) != source.get("sha256"):
            raise BuildError(f"pinned native {name} artifact changed")
        artifacts[name] = inspect_elf(path)
    sources = repo / "target/build/giga-native/sources"
    pins = {"io500": IO500_COMMIT, "io500/build/ior": IOR_COMMIT, "io500/build/pfind": PFIND_COMMIT}
    for relative, commit in pins.items():
        git_dir = sources / relative
        actual = run(["/usr/bin/git", "-C", str(git_dir), "rev-parse", "HEAD"])
        if actual != commit:
            raise BuildError(f"native benchmark source pin changed: {relative}")
    return {"source_manifest": str(legacy_manifest), "pins": pins, "artifacts": artifacts}


def write_manifest(path: Path, artifacts: dict[str, Path], **values: object) -> Path:
    inspected = {name: inspect_elf(artifact) for name, artifact in artifacts.items()}
    manifest = {"schema": SCHEMA, **values, "artifacts": inspected}
    manifest["manifest_payload_sha256"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return path


def verify_manifest(path: Path) -> dict:
    record = json.loads(Path(path).read_text())
    if record.get("schema") != SCHEMA:
        raise ValueError("native build manifest schema changed")
    payload = dict(record)
    expected = payload.pop("manifest_payload_sha256", None)
    actual = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if actual != expected:
        raise ValueError("native build manifest payload changed")
    for item in record.get("artifacts", {}).values():
        artifact = Path(item["path"])
        if not artifact.is_file() or sha256(artifact) != item["sha256"]:
            raise ValueError(f"artifact changed: {artifact}")
    return record


def execute(repo: Path, output: Path, jobs: int) -> Path:
    repo = Path(repo).resolve(strict=True)
    output = Path(output).resolve()
    if platform.machine() != "x86_64":
        raise BuildError("giga-native build requires x86_64")
    if jobs <= 0:
        raise BuildError("jobs must be positive")
    for tool in ("cmake", "ninja", "clang-18", "clang++-18", "dpkg-deb", "file", "readelf", "ldd"):
        if shutil.which(tool) is None:
            raise BuildError(f"missing build tool: {tool}")
    for header in (Path("/usr/include/libaio.h"), Path("/usr/include/gperftools/profiler.h")):
        if not header.is_file():
            raise BuildError(f"missing native build dependency: {header}")
    output.mkdir(parents=True, exist_ok=True)
    logs = repo / "target/results/giga-native-3fs/build-logs"
    logs.mkdir(parents=True, exist_ok=True)
    fdb_prefix, fdb = prepare_fdb(output, logs / "foundationdb.log")
    source = repo / "components/3FS"
    build_dir = output / "hf3fs"
    configure = hf3fs_cmake_command(source, build_dir, fdb_prefix)
    compiler_env = native_compiler_environment(dict(os.environ), jobs)
    run(configure, log=logs / "hf3fs.log", env=compiler_env)
    run(
        ["/usr/bin/cmake", "--build", str(build_dir), "--parallel", str(jobs), "--target", *PRODUCTS, *TEST_TARGETS],
        log=logs / "hf3fs.log", env=compiler_env,
    )
    io500 = adopt_io500(repo)
    artifacts = {name: _find_product(build_dir, name) for name in PRODUCTS}
    for name in TEST_TARGETS:
        artifacts[name] = _find_product(build_dir, name)
    artifacts["fdbserver"] = Path(fdb["artifacts"]["fdbserver"]["path"])
    artifacts["fdbcli"] = Path(fdb["artifacts"]["fdbcli"]["path"])
    artifacts["libfdb_c"] = Path(fdb["artifacts"]["libfdb_c"]["path"])
    artifacts["io500"] = Path(io500["artifacts"]["io500"]["path"])
    artifacts["io500-verify"] = Path(io500["artifacts"]["io500-verify"]["path"])
    fingerprint, source_files = source_fingerprint(source)
    profile = source / "deploy/giga-native/io500-all-bounded-1s.ini"
    manifest = repo / "target/results/giga-native-3fs/build-manifest.json"
    return write_manifest(
        manifest,
        artifacts,
        host=platform.node(),
        machine=platform.machine(),
        source={"root": str(source), "sha256": fingerprint, "files": source_files},
        configure=configure,
        fdb=fdb,
        io500=io500,
        profile={"path": str(profile), "sha256": sha256(profile)},
        compiler_environment={key: compiler_env[key] for key in
                              ("CC", "CXX", "CXXFLAGS", "CARGO_BUILD_JOBS", "CMAKE_POLICY_VERSION_MINIMUM")},
        tools={
            "cmake": run(["/usr/bin/cmake", "--version"]).splitlines()[0],
            "ninja": run(["/usr/bin/ninja", "--version"]),
            "compiler": run(["/usr/bin/clang++-18", "--version"]).splitlines()[0],
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=max(1, os.cpu_count() or 1))
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    try:
        if args.verify:
            verify_manifest(args.verify)
            print(f"hf3fs-giga-build=PASS manifest={args.verify}")
        else:
            manifest = execute(args.repo, args.output, args.jobs)
            print(f"hf3fs-giga-build=PASS manifest={manifest}")
        return 0
    except (BuildError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"hf3fs-giga-build=FAIL reason={error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
