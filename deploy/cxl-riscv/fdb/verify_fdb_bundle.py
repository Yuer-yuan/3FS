#!/usr/bin/env python3
"""Verify provenance and target closure for a native RISC-V FDB bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from collections.abc import Mapping, Sequence
from typing import Any


DEPLOY_DIR = Path(__file__).resolve().parent.parent
if str(DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(DEPLOY_DIR))

import preflight


BUNDLE_SCHEMA = "hf3fs.fdb-riscv-bundle.v1"
LOCK_SCHEMA = "hf3fs.fdb-riscv-lock.v1"
REQUIRED_API_VERSION = 710
REQUIRED_HEADERS = {
    "foundationdb/fdb_c.h",
    "foundationdb/fdb_c_apiversion.g.h",
    "foundationdb/fdb_c_options.g.h",
    "foundationdb/fdb_c_types.h",
}
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_path(value: object) -> Path | None:
    if not isinstance(value, str):
        return None
    path = Path(value)
    if not path.is_absolute():
        return None
    return Path(os.path.normpath(path))


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    return ()


def _locked_source_modification_set(
    lock: Mapping[str, Any],
) -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    for item in _sequence(lock.get("source_modifications")):
        modification = _mapping(item)
        path = modification.get("path")
        sha256 = modification.get("sha256")
        if isinstance(path, str) and isinstance(sha256, str):
            result.add((path, sha256))
    return result


def _verify_actual_file(
    record: Mapping[str, Any],
    *,
    label: str,
    sysroot: Path,
    require_executable: bool,
) -> list[str]:
    errors: list[str] = []
    declared = _canonical_path(record.get("path"))
    if declared is None:
        return [f"{label} path is not absolute"]
    try:
        actual = declared.resolve(strict=True)
    except (OSError, RuntimeError):
        return [f"{label} file is absent"]
    if not actual.is_file():
        return [f"{label} is not a regular file"]
    if not _is_within(actual, sysroot):
        errors.append("FDB target artifact escapes the RISC-V sysroot")
    if require_executable and not os.access(actual, os.X_OK):
        errors.append("fdbserver is not executable")
    expected_hash = record.get("sha256")
    if not isinstance(expected_hash, str) or _sha256_file(actual) != expected_hash:
        errors.append(f"{label} SHA-256 differs from its manifest")
    try:
        info = preflight.inspect_elf(actual)
    except (OSError, UnicodeError, ValueError):
        errors.append(f"{label} is not RISC-V")
    else:
        if not info.is_riscv64_little_endian:
            errors.append(f"{label} is not RISC-V")
    return errors


def _verify_runtime_closure(
    roots: Sequence[Path],
    dependency_records: Sequence[Any],
    sysroot: Path,
) -> list[str]:
    """Match the recursive ELF closure to the dependency manifest."""

    errors: list[str] = []
    declared: set[Path] = set()
    for item in dependency_records:
        path = _canonical_path(_mapping(item).get("path"))
        if path is None:
            continue
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved.is_file() and _is_within(resolved, sysroot):
            declared.add(resolved)

    pending: list[Path] = []
    for root in roots:
        try:
            pending.append(root.resolve(strict=True))
        except (OSError, RuntimeError):
            continue
    visited: set[Path] = set()
    closure: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        try:
            info = preflight.inspect_elf(path)
        except (OSError, UnicodeError, ValueError):
            continue
        references: list[Path] = []
        if info.interpreter is not None:
            if not info.interpreter.startswith("/"):
                errors.append("FDB target interpreter path is not absolute")
            else:
                interpreter = sysroot / info.interpreter.removeprefix("/")
                try:
                    references.append(interpreter.resolve(strict=True))
                except (OSError, RuntimeError):
                    errors.append("FDB target interpreter is absent from its sysroot")
        for needed in info.needed:
            dependency = preflight._find_dependency(sysroot, needed)
            if dependency is None:
                errors.append(
                    "FDB recursive target dependency is absent or ambiguous in its sysroot"
                )
            else:
                references.append(dependency)
        for dependency in references:
            if not dependency.is_file() or not _is_within(dependency, sysroot):
                errors.append("FDB recursive target dependency escapes its sysroot")
                continue
            closure.add(dependency)
            pending.append(dependency)

    if not closure.issubset(declared):
        errors.append("FDB recursive target dependency is absent from the manifest")
    if not declared.issubset(closure):
        errors.append("FDB dependency manifest contains an unreachable target file")
    return errors


def verify(
    bundle: Mapping[str, Any],
    lock: Mapping[str, Any],
    *,
    check_files: bool = True,
) -> list[str]:
    """Return every deterministic bundle error without mutating the input."""

    errors: list[str] = []
    if lock.get("schema") != LOCK_SCHEMA:
        errors.append("FDB lock schema is unsupported")
    if bundle.get("schema") != BUNDLE_SCHEMA:
        errors.append("FDB bundle schema is unsupported")

    lock_source = _mapping(lock.get("source"))
    bundle_source = _mapping(bundle.get("source"))
    locked_identity = (
        lock_source.get("url"),
        lock_source.get("annotated_tag_object"),
        lock_source.get("commit"),
    )
    bundle_identity = (
        bundle_source.get("url"),
        bundle_source.get("annotated_tag_object"),
        bundle_source.get("commit"),
    )
    if bundle_identity != locked_identity or bundle.get("version") != lock.get(
        "version"
    ):
        errors.append("FDB source identity does not match the lock")
    if not isinstance(lock_source.get("commit"), str) or not HEX40.fullmatch(
        str(lock_source.get("commit"))
    ):
        errors.append("FDB lock commit is malformed")

    fdbserver = _mapping(bundle.get("fdbserver"))
    client = _mapping(bundle.get("libfdb_c"))
    server_commit = fdbserver.get("source_commit")
    client_commit = client.get("source_commit")
    if server_commit != client_commit:
        errors.append("FDB server/client source identity differs")
    if server_commit != lock_source.get("commit"):
        errors.append("FDB artifact source identity does not match the lock")

    if fdbserver.get("machine") != "RISC-V":
        errors.append("fdbserver is not RISC-V")
    if client.get("machine") != "RISC-V":
        errors.append("libfdb_c is not RISC-V")
    if REQUIRED_API_VERSION not in _sequence(bundle.get("api_versions")):
        errors.append("FoundationDB C API 710 is unsupported")

    locked_modifications = _locked_source_modification_set(lock)
    declared_modifications: set[tuple[str, str]] = set()
    for item in _sequence(bundle.get("source_modifications")):
        modification = _mapping(item)
        identity = (
            str(modification.get("path", "")),
            str(modification.get("sha256", "")),
        )
        declared_modifications.add(identity)
        if identity not in locked_modifications:
            errors.append("FDB source modification is not lock-pinned")
    if declared_modifications != locked_modifications:
        errors.append("FDB bundle source modifications differ from the lock")

    sysroot = _canonical_path(bundle.get("sysroot"))
    if sysroot is None:
        errors.append("FDB bundle sysroot is not absolute")
    else:
        for artifact in (fdbserver, client):
            artifact_path = _canonical_path(artifact.get("path"))
            if artifact_path is None or not _is_within(artifact_path, sysroot):
                errors.append("FDB target artifact escapes the RISC-V sysroot")
            for needed in _sequence(artifact.get("needed")):
                needed_path = _canonical_path(needed)
                if needed_path is None or not _is_within(needed_path, sysroot):
                    errors.append(
                        "FDB target dependency escapes the RISC-V sysroot"
                    )

        dependencies = _sequence(bundle.get("dependencies"))
        dependency_paths: set[str] = set()
        for item in dependencies:
            dependency = _mapping(item)
            dependency_path = _canonical_path(dependency.get("path"))
            if dependency_path is None or not _is_within(dependency_path, sysroot):
                errors.append("FDB target dependency escapes the RISC-V sysroot")
            else:
                dependency_paths.add(str(dependency_path))
            if dependency.get("machine") != "RISC-V":
                errors.append("FDB target dependency is not RISC-V")
            if not isinstance(dependency.get("sha256"), str) or not HEX64.fullmatch(
                str(dependency.get("sha256"))
            ):
                errors.append("FDB target dependency SHA-256 is malformed")
        for artifact in (fdbserver, client):
            for needed in _sequence(artifact.get("needed")):
                if isinstance(needed, str) and needed.startswith(str(sysroot)):
                    if needed not in dependency_paths:
                        errors.append("FDB target dependency is absent from the manifest")

        headers = _mapping(bundle.get("headers"))
        header_root = _canonical_path(headers.get("root"))
        header_files = _mapping(headers.get("files"))
        if header_root is None or not _is_within(header_root, sysroot):
            errors.append("FDB headers escape the RISC-V sysroot")
        if not REQUIRED_HEADERS.issubset(header_files):
            errors.append("FDB C API header set is incomplete")
        for relative, sha256 in header_files.items():
            relative_path = Path(str(relative))
            if (
                relative_path.is_absolute()
                or ".." in relative_path.parts
                or not isinstance(sha256, str)
                or not HEX64.fullmatch(sha256)
            ):
                errors.append("FDB header manifest entry is malformed")

        if check_files:
            try:
                actual_sysroot = sysroot.resolve(strict=True)
            except (OSError, RuntimeError):
                errors.append("FDB bundle sysroot is absent")
            else:
                if not actual_sysroot.is_dir():
                    errors.append("FDB bundle sysroot is not a directory")
                errors.extend(
                    _verify_actual_file(
                        fdbserver,
                        label="fdbserver",
                        sysroot=actual_sysroot,
                        require_executable=True,
                    )
                )
                errors.extend(
                    _verify_actual_file(
                        client,
                        label="libfdb_c",
                        sysroot=actual_sysroot,
                        require_executable=False,
                    )
                )
                if header_root is not None:
                    for relative, sha256 in header_files.items():
                        if not isinstance(relative, str) or not isinstance(sha256, str):
                            continue
                        declared = header_root / relative
                        try:
                            actual = declared.resolve(strict=True)
                        except (OSError, RuntimeError):
                            errors.append("FDB header file is absent")
                            continue
                        if not actual.is_file() or not _is_within(actual, actual_sysroot):
                            errors.append("FDB header file escapes the RISC-V sysroot")
                        elif _sha256_file(actual) != sha256:
                            errors.append("FDB header SHA-256 differs from its manifest")
                for item in dependencies:
                    dependency = _mapping(item)
                    errors.extend(
                        _verify_actual_file(
                            dependency,
                            label="FDB target dependency",
                            sysroot=actual_sysroot,
                            require_executable=False,
                        )
                    )
                closure_roots: list[Path] = []
                for artifact in (fdbserver, client):
                    artifact_path = _canonical_path(artifact.get("path"))
                    if artifact_path is not None:
                        closure_roots.append(artifact_path)
                errors.extend(
                    _verify_runtime_closure(
                        closure_roots, dependencies, actual_sysroot
                    )
                )

    for artifact, label in ((fdbserver, "fdbserver"), (client, "libfdb_c")):
        if not isinstance(artifact.get("sha256"), str) or not HEX64.fullmatch(
            str(artifact.get("sha256"))
        ):
            errors.append(f"{label} SHA-256 is malformed")

    return list(dict.fromkeys(errors))


def verify_bundle(bundle_path: Path, lock_path: Path) -> list[str]:
    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return [f"cannot read FDB bundle manifest: {error}"]
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return [f"cannot read FDB lock: {error}"]
    if not isinstance(bundle, Mapping) or not isinstance(lock, Mapping):
        return ["FDB bundle and lock must be JSON objects"]
    return verify(bundle, lock)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    arguments = parser.parse_args(argv)
    errors = verify_bundle(arguments.bundle, arguments.lock)
    result = {
        "schema": "hf3fs.fdb-riscv-verification.v1",
        "status": "passed" if not errors else "failed",
        "bundle": str(arguments.bundle.resolve(strict=False)),
        "lock": str(arguments.lock.resolve(strict=False)),
        "errors": errors,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
