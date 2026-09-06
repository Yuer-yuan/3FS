#!/usr/bin/env python3
"""Import or attempt a pinned native RISC-V FoundationDB build."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from typing import Any


FDB_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = FDB_DIR.parents[2]
DEFAULT_LOCK = FDB_DIR / "fdb-riscv.lock.json"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "out" / "cxl-riscv" / "fdb"
DEFAULT_SOURCE = PROJECT_ROOT / "third_party" / "foundationdb"
if str(FDB_DIR) not in sys.path:
    sys.path.insert(0, str(FDB_DIR))

import verify_fdb_bundle

preflight = verify_fdb_bundle.preflight


class BuildError(RuntimeError):
    pass


ATTEMPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TARGET_RUNTIME_DIRECTORIES = (
    Path("lib"),
    Path("lib64"),
    Path("lib/riscv64-linux-gnu"),
    Path("usr/lib"),
    Path("usr/lib64"),
    Path("usr/lib/riscv64-linux-gnu"),
    Path("usr/riscv64-linux-gnu/lib"),
)


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BuildError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise BuildError(f"{path} must contain a JSON object")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(candidate: Path, directory: Path) -> bool:
    try:
        candidate.relative_to(directory)
    except ValueError:
        return False
    return True


def find_target_runtime_file(sysroot: Path, reference: str) -> Path:
    """Find one target loader input while preserving its visible filename.

    Resolving a SONAME symlink before choosing the destination would stage only
    (for example) ``libstdc++.so.6.0.33``.  The target loader asks for
    ``libstdc++.so.6``, so this function validates the resolved file but returns
    the lexical alias that the loader actually opens.
    """

    root = sysroot.resolve(strict=True)
    if not root.is_dir():
        raise BuildError("FDB target sysroot is not a directory")
    requested = Path(reference)
    if requested.is_absolute():
        relative = Path(*requested.parts[1:])
        candidates = [root / relative]
    else:
        if len(requested.parts) != 1 or reference in {"", ".", ".."}:
            raise BuildError(f"invalid FDB target runtime reference: {reference}")
        candidates = [root / directory / reference for directory in TARGET_RUNTIME_DIRECTORIES]
        try:
            candidates.extend(root.rglob(reference))
        except OSError as error:
            raise BuildError(
                f"cannot search FDB target sysroot for {reference}: {error}"
            ) from error

    valid: list[tuple[Path, Path]] = []
    seen_lexical: set[Path] = set()
    for candidate in candidates:
        lexical = Path(os.path.normpath(candidate))
        if lexical in seen_lexical:
            continue
        seen_lexical.add(lexical)
        try:
            resolved = lexical.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved.is_file() and _is_within(resolved, root):
            valid.append((lexical, resolved))

    resolved_files = {resolved for _, resolved in valid}
    if len(resolved_files) != 1:
        raise BuildError(
            f"FDB target runtime reference is absent or ambiguous: {reference}"
        )
    selected_target = next(iter(resolved_files))
    return next(lexical for lexical, resolved in valid if resolved == selected_target)


def copy_target_runtime_file(
    source: Path, source_sysroot: Path, target_sysroot: Path
) -> Path:
    """Copy target bytes under the same loader-visible path in a new sysroot."""

    source_root = source_sysroot.resolve(strict=True)
    lexical_source = Path(os.path.normpath(source))
    try:
        relative = lexical_source.relative_to(source_root)
        actual_source = lexical_source.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise BuildError("FDB target runtime file escapes its sysroot") from error
    if not actual_source.is_file() or not _is_within(actual_source, source_root):
        raise BuildError("FDB target runtime file escapes its sysroot")
    destination = target_sysroot / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_file() or _sha256_file(destination) != _sha256_file(
            actual_source
        ):
            raise BuildError(f"conflicting staged FDB runtime file: {relative}")
        return destination
    shutil.copy2(actual_source, destination, follow_symlinks=True)
    return destination


def require_output_root(output_root: Path, project_root: Path = PROJECT_ROOT) -> Path:
    if not output_root.is_absolute():
        raise BuildError("FDB output root must be absolute")
    resolved_project = project_root.resolve(strict=True)
    resolved_output = output_root.resolve(strict=False)
    approved = resolved_project / "out" / "cxl-riscv" / "fdb"
    if not _is_within(resolved_output, approved):
        raise BuildError("FDB output root escapes components/3FS/out/cxl-riscv/fdb")
    return resolved_output


def validate_attempt_id(value: str) -> str:
    if not ATTEMPT_ID.fullmatch(value):
        raise BuildError(
            "attempt id must contain only letters, digits, '.', '_' or '-' "
            "and must begin with a letter or digit"
        )
    return value


def default_attempt_id() -> str:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"attempt-{timestamp}-{os.getpid()}"


def tool_environment(
    cxx_compiler: Path, base_environment: Mapping[str, str]
) -> dict[str, str]:
    """Create the host environment used to launch cross-toolchain programs.

    Ubuntu's extracted cross compiler loads host-side binutils libraries from
    its private multiarch directory.  Resolve that directory from the exact
    compiler path and never inherit an unrelated ambient LD_LIBRARY_PATH.
    """

    environment = dict(base_environment)
    environment["LANG"] = "C"
    environment["LC_ALL"] = "C"
    environment.pop("LD_LIBRARY_PATH", None)
    resolved = cxx_compiler.resolve(strict=False)
    if resolved.parent.name == "bin" and resolved.parent.parent.name == "usr":
        toolchain_root = resolved.parent.parent.parent
        candidate = toolchain_root / "usr" / "lib" / "x86_64-linux-gnu"
        if toolchain_root != Path("/") and candidate.is_dir():
            environment["LD_LIBRARY_PATH"] = str(candidate.resolve(strict=True))
    return environment


def locked_source_modifications(
    source: Path, lock: Mapping[str, Any]
) -> dict[str, str]:
    """Resolve the exact, directly edited files allowed in the FDB checkout."""

    records = lock.get("source_modifications")
    if not isinstance(records, list):
        raise BuildError("FDB lock source_modifications must be a list")
    resolved_source = source.resolve(strict=True)
    result: dict[str, str] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise BuildError("FDB lock contains a non-object source modification")
        declared = record.get("path")
        expected_hash = record.get("sha256")
        if not isinstance(declared, str) or not isinstance(expected_hash, str):
            raise BuildError("FDB source modification lock record is malformed")
        relative = Path(declared)
        if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
            raise BuildError("FDB source modification path escapes its checkout")
        target = (resolved_source / relative).resolve(strict=True)
        if not target.is_file() or not _is_within(target, resolved_source):
            raise BuildError("FDB source modification path escapes its checkout")
        normalized = relative.as_posix()
        if normalized in result:
            raise BuildError("FDB source modification path is duplicated")
        if _sha256_file(target) != expected_hash:
            raise BuildError(
                f"FDB source modification SHA-256 differs from lock: {normalized}"
            )
        result[normalized] = expected_hash
    return result


def seed_boost_archive(
    archive: Path, build: Path, lock: Mapping[str, Any]
) -> Path:
    record = lock.get("upstream_archives", {}).get("boost_1_78_0")
    if not isinstance(record, Mapping):
        raise BuildError("FDB lock has no Boost 1.78.0 archive record")
    filename = record.get("filename")
    expected_size = record.get("size")
    expected_hash = record.get("sha256")
    if (
        filename != "boost_1_78_0.tar.bz2"
        or not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size <= 0
        or not isinstance(expected_hash, str)
    ):
        raise BuildError("FDB Boost archive lock record is malformed")
    source = archive.resolve(strict=True)
    if (
        not source.is_file()
        or source.stat().st_size != expected_size
        or _sha256_file(source) != expected_hash
    ):
        raise BuildError("FDB Boost archive differs from the lock")
    destination = (
        build / "boost_targetProject-prefix" / "src" / str(filename)
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if (
            not destination.is_file()
            or destination.stat().st_size != expected_size
            or _sha256_file(destination) != expected_hash
        ):
            raise BuildError("existing FDB Boost build archive differs from the lock")
        return destination
    temporary = destination.with_name(f".{destination.name}.incoming-{os.getpid()}")
    shutil.copy2(source, temporary)
    if temporary.stat().st_size != expected_size or _sha256_file(temporary) != expected_hash:
        raise BuildError("copied FDB Boost archive differs from the lock")
    temporary.replace(destination)
    return destination


def configure_command(
    *,
    source: Path,
    build: Path,
    cmake: Path,
    ninja: Path,
    sysroot: Path,
    c_compiler: Path,
    cxx_compiler: Path,
    mono: Path,
    mcs: Path,
) -> list[str]:
    return [
        str(cmake),
        "-S",
        str(source),
        "-B",
        str(build),
        "-G",
        "Ninja",
        f"-DCMAKE_MAKE_PROGRAM={ninja}",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_SYSTEM_NAME=Linux",
        "-DCMAKE_SYSTEM_PROCESSOR=riscv64",
        "-DCMAKE_LIBRARY_ARCHITECTURE=riscv64-linux-gnu",
        "-DCMAKE_SYSTEM_LIBRARY_PATH=/usr/lib/riscv64-linux-gnu;"
        "/usr/riscv64-linux-gnu/lib",
        f"-DCMAKE_SYSROOT={sysroot}",
        f"-DCMAKE_FIND_ROOT_PATH={sysroot}",
        "-DCMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER",
        "-DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY",
        "-DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY",
        "-DCMAKE_FIND_ROOT_PATH_MODE_PACKAGE=ONLY",
        "-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY",
        f"-DCMAKE_C_COMPILER={c_compiler}",
        f"-DCMAKE_CXX_COMPILER={cxx_compiler}",
        f"-DMONO_EXECUTABLE={mono}",
        f"-DMCS_EXECUTABLE={mcs}",
        "-DFDB_RELEASE=ON",
        "-DUSE_CCACHE=OFF",
        "-DUSE_JEMALLOC=OFF",
        "-DUSE_AVX=OFF",
        "-DUSE_AVX512F=OFF",
        "-DSSD_ROCKSDB_EXPERIMENTAL=OFF",
        "-DBUILD_DOCUMENTATION=OFF",
        "-DBUILD_C_BINDING=ON",
        "-DBUILD_PYTHON_BINDING=OFF",
        "-DBUILD_JAVA_BINDING=OFF",
        "-DBUILD_GO_BINDING=OFF",
        "-DBUILD_RUBY_BINDING=OFF",
    ]


def rewrite_manifest_paths(
    bundle: Mapping[str, Any], source_sysroot: Path, target_sysroot: Path
) -> dict[str, Any]:
    source = Path(os.path.normpath(source_sysroot))
    target = Path(os.path.normpath(target_sysroot))

    def rewrite(value: object) -> object:
        if isinstance(value, dict):
            return {key: rewrite(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if isinstance(value, str) and value.startswith(str(source) + os.sep):
            path = Path(os.path.normpath(value))
            try:
                relative = path.relative_to(source)
            except ValueError:
                return value
            return str(target / relative)
        if value == str(source):
            return str(target)
        return value

    result = rewrite(copy.deepcopy(dict(bundle)))
    assert isinstance(result, dict)
    return result


def _run(
    command: Sequence[str],
    *,
    log: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> str:
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as destination:
            destination.write("COMMAND " + json.dumps(list(command)) + "\n")
            destination.flush()
    process = subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=None if environment is None else dict(environment),
    )
    assert process.stdout is not None
    output: list[str] = []
    with process.stdout as source:
        if log is None:
            output.append(source.read())
        else:
            with log.open("a", encoding="utf-8") as destination:
                for line in source:
                    output.append(line)
                    destination.write(line)
                    destination.flush()
    returncode = process.wait()
    combined = "".join(output)
    if log is not None:
        with log.open("a", encoding="utf-8") as destination:
            if combined and not combined.endswith("\n"):
                destination.write("\n")
            destination.write(f"EXIT {returncode}\n")
            destination.flush()
    if returncode != 0:
        raise BuildError(
            f"command failed with exit {returncode}: "
            + " ".join(command)
        )
    return combined.rstrip()


def verify_source_checkout(source: Path, lock: Mapping[str, Any]) -> None:
    locked = lock["source"]
    head = _run(["git", "-C", str(source), "rev-parse", "HEAD"])
    tag_object = _run(
        ["git", "-C", str(source), "rev-parse", f"refs/tags/{locked['tag']}"]
    )
    peeled = _run(
        [
            "git",
            "-C",
            str(source),
            "rev-parse",
            f"refs/tags/{locked['tag']}^{{commit}}",
        ]
    )
    if head != locked["commit"] or peeled != locked["commit"]:
        raise BuildError("FoundationDB checkout commit differs from the lock")
    if tag_object != locked["annotated_tag_object"]:
        raise BuildError("FoundationDB annotated tag object differs from the lock")
    _run(["git", "-C", str(source), "diff", "--check", "HEAD", "--"])
    changed = set(
        _run(
            [
                "git",
                "-C",
                str(source),
                "diff",
                "--name-only",
                "--no-renames",
                "HEAD",
                "--",
            ]
        ).splitlines()
    )
    untracked = set(
        _run(
            [
                "git",
                "-C",
                str(source),
                "ls-files",
                "--others",
                "--exclude-standard",
            ]
        ).splitlines()
    )
    actual_paths = changed | untracked
    expected_paths = set(locked_source_modifications(source, lock))
    if actual_paths != expected_paths:
        raise BuildError(
            "FoundationDB direct source modifications differ from the lock"
        )
    if untracked:
        raise BuildError("FoundationDB source modifications must be tracked files")


def _inspect_target_elf(path: Path, label: str) -> preflight.ElfInfo:
    try:
        info = preflight.inspect_elf(path)
    except (OSError, UnicodeError, ValueError) as error:
        raise BuildError(f"{label} is not a valid target ELF: {error}") from error
    if not info.is_riscv64_little_endian:
        raise BuildError(f"{label} is not RISC-V ELF64 little-endian")
    return info


def collect_target_dependency_closure(
    artifacts: Sequence[Path], sysroot: Path
) -> tuple[dict[Path, preflight.ElfInfo], dict[Path, preflight.ElfInfo]]:
    """Inspect target artifacts and resolve their complete runtime closure."""

    root = sysroot.resolve(strict=True)
    artifact_info: dict[Path, preflight.ElfInfo] = {}
    pending: list[str] = []
    for artifact in artifacts:
        resolved = artifact.resolve(strict=True)
        if not resolved.is_file():
            raise BuildError(f"FDB target artifact is not a regular file: {artifact}")
        info = _inspect_target_elf(resolved, str(artifact))
        artifact_info[resolved] = info
        if info.interpreter is not None:
            pending.append(info.interpreter)
        pending.extend(info.needed)

    dependencies: dict[Path, preflight.ElfInfo] = {}
    seen_relative: set[Path] = set()
    while pending:
        reference = pending.pop(0)
        source = find_target_runtime_file(root, reference)
        relative = source.relative_to(root)
        if relative in seen_relative:
            continue
        seen_relative.add(relative)
        info = _inspect_target_elf(source.resolve(strict=True), reference)
        dependencies[source] = info
        if info.interpreter is not None:
            pending.append(info.interpreter)
        pending.extend(info.needed)
    return artifact_info, dependencies


def _copy_built_artifact(source: Path, destination: Path, build: Path) -> Path:
    resolved_build = build.resolve(strict=True)
    resolved_source = source.resolve(strict=True)
    if not resolved_source.is_file() or not _is_within(resolved_source, resolved_build):
        raise BuildError("FDB build artifact escapes its build directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise BuildError(f"FDB staged artifact already exists: {destination}")
    shutil.copy2(resolved_source, destination)
    return destination


def stage_built_bundle(
    *,
    source: Path,
    build: Path,
    source_sysroot: Path,
    output_root: Path,
    lock: Mapping[str, Any],
    attempt_id: str,
) -> Path:
    """Stage a verified, self-contained RV64 FDB runtime bundle atomically."""

    validate_attempt_id(attempt_id)
    source_root = source.resolve(strict=True)
    build_root = build.resolve(strict=True)
    runtime_root = source_sysroot.resolve(strict=True)
    fdbserver_source = (build_root / "bin/fdbserver").resolve(strict=True)
    client_source = (build_root / "lib/libfdb_c.so").resolve(strict=True)
    artifact_info, dependency_info = collect_target_dependency_closure(
        (fdbserver_source, client_source), runtime_root
    )

    bundle_id = (
        f"fdb-{lock['version']}-{str(lock['source']['commit'])[:12]}-"
        f"rv64-{attempt_id}"
    )
    target = output_root / bundle_id
    if target.exists():
        raise BuildError(f"FDB bundle output already exists: {target}")
    staging = output_root / "attempts" / attempt_id / "bundle-staging"
    if staging.exists():
        raise BuildError(f"FDB bundle staging output already exists: {staging}")
    staging.mkdir(parents=True)
    staged_sysroot = staging / "sysroot"

    fdbserver = _copy_built_artifact(
        fdbserver_source, staged_sysroot / "usr/bin/fdbserver", build_root
    )
    client = _copy_built_artifact(
        client_source, staged_sysroot / "usr/lib/libfdb_c.so", build_root
    )
    staged_dependencies: dict[Path, Path] = {}
    for dependency in dependency_info:
        staged_dependencies[dependency] = copy_target_runtime_file(
            dependency, runtime_root, staged_sysroot
        )

    header_sources = {
        "foundationdb/fdb_c.h": source_root
        / "bindings/c/foundationdb/fdb_c.h",
        "foundationdb/fdb_c_apiversion.g.h": build_root
        / "bindings/c/foundationdb/fdb_c_apiversion.g.h",
        "foundationdb/fdb_c_options.g.h": build_root
        / "bindings/c/foundationdb/fdb_c_options.g.h",
        "foundationdb/fdb_c_types.h": source_root
        / "bindings/c/foundationdb/fdb_c_types.h",
    }
    header_root = staged_sysroot / "usr/include"
    header_hashes: dict[str, str] = {}
    for relative, header_source in header_sources.items():
        resolved_header = header_source.resolve(strict=True)
        if not resolved_header.is_file():
            raise BuildError(f"FDB C API header is absent: {relative}")
        destination = header_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(resolved_header, destination)
        header_hashes[relative] = _sha256_file(destination)

    def needed_paths(info: preflight.ElfInfo) -> list[str]:
        result: list[str] = []
        for needed in info.needed:
            dependency = find_target_runtime_file(runtime_root, needed)
            result.append(str(staged_dependencies[dependency]))
        return result

    commit = str(lock["source"]["commit"])
    bundle: dict[str, Any] = {
        "schema": verify_fdb_bundle.BUNDLE_SCHEMA,
        "version": lock["version"],
        "source": {
            "url": lock["source"]["url"],
            "annotated_tag_object": lock["source"]["annotated_tag_object"],
            "commit": commit,
        },
        "sysroot": str(staged_sysroot),
        "api_versions": list(lock["api_versions"]),
        "source_modifications": copy.deepcopy(lock["source_modifications"]),
        "fdbserver": {
            "path": str(fdbserver),
            "sha256": _sha256_file(fdbserver),
            "machine": "RISC-V",
            "source_commit": commit,
            "interpreter": artifact_info[fdbserver_source].interpreter,
            "needed": needed_paths(artifact_info[fdbserver_source]),
        },
        "libfdb_c": {
            "path": str(client),
            "sha256": _sha256_file(client),
            "machine": "RISC-V",
            "source_commit": commit,
            "interpreter": artifact_info[client_source].interpreter,
            "needed": needed_paths(artifact_info[client_source]),
        },
        "headers": {"root": str(header_root), "files": header_hashes},
        "dependencies": [
            {
                "path": str(staged_dependencies[source_path]),
                "sha256": _sha256_file(staged_dependencies[source_path]),
                "machine": "RISC-V",
            }
            for source_path in sorted(
                staged_dependencies, key=lambda path: path.relative_to(runtime_root).as_posix()
            )
        ],
    }
    errors = verify_fdb_bundle.verify(bundle, lock, check_files=True)
    if errors:
        raise BuildError("staged FDB bundle failed verification: " + "; ".join(errors))

    final_bundle = rewrite_manifest_paths(bundle, staged_sysroot, target / "sysroot")
    (staging / "fdb-bundle-manifest.json").write_text(
        json.dumps(final_bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    staging.rename(target)
    final_errors = verify_fdb_bundle.verify(final_bundle, lock, check_files=True)
    if final_errors:
        raise BuildError("final FDB bundle failed verification: " + "; ".join(final_errors))
    return target / "fdb-bundle-manifest.json"


def _copy_manifested_file(
    source_sysroot: Path, target_sysroot: Path, declared: object
) -> None:
    if not isinstance(declared, str):
        raise BuildError("FDB bundle contains a non-string artifact path")
    source = Path(declared).resolve(strict=True)
    try:
        relative = source.relative_to(source_sysroot)
    except ValueError as error:
        raise BuildError("FDB import artifact escapes its sysroot") from error
    destination = target_sysroot / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.read_bytes() != source.read_bytes():
            raise BuildError(f"conflicting FDB import artifact: {relative}")
        return
    shutil.copy2(source, destination, follow_symlinks=True)


def import_bundle(source_bundle: Path, output_root: Path, lock: Mapping[str, Any]) -> Path:
    source_manifest = source_bundle / "fdb-bundle-manifest.json"
    bundle = load_object(source_manifest)
    errors = verify_fdb_bundle.verify(bundle, lock, check_files=True)
    if errors:
        raise BuildError("invalid imported FDB bundle: " + "; ".join(errors))
    source_sysroot = Path(str(bundle["sysroot"])).resolve(strict=True)
    bundle_id = (
        f"fdb-{lock['version']}-{str(lock['source']['commit'])[:12]}-rv64-import"
    )
    target = output_root / bundle_id
    if target.exists():
        raise BuildError(f"FDB bundle output already exists: {target}")
    output_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{bundle_id}-", dir=output_root))
    temporary_sysroot = temporary / "sysroot"

    artifact_paths: list[object] = [
        bundle["fdbserver"]["path"],
        bundle["libfdb_c"]["path"],
    ]
    artifact_paths.extend(item["path"] for item in bundle["dependencies"])
    for declared in artifact_paths:
        _copy_manifested_file(source_sysroot, temporary_sysroot, declared)
    header_root = Path(str(bundle["headers"]["root"])).resolve(strict=True)
    for relative in bundle["headers"]["files"]:
        _copy_manifested_file(
            source_sysroot, temporary_sysroot, str(header_root / relative)
        )

    temporary_manifest = rewrite_manifest_paths(
        bundle, source_sysroot, temporary_sysroot
    )
    errors = verify_fdb_bundle.verify(temporary_manifest, lock, check_files=True)
    if errors:
        raise BuildError("copied FDB bundle failed verification: " + "; ".join(errors))
    final_manifest = rewrite_manifest_paths(bundle, source_sysroot, target / "sysroot")
    (temporary / "fdb-bundle-manifest.json").write_text(
        json.dumps(final_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.rename(target)
    return target / "fdb-bundle-manifest.json"


def _write_failure(
    output_root: Path, error: Exception, attempt_id: str | None
) -> Path:
    timestamp = dt.datetime.now(dt.timezone.utc)
    if attempt_id is None:
        evidence_directory = output_root / "attempts" / "invalid-attempt"
    else:
        evidence_directory = output_root / "attempts" / attempt_id
    evidence_directory.mkdir(parents=True, exist_ok=True)
    result = {
        "schema": "hf3fs.fdb-riscv-port-result.v1",
        "status": "failed",
        "first_failure": str(error),
        "attempt_id": attempt_id,
        "timestamp_utc": timestamp.isoformat(),
    }
    suffix = timestamp.strftime("%Y%m%dT%H%M%S%fZ")
    destination = evidence_directory / f"fdb-port-result-{suffix}.json"
    temporary = evidence_directory / f".{destination.name}.tmp-{os.getpid()}"
    temporary.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(destination)
    return destination


def _require_source_arguments(arguments: argparse.Namespace) -> None:
    for name in (
        "sysroot",
        "c_compiler",
        "cxx_compiler",
        "cmake",
        "ninja",
        "mono",
        "mcs",
        "boost_archive",
    ):
        value = getattr(arguments, name)
        if value is None or not value.is_absolute():
            raise BuildError(f"--{name.replace('_', '-')} must be an absolute path")
        if not value.exists():
            raise BuildError(f"--{name.replace('_', '-')} does not exist")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--import-bundle", type=Path)
    mode.add_argument("--source-dir", type=Path)
    parser.add_argument("--sysroot", type=Path)
    parser.add_argument("--c-compiler", type=Path)
    parser.add_argument("--cxx-compiler", type=Path)
    parser.add_argument("--cmake", type=Path)
    parser.add_argument("--ninja", type=Path)
    parser.add_argument("--mono", type=Path)
    parser.add_argument("--mcs", type=Path)
    parser.add_argument("--boost-archive", type=Path)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--attempt-id")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--print-only", action="store_true")
    arguments = parser.parse_args(argv)

    attempt_id: str | None = None
    try:
        lock = load_object(arguments.lock)
        output_root = require_output_root(arguments.output_root)
        if arguments.import_bundle is not None:
            manifest = import_bundle(arguments.import_bundle, output_root, lock)
            print(manifest)
            return 0

        _require_source_arguments(arguments)
        attempt_id = validate_attempt_id(
            arguments.attempt_id or default_attempt_id()
        )
        attempt_directory = output_root / "attempts" / attempt_id
        attempt_directory.parent.mkdir(parents=True, exist_ok=True)
        attempt_directory.mkdir()
        log = attempt_directory / "fdb-source-port.log"
        if arguments.source_dir is None:
            raise BuildError("source builds require --source-dir")
        source = arguments.source_dir.resolve(strict=True)
        verify_source_checkout(source, lock)
        if arguments.prepare_only:
            print(source)
            return 0

        build = output_root / "builds" / (
            f"build-{lock['version']}-{str(lock['source']['commit'])[:12]}-"
            f"{attempt_id}"
        )
        command = configure_command(
            source=source,
            build=build,
            cmake=arguments.cmake,
            ninja=arguments.ninja,
            sysroot=arguments.sysroot,
            c_compiler=arguments.c_compiler,
            cxx_compiler=arguments.cxx_compiler,
            mono=arguments.mono,
            mcs=arguments.mcs,
        )
        build_command = [
            str(arguments.cmake),
            "--build",
            str(build),
            "--target",
            "fdbserver",
            "fdb_c",
            "--parallel",
            str(arguments.jobs),
        ]
        if arguments.print_only:
            print(
                json.dumps(
                    {
                        "attempt_id": attempt_id,
                        "configure": command,
                        "build": build_command,
                        "boost_archive": str(arguments.boost_archive),
                    },
                    indent=2,
                )
            )
            return 0
        environment = tool_environment(arguments.cxx_compiler, os.environ)
        _run(command, log=log, environment=environment)
        seed_boost_archive(arguments.boost_archive, build, lock)
        _run(build_command, log=log, environment=environment)
        manifest = stage_built_bundle(
            source=source,
            build=build,
            source_sysroot=arguments.sysroot,
            output_root=output_root,
            lock=lock,
            attempt_id=attempt_id,
        )
        print(manifest)
        return 0
    except (BuildError, OSError, KeyError, TypeError, ValueError) as error:
        try:
            output_root = require_output_root(arguments.output_root)
            result = _write_failure(output_root, error, attempt_id)
            print(result, file=sys.stderr)
        except Exception:
            pass
        print(f"FDB RISC-V preparation failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
