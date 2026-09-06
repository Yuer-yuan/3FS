#!/usr/bin/env python3
"""Validate and freeze the inputs for the 3FS RISC-V CXL build.

The preflight is intentionally read-only.  In particular, target executables
are inspected as ELF files and are never executed on the build host.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import pathlib
import struct
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any, BinaryIO, TextIO


SCHEMA_VERSION = 1
EM_RISCV = 243
ELFCLASS64 = 2
ELFDATA2LSB = 1

ENV_BY_FIELD = {
    "project_root": "HF3FS_SOURCE_DIR",
    "superproject_root": "HF3FS_SUPERPROJECT_ROOT",
    "qemu": "HF3FS_QEMU",
    "cxlmemsim_server": "HF3FS_CXLMEMSIM_SERVER",
    "cxlmemsim_topology": "HF3FS_CXLMEMSIM_TOPOLOGY",
    "opensbi": "HF3FS_RISCV_OPENSBI",
    "uboot": "HF3FS_RISCV_UBOOT",
    "kernel": "HF3FS_RISCV_KERNEL",
    "kernel_config": "HF3FS_RISCV_KERNEL_CONFIG",
    "sysroot": "HF3FS_RISCV_SYSROOT",
    "rootfs": "HF3FS_RISCV_ROOTFS",
    "c_compiler": "HF3FS_RISCV_CC",
    "cxx_compiler": "HF3FS_RISCV_CXX",
    "cmake": "HF3FS_CMAKE",
    "ninja": "HF3FS_NINJA",
    "fdb_include": "HF3FS_RISCV_FDB_INCLUDE",
    "fdb_client": "HF3FS_RISCV_FDB_CLIENT",
    "fdb_server": "HF3FS_RISCV_FDBSERVER",
    "fuse_include": "HF3FS_RISCV_FUSE_INCLUDE",
    "fuse_library": "HF3FS_RISCV_FUSE_LIBRARY",
}

DIRECTORY_FIELDS = {
    "project_root",
    "superproject_root",
    "sysroot",
    "rootfs",
    "fdb_include",
    "fuse_include",
}
EXECUTABLE_FIELDS = {
    "qemu",
    "cxlmemsim_server",
    "c_compiler",
    "cxx_compiler",
    "cmake",
    "ninja",
    "fdb_server",
}
PLATFORM_FILE_FIELDS = {
    "qemu",
    "cxlmemsim_server",
    "cxlmemsim_topology",
    "opensbi",
    "uboot",
    "kernel",
    "kernel_config",
}
TARGET_SYSROOT_FIELDS = {
    "fdb_include",
    "fdb_client",
    "fdb_server",
    "fuse_include",
    "fuse_library",
}
TARGET_ELF_FIELDS = {"fdb_client", "fdb_server", "fuse_library"}


@dataclasses.dataclass(frozen=True)
class Inputs:
    project_root: pathlib.Path
    superproject_root: pathlib.Path
    qemu: pathlib.Path
    cxlmemsim_server: pathlib.Path
    cxlmemsim_topology: pathlib.Path
    opensbi: pathlib.Path
    uboot: pathlib.Path
    kernel: pathlib.Path
    kernel_config: pathlib.Path
    sysroot: pathlib.Path
    rootfs: pathlib.Path
    c_compiler: pathlib.Path
    cxx_compiler: pathlib.Path
    cmake: pathlib.Path
    ninja: pathlib.Path
    fdb_include: pathlib.Path
    fdb_client: pathlib.Path
    fdb_server: pathlib.Path
    fuse_include: pathlib.Path
    fuse_library: pathlib.Path

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> "Inputs":
        missing = sorted(
            env_name
            for env_name in ENV_BY_FIELD.values()
            if not env.get(env_name, "").strip()
        )
        if missing:
            raise ValueError("missing environment values: " + ", ".join(missing))
        return cls(
            **{
                field_name: pathlib.Path(env[env_name])
                for field_name, env_name in ENV_BY_FIELD.items()
            }
        )


@dataclasses.dataclass(frozen=True)
class ElfInfo:
    machine: int
    elf_class: int
    byte_order: int
    interpreter: str | None
    needed: tuple[str, ...]

    @property
    def is_riscv64_little_endian(self) -> bool:
        return (
            self.machine == EM_RISCV
            and self.elf_class == ELFCLASS64
            and self.byte_order == ELFDATA2LSB
        )


def _read_exact(handle: BinaryIO, offset: int, size: int) -> bytes:
    if offset < 0 or size < 0:
        raise ValueError("negative ELF file range")
    handle.seek(offset)
    data = handle.read(size)
    if len(data) != size:
        raise ValueError("truncated ELF file")
    return data


def inspect_elf(path: pathlib.Path) -> ElfInfo:
    """Read enough ELF64 metadata to validate target identity and dependencies."""

    with path.open("rb") as handle:
        header = _read_exact(handle, 0, 64)
        if header[:4] != b"\x7fELF":
            raise ValueError("missing ELF magic")
        elf_class = header[4]
        byte_order = header[5]
        if elf_class != ELFCLASS64 or byte_order != ELFDATA2LSB:
            return ElfInfo(
                machine=struct.unpack_from("<H", header, 18)[0],
                elf_class=elf_class,
                byte_order=byte_order,
                interpreter=None,
                needed=(),
            )

        unpacked = struct.unpack("<16sHHIQQQIHHHHHH", header)
        machine = unpacked[2]
        program_header_offset = unpacked[5]
        program_header_entry_size = unpacked[9]
        program_header_count = unpacked[10]
        if program_header_count > 4096:
            raise ValueError("unreasonable ELF program-header count")
        if program_header_count and program_header_entry_size < 56:
            raise ValueError("short ELF64 program-header entry")

        program_headers: list[tuple[int, int, int, int]] = []
        interpreter: str | None = None
        dynamic_range: tuple[int, int] | None = None
        for index in range(program_header_count):
            offset = program_header_offset + index * program_header_entry_size
            entry = _read_exact(handle, offset, program_header_entry_size)
            (
                segment_type,
                _flags,
                file_offset,
                virtual_address,
                _physical_address,
                file_size,
                _memory_size,
                _alignment,
            ) = struct.unpack_from("<IIQQQQQQ", entry)
            program_headers.append(
                (segment_type, file_offset, virtual_address, file_size)
            )
            if segment_type == 3:  # PT_INTERP
                if file_size == 0 or file_size > 4096:
                    raise ValueError("invalid PT_INTERP size")
                raw = _read_exact(handle, file_offset, file_size)
                if not raw.endswith(b"\0"):
                    raise ValueError("unterminated PT_INTERP")
                interpreter = raw[:-1].decode("utf-8", errors="strict")
            elif segment_type == 2:  # PT_DYNAMIC
                dynamic_range = (file_offset, file_size)

        needed_offsets: list[int] = []
        string_table_address: int | None = None
        string_table_size: int | None = None
        if dynamic_range is not None:
            dynamic_offset, dynamic_size = dynamic_range
            if dynamic_size % 16 != 0 or dynamic_size > 16 * 1024 * 1024:
                raise ValueError("invalid PT_DYNAMIC size")
            for entry_offset in range(dynamic_offset, dynamic_offset + dynamic_size, 16):
                tag, value = struct.unpack("<QQ", _read_exact(handle, entry_offset, 16))
                if tag == 0:  # DT_NULL
                    break
                if tag == 1:  # DT_NEEDED
                    needed_offsets.append(value)
                elif tag == 5:  # DT_STRTAB
                    string_table_address = value
                elif tag == 10:  # DT_STRSZ
                    string_table_size = value

        needed: list[str] = []
        if needed_offsets:
            if string_table_address is None or string_table_size is None:
                raise ValueError("DT_NEEDED without a complete dynamic string table")
            if string_table_size > 64 * 1024 * 1024:
                raise ValueError("unreasonable ELF dynamic string-table size")
            string_table_offset: int | None = None
            for segment_type, file_offset, virtual_address, file_size in program_headers:
                if (
                    segment_type == 1
                    and virtual_address <= string_table_address
                    and string_table_address - virtual_address < file_size
                ):
                    string_table_offset = (
                        file_offset + string_table_address - virtual_address
                    )
                    break
            if string_table_offset is None:
                raise ValueError("dynamic string table is outside a PT_LOAD segment")
            string_table = _read_exact(handle, string_table_offset, string_table_size)
            for needed_offset in needed_offsets:
                if needed_offset >= len(string_table):
                    raise ValueError("DT_NEEDED offset is outside the string table")
                end = string_table.find(b"\0", needed_offset)
                if end < 0:
                    raise ValueError("unterminated DT_NEEDED name")
                name = string_table[needed_offset:end].decode(
                    "utf-8", errors="strict"
                )
                if not name or "/" in name:
                    raise ValueError("invalid DT_NEEDED name")
                needed.append(name)

        return ElfInfo(
            machine=machine,
            elf_class=elf_class,
            byte_order=byte_order,
            interpreter=interpreter,
            needed=tuple(needed),
        )


def _is_within(path: pathlib.Path, directory: pathlib.Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _resolve_existing(path: pathlib.Path) -> pathlib.Path | None:
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def _find_dependency(sysroot: pathlib.Path, name: str) -> pathlib.Path | None:
    candidates: set[pathlib.Path] = set()
    common_directories = (
        sysroot / "lib",
        sysroot / "lib64",
        sysroot / "usr" / "lib",
        sysroot / "usr" / "lib64",
    )
    for directory in common_directories:
        candidate = _resolve_existing(directory / name)
        if candidate is not None and candidate.is_file() and _is_within(candidate, sysroot):
            candidates.add(candidate)
    if not candidates:
        try:
            matches = sysroot.rglob(name)
            for match in matches:
                candidate = _resolve_existing(match)
                if (
                    candidate is not None
                    and candidate.is_file()
                    and _is_within(candidate, sysroot)
                ):
                    candidates.add(candidate)
        except OSError:
            return None
    if len(candidates) != 1:
        return None
    return next(iter(candidates))


def _dependency_errors(
    root: pathlib.Path, env_name: str, sysroot: pathlib.Path
) -> list[str]:
    errors: list[str] = []
    pending = [root]
    visited: set[pathlib.Path] = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        try:
            info = inspect_elf(path)
        except (OSError, UnicodeError, ValueError) as error:
            errors.append(f"{env_name} has invalid target ELF dependency {path}: {error}")
            continue
        if not info.is_riscv64_little_endian:
            errors.append(f"{env_name} target dependency is not RISC-V ELF64: {path}")
            continue
        if info.interpreter is not None:
            if not info.interpreter.startswith("/"):
                errors.append(f"{env_name} has a non-absolute target interpreter")
            else:
                interpreter = _resolve_existing(
                    sysroot / info.interpreter.removeprefix("/")
                )
                if (
                    interpreter is None
                    or not interpreter.is_file()
                    or not _is_within(interpreter, sysroot)
                ):
                    errors.append(
                        f"{env_name} target interpreter is absent from HF3FS_RISCV_SYSROOT"
                    )
                else:
                    pending.append(interpreter)
        for needed in info.needed:
            dependency = _find_dependency(sysroot, needed)
            if dependency is None:
                errors.append(
                    f"{env_name} target dependency {needed} is absent or ambiguous "
                    "in HF3FS_RISCV_SYSROOT"
                )
            else:
                pending.append(dependency)
    return errors


def validate(
    inputs: Inputs,
    is_executable: Callable[[pathlib.Path, int], bool] = os.access,
) -> list[str]:
    errors: list[str] = []
    resolved: dict[str, pathlib.Path] = {}

    for field in dataclasses.fields(inputs):
        field_name = field.name
        env_name = ENV_BY_FIELD[field_name]
        path = getattr(inputs, field_name)
        if not path.is_absolute():
            errors.append(f"{env_name} must be absolute")
            continue
        target = _resolve_existing(path)
        if target is None:
            errors.append(f"{env_name} does not exist or cannot be resolved")
            continue
        resolved[field_name] = target

        if field_name in DIRECTORY_FIELDS:
            if not target.is_dir():
                errors.append(f"{env_name} is not a directory")
        elif not target.is_file():
            errors.append(f"{env_name} is not a regular file")

        if field_name in EXECUTABLE_FIELDS and not is_executable(target, os.X_OK):
            errors.append(f"{env_name} is not executable")

    superproject = resolved.get("superproject_root")
    project = resolved.get("project_root")
    if superproject is not None and project is not None:
        expected_project = _resolve_existing(superproject / "components" / "3FS")
        if expected_project is None or project != expected_project:
            errors.append(
                "HF3FS_SOURCE_DIR must resolve to "
                "HF3FS_SUPERPROJECT_ROOT/components/3FS"
            )
        for field_name in sorted(PLATFORM_FILE_FIELDS):
            target = resolved.get(field_name)
            if target is not None and not _is_within(target, superproject):
                errors.append(
                    f"{ENV_BY_FIELD[field_name]} resolves outside "
                    "HF3FS_SUPERPROJECT_ROOT"
                )

    sysroot = resolved.get("sysroot")
    if sysroot is not None:
        for field_name in sorted(TARGET_SYSROOT_FIELDS):
            target = resolved.get(field_name)
            if target is not None and not _is_within(target, sysroot):
                errors.append(
                    f"{ENV_BY_FIELD[field_name]} resolves outside "
                    "HF3FS_RISCV_SYSROOT"
                )

        for field_name in sorted(TARGET_ELF_FIELDS):
            target = resolved.get(field_name)
            if target is None or not target.is_file():
                continue
            env_name = ENV_BY_FIELD[field_name]
            try:
                info = inspect_elf(target)
            except (OSError, UnicodeError, ValueError):
                errors.append(f"{env_name} is not RISC-V ELF64")
                continue
            if not info.is_riscv64_little_endian:
                errors.append(f"{env_name} is not RISC-V ELF64")
                continue
            errors.extend(_dependency_errors(target, env_name, sysroot))

    return list(dict.fromkeys(errors))


def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_report(inputs: Inputs) -> dict[str, Any]:
    errors = validate(inputs)
    resolved: dict[str, dict[str, Any]] = {}
    for field in dataclasses.fields(inputs):
        field_name = field.name
        input_path = getattr(inputs, field_name)
        entry: dict[str, Any] = {"input_path": str(input_path)}
        target = _resolve_existing(input_path) if input_path.is_absolute() else None
        if target is not None:
            entry["path"] = str(target)
            if target.is_dir():
                entry["kind"] = "directory"
            elif target.is_file():
                entry["kind"] = "file"
                try:
                    entry["size"] = target.stat().st_size
                    entry["sha256"] = _sha256_file(target)
                except OSError as error:
                    errors.append(
                        f"{ENV_BY_FIELD[field_name]} could not be hashed: {error}"
                    )
                if field_name in TARGET_ELF_FIELDS:
                    try:
                        info = inspect_elf(target)
                        entry["elf"] = {
                            "class": info.elf_class,
                            "byte_order": info.byte_order,
                            "machine": info.machine,
                            "interpreter": info.interpreter,
                            "needed": list(info.needed),
                        }
                    except (OSError, UnicodeError, ValueError):
                        pass
            else:
                entry["kind"] = "other"
        resolved[field_name] = entry

    errors = list(dict.fromkeys(errors))
    platform = {name: resolved[name] for name in sorted(PLATFORM_FILE_FIELDS)}
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "passed" if not errors else "failed",
        "resolved": resolved,
        "errors": errors,
        "identity_sha256": _canonical_sha256(resolved),
        "platform_identity_sha256": _canonical_sha256(platform),
    }


def _missing_environment_report(error: ValueError) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "failed",
        "resolved": {},
        "errors": [str(error)],
        "identity_sha256": None,
        "platform_identity_sha256": None,
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="Validate immutable inputs for the 3FS RISC-V CXL build"
    )
    parser.parse_args(argv)
    environment = os.environ if env is None else env
    destination = sys.stdout if stdout is None else stdout
    try:
        inputs = Inputs.from_environment(environment)
    except ValueError as error:
        report = _missing_environment_report(error)
    else:
        report = build_report(inputs)
    json.dump(report, destination, sort_keys=True, indent=2)
    destination.write("\n")
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
