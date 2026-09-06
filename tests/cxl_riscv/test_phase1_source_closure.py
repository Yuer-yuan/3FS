from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from collections.abc import Mapping, Sequence
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "cxl-riscv"
sys.path.insert(0, str(DEPLOY))

import preflight
import full_deps


SERVER_CONFIGS = (
    ROOT / "configs" / "storage_main.toml",
    ROOT / "configs" / "meta_main.toml",
    ROOT / "configs" / "mgmtd_main.toml",
)
FUSE_TEST_SERVER_CONFIGS = (
    ROOT / "tests" / "fuse" / "config" / "storage_main.toml",
    ROOT / "tests" / "fuse" / "config" / "meta_main.toml",
    ROOT / "tests" / "fuse" / "config" / "mgmtd_main.toml",
)
DATA_CLIENT_CONFIGS = (
    ROOT / "configs" / "hf3fs_fuse_main.toml",
    ROOT / "configs" / "hf3fs_client_agent.toml",
    ROOT / "configs" / "admin_cli.toml",
)
LAUNCHER_CONFIGS = (
    ROOT / "configs" / "storage_main_launcher.toml",
    ROOT / "configs" / "meta_main_launcher.toml",
    ROOT / "configs" / "mgmtd_main_launcher.toml",
    ROOT / "configs" / "hf3fs_fuse_main_launcher.toml",
    ROOT / "configs" / "hf3fs_client_agent_launcher.toml",
    ROOT / "configs" / "admin_cli.toml",
)
CONFIG_DIRS = (ROOT / "configs", ROOT / "tests" / "fuse" / "config")
CXL_QUEUE_DEPTH = 8
CXL_CELL_BYTES = 64 * 1024
CXL_CLIENT_IO_WORKERS = {
    ROOT / "configs" / "admin_cli.toml": (
        ("client", "io_worker"),
        ("storage_client", "net_client", "io_worker"),
        ("storage_client", "net_client_for_updates", "io_worker"),
    ),
    ROOT / "configs" / "hf3fs_fuse_main.toml": (
        ("client", "io_worker"),
        ("storage", "net_client", "io_worker"),
        ("storage", "net_client_for_updates", "io_worker"),
    ),
    ROOT / "configs" / "hf3fs_client_agent.toml": (
        ("server", "background_client", "io_worker"),
        ("server", "storage", "net_client", "io_worker"),
        ("server", "storage", "net_client_for_updates", "io_worker"),
    ),
    ROOT / "configs" / "meta_main.toml": (
        ("server", "background_client", "io_worker"),
        ("server", "storage_client", "net_client", "io_worker"),
        ("server", "storage_client", "net_client_for_updates", "io_worker"),
    ),
    ROOT / "configs" / "storage_main.toml": (
        ("server", "client", "io_worker"),
        ("server", "forward_client", "io_worker"),
    ),
    ROOT / "configs" / "meta_main_launcher.toml": (("client", "io_worker"),),
    ROOT / "configs" / "storage_main_launcher.toml": (("client", "io_worker"),),
    ROOT / "configs" / "hf3fs_fuse_main_launcher.toml": (("client", "io_worker"),),
    ROOT / "configs" / "hf3fs_client_agent_launcher.toml": (("client", "io_worker"),),
    ROOT / "tests" / "fuse" / "config" / "admin_cli.toml": (
        ("client", "io_worker"),
        ("storage_client", "net_client", "io_worker"),
        ("storage_client", "net_client_for_updates", "io_worker"),
    ),
    ROOT / "tests" / "fuse" / "config" / "hf3fs_fuse_main.toml": (
        ("client", "io_worker"),
        ("storage", "net_client", "io_worker"),
        ("storage", "net_client_for_updates", "io_worker"),
    ),
    ROOT / "tests" / "fuse" / "config" / "meta_main.toml": (
        ("server", "background_client", "io_worker"),
        ("server", "storage_client", "net_client", "io_worker"),
        ("server", "storage_client", "net_client_for_updates", "io_worker"),
    ),
    ROOT / "tests" / "fuse" / "config" / "storage_main.toml": (
        ("server", "client", "io_worker"),
        ("server", "forward_client", "io_worker"),
    ),
    ROOT / "tests" / "fuse" / "config" / "meta_main_launcher.toml": (("client", "io_worker"),),
    ROOT / "tests" / "fuse" / "config" / "storage_main_launcher.toml": (("client", "io_worker"),),
    ROOT / "tests" / "fuse" / "config" / "hf3fs_fuse_main_launcher.toml": (("client", "io_worker"),),
}
FORBIDDEN_CONFIG = re.compile(
    r"network_type\s*=\s*['\"]RDMA['\"]|RDMA://|"
    r"^\[[^]]*(?:ib_devices|ibsocket)[^]]*\]",
    re.MULTILINE,
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _git(args: Sequence[str], cwd: Path = ROOT) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, stdout=subprocess.PIPE
    ).stdout


def _nested(config: Mapping[str, Any], keys: Sequence[str]) -> Any:
    value: Any = config
    for key in keys:
        if not isinstance(value, Mapping) or key not in value:
            return None
        value = value[key]
    return value


def _load_toml(path: Path) -> Mapping[str, Any]:
    content = re.sub(r"\$\{[^}]+\}", "1", path.read_text(encoding="utf-8"))
    return tomllib.loads(content)


def _validate_cxl_socket(path: Path, label: str, io_worker: Any) -> str | None:
    socket = io_worker.get("cxlsocket") if isinstance(io_worker, Mapping) else None
    if not isinstance(socket, Mapping):
        return f"{path.relative_to(ROOT)} {label} has no explicit CXL socket geometry"
    if socket.get("queue_depth") != CXL_QUEUE_DEPTH or socket.get("cell_bytes") != CXL_CELL_BYTES:
        return (
            f"{path.relative_to(ROOT)} {label} CXL socket geometry is "
            f"{socket.get('queue_depth')}/{socket.get('cell_bytes')}, expected "
            f"{CXL_QUEUE_DEPTH}/{CXL_CELL_BYTES}"
        )
    return None


def validate_configs() -> list[str]:
    errors: list[str] = []
    for directory in CONFIG_DIRS:
        for path in sorted(directory.glob("*.toml")):
            match = FORBIDDEN_CONFIG.search(path.read_text(encoding="utf-8"))
            if match:
                errors.append(f"{path.relative_to(ROOT)} retains {match.group(0)}")
    for path in SERVER_CONFIGS:
        config = _load_toml(path)
        groups = config["server"]["base"]["groups"]
        data = [group for group in groups if group["network_type"] == "CXL"]
        control = [group for group in groups if group["network_type"] == "TCP"]
        if len(data) != 1 or data[0].get("service_plane") != "Data":
            errors.append(f"{path.relative_to(ROOT)} has no single CXL Data group")
        if len(control) != 1 or control[0].get("service_plane") != "Control":
            errors.append(f"{path.relative_to(ROOT)} has no single TCP Control group")
        if len(data) == 1:
            if error := _validate_cxl_socket(path, "CXL Data group", data[0].get("io_worker")):
                errors.append(error)
    for path in FUSE_TEST_SERVER_CONFIGS:
        config = _load_toml(path)
        groups = config["server"]["base"]["groups"]
        if not groups:
            errors.append(f"{path.relative_to(ROOT)} has no server groups")
        elif error := _validate_cxl_socket(path, "first Data group", groups[0].get("io_worker")):
            errors.append(error)
    for path in DATA_CLIENT_CONFIGS:
        config = _load_toml(path)
        if path.name == "hf3fs_fuse_main.toml":
            client = config["meta"]
        elif path.name == "hf3fs_client_agent.toml":
            client = config["server"]["meta"]
        else:
            client = config["meta_client"]
        if client.get("network_type") != "CXL":
            errors.append(f"{path.relative_to(ROOT)} data client does not select CXL")
    for path, io_worker_paths in CXL_CLIENT_IO_WORKERS.items():
        config = _load_toml(path)
        for keys in io_worker_paths:
            if error := _validate_cxl_socket(path, ".".join(keys), _nested(config, keys)):
                errors.append(error)
    for path in LAUNCHER_CONFIGS:
        config = _load_toml(path)
        cxl = config.get("cxl")
        if not isinstance(cxl, Mapping) or cxl.get("enabled") is not True:
            errors.append(f"{path.relative_to(ROOT)} does not enable CXL")
            continue
        if cxl.get("mode") != "Attach" or not cxl.get("manifest_path"):
            errors.append(f"{path.relative_to(ROOT)} is not attach-only and manifest-bound")
        if not isinstance(cxl.get("endpoint"), int) or cxl["endpoint"] <= 0:
            errors.append(f"{path.relative_to(ROOT)} has no process endpoint identity")
    return errors


def validate_source_guards() -> list[str]:
    errors: list[str] = []
    include = re.compile(r'^\s*#include\s+["<]common/net/ib/', re.MULTILINE)
    for path in sorted((ROOT / "src").rglob("*")):
        if path.suffix not in {".h", ".cc", ".cpp"} or "common/net/ib" in path.as_posix():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        rdma_guard_depth = 0
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#if") and "HF3FS_ENABLE_RDMA" in stripped:
                rdma_guard_depth += 1
                continue
            if stripped == "#endif" and rdma_guard_depth:
                rdma_guard_depth -= 1
                continue
            if not include.match(line):
                continue
            if rdma_guard_depth == 0:
                errors.append(f"{path.relative_to(ROOT)}:{index + 1} has an unguarded verbs include")
    cmake = (ROOT / "src" / "common" / "CMakeLists.txt").read_text(encoding="utf-8")
    if "if(NOT HF3FS_ENABLE_RDMA)" not in cmake or 'net/ib/' not in cmake:
        errors.append("src/common/CMakeLists.txt does not exclude the verbs backend")
    return errors


def _source_identity() -> dict[str, Any]:
    return full_deps.source_identity(ROOT)


def _cache_options(build_dir: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (build_dir / "CMakeCache.txt").read_text(encoding="utf-8").splitlines():
        if line.startswith("HF3FS_ENABLE_CXL:") or line.startswith("HF3FS_ENABLE_RDMA:"):
            key, value = line.split("=", 1)
            values[key.split(":", 1)[0]] = value
    return values


def inspect_elf_closure(build_dir: Path) -> tuple[list[str], list[dict[str, Any]]]:
    errors: list[str] = []
    records: list[dict[str, Any]] = []
    binary_dir = build_dir / "bin"
    for path in sorted(binary_dir.iterdir() if binary_dir.is_dir() else ()):
        if not path.is_file() or not os.access(path, os.X_OK):
            continue
        try:
            info = preflight.inspect_elf(path)
        except (OSError, UnicodeError, ValueError):
            continue
        forbidden = [name for name in info.needed if name.startswith("libibverbs.so") or name.startswith("librdmacm.so")]
        if forbidden:
            errors.append(f"{path.name} links forbidden verbs libraries: {', '.join(forbidden)}")
        records.append(
            {"path": str(path.relative_to(build_dir)), "sha256": _sha256_file(path), "needed": list(info.needed)}
        )
    if not records:
        errors.append("build contains no executable ELF artifacts")
    return errors, records


def write_evidence(build_dir: Path, output: Path) -> list[str]:
    errors = validate_configs() + validate_source_guards()
    try:
        cache = _cache_options(build_dir)
    except OSError as error:
        errors.append(f"cannot read CMake cache: {error}")
        cache = {}
    if cache.get("HF3FS_ENABLE_CXL") != "ON":
        errors.append("HF3FS_ENABLE_CXL must be ON")
    if cache.get("HF3FS_ENABLE_RDMA") != "OFF":
        errors.append("HF3FS_ENABLE_RDMA must be OFF")
    elf_errors, artifacts = inspect_elf_closure(build_dir)
    errors.extend(elf_errors)
    if errors:
        return errors
    config_hashes = {
        str(path.relative_to(ROOT)): _sha256_file(path)
        for directory in CONFIG_DIRS
        for path in sorted(directory.glob("*.toml"))
    }
    record = {
        "schema": "hf3fs.cxl-source-closure.v1",
        "gate": "phase1",
        "status": "passed",
        "cmake": cache,
        "source": _source_identity(),
        "configs": config_hashes,
        "artifacts": artifacts,
    }
    destination = Path(os.path.normpath(output))
    if not destination.is_absolute():
        return ["evidence output must be absolute"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=destination.parent, prefix=destination.name + ".tmp-", delete=False
    ) as handle:
        temporary = Path(handle.name)
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(destination)
    return []


class Phase1SourceClosureTest(unittest.TestCase):
    def test_phase1_configs(self) -> None:
        self.assertEqual(validate_configs(), [])

    def test_verbs_backend_is_compile_time_guarded(self) -> None:
        self.assertEqual(validate_source_guards(), [])

    def test_required_cmake_switches_exist(self) -> None:
        cmake = (ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
        self.assertIn("option(HF3FS_ENABLE_CXL", cmake)
        self.assertIn("option(HF3FS_ENABLE_RDMA", cmake)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-dir", type=Path)
    parser.add_argument("--write-evidence", type=Path)
    args = parser.parse_args(argv)
    if args.write_evidence is not None and args.build_dir is None:
        parser.error("--write-evidence requires --build-dir")
    if args.build_dir is None:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Phase1SourceClosureTest)
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    errors = write_evidence(args.build_dir.resolve(), args.write_evidence.resolve())
    for error in errors:
        print(f"phase1-source-closure: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
