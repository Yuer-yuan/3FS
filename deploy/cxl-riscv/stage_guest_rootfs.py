#!/usr/bin/env python3
"""Stage the native RISC-V G0 probes and runtime closure into a fresh rootfs."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from collections.abc import Mapping, Sequence
from typing import Any


DEPLOY_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DEPLOY_DIR.parents[1]
FDB_DIR = DEPLOY_DIR / "fdb"
MANIFEST_SCHEMA = "hf3fs.cxl-riscv-rootfs.v1"
APPLET_NAMES = (
    "awk",
    "basename",
    "cat",
    "chmod",
    "cp",
    "date",
    "find",
    "grep",
    "ip",
    "kill",
    "ln",
    "mkdir",
    "mknod",
    "mount",
    "nc",
    "poweroff",
    "printf",
    "ps",
    "readlink",
    "reboot",
    "rm",
    "sed",
    "seq",
    "sh",
    "sleep",
    "sync",
    "tr",
    "umount",
    "uname",
)

for directory in (DEPLOY_DIR, FDB_DIR):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
import preflight
import build_fdb_riscv
import verify_fdb_bundle


class RootfsStageError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def is_within(candidate: Path, directory: Path) -> bool:
    try:
        candidate.relative_to(directory)
    except ValueError:
        return False
    return True


def require_output(path: Path) -> Path:
    if not path.is_absolute():
        raise RootfsStageError("guest rootfs output must be absolute")
    project = PROJECT_ROOT.resolve(strict=True)
    output = path.resolve(strict=False)
    approved = project / "out/cxl-riscv/rootfs"
    if not is_within(output, approved) or output == approved:
        raise RootfsStageError("guest rootfs output escapes components/3FS/out")
    if output.exists():
        raise RootfsStageError("guest rootfs output already exists")
    return output


def load_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RootfsStageError(f"cannot read {description}: {error}") from error
    if not isinstance(value, dict):
        raise RootfsStageError(f"{description} must contain an object")
    return value


def install_file(source: Path, destination: Path, *, executable: bool | None = None) -> Path:
    actual = source.resolve(strict=True)
    if not actual.is_file():
        raise RootfsStageError(f"staged source is not a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise RootfsStageError(f"staged destination already exists: {destination}")
    shutil.copy2(actual, destination)
    if executable is True:
        destination.chmod(destination.stat().st_mode | 0o111)
    elif executable is False:
        destination.chmod(destination.stat().st_mode & ~0o111)
    return destination


def copy_sysroot_file(source: Path, source_root: Path, destination_root: Path) -> Path:
    root = source_root.resolve(strict=True)
    lexical = Path(os.path.normpath(source))
    try:
        relative = lexical.relative_to(root)
        actual = lexical.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise RootfsStageError("runtime artifact escapes its source sysroot") from error
    if not actual.is_file() or not is_within(actual, root):
        raise RootfsStageError("runtime artifact escapes its source sysroot")
    destination = destination_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_file() or sha256_file(destination) != sha256_file(actual):
            raise RootfsStageError(f"conflicting guest runtime file: {relative}")
        return destination
    shutil.copy2(actual, destination, follow_symlinks=True)
    return destination


def inspect_required_elf(path: Path, label: str) -> preflight.ElfInfo:
    try:
        info = preflight.inspect_elf(path)
    except (OSError, UnicodeError, ValueError) as error:
        raise RootfsStageError(f"{label} is not a valid ELF: {error}") from error
    if not info.is_riscv64_little_endian:
        raise RootfsStageError(f"{label} is not RISC-V")
    for needed in info.needed:
        if needed == "libibverbs.so" or needed.startswith("libibverbs.so."):
            raise RootfsStageError(f"{label} links {needed}")
    return info


def stage_runtime_closure(
    artifacts: Sequence[Path], source_sysroot: Path, destination_root: Path
) -> None:
    """Stage the recursive target closure needed outside the FDB bundle.

    FoundationDB statically links its C++ runtime, while the small 3FS probes
    and fdbcli do not.  Resolve those extra loader-visible names from the same
    pinned cross sysroot used to build all target artifacts.  Prefer a file
    already present in the destination so explicit FDB and FUSE artifacts stay
    authoritative.
    """

    runtime_root = source_sysroot.resolve(strict=True)
    if not runtime_root.is_dir():
        raise RootfsStageError("target runtime sysroot is not a directory")
    destination = destination_root.resolve(strict=True)
    pending = [artifact.resolve(strict=True) for artifact in artifacts]
    visited: set[Path] = set()
    while pending:
        artifact = pending.pop()
        actual = artifact.resolve(strict=True)
        if actual in visited:
            continue
        visited.add(actual)
        info = inspect_required_elf(actual, actual.name)
        references: list[str] = []
        if info.interpreter is not None:
            references.append(info.interpreter)
        references.extend(info.needed)
        for reference in references:
            existing: Path | None
            if reference.startswith("/"):
                candidate = destination / reference.removeprefix("/")
                try:
                    existing = candidate.resolve(strict=True)
                except (OSError, RuntimeError):
                    existing = None
            else:
                existing = preflight._find_dependency(destination, reference)
            if existing is not None:
                pending.append(existing)
                continue
            try:
                source = build_fdb_riscv.find_target_runtime_file(
                    runtime_root, reference
                )
                copied = copy_sysroot_file(source, runtime_root, destination)
            except (build_fdb_riscv.BuildError, OSError, RuntimeError) as error:
                raise RootfsStageError(
                    f"cannot stage target runtime dependency {reference}: {error}"
                ) from error
            pending.append(copied)


def verify_guest_closure(root: Path, binaries: Mapping[str, Path]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    pending: list[Path] = []
    for label, path in binaries.items():
        info = inspect_required_elf(path, label)
        pending.append(path.resolve(strict=True))
        records[label] = {
            "path": str(path),
            "sha256": sha256_file(path),
            "machine": "RISC-V",
            "interpreter": info.interpreter,
            "needed": list(info.needed),
        }
    visited: set[Path] = set()
    while pending:
        artifact = pending.pop()
        if artifact in visited:
            continue
        visited.add(artifact)
        info = inspect_required_elf(artifact, artifact.name)
        references: list[Path] = []
        if info.interpreter is not None:
            if not info.interpreter.startswith("/"):
                raise RootfsStageError("guest ELF interpreter is not absolute")
            references.append(root / info.interpreter.removeprefix("/"))
        for needed in info.needed:
            dependency = preflight._find_dependency(root, needed)
            if dependency is None:
                raise RootfsStageError(f"guest dependency is absent or ambiguous: {needed}")
            references.append(dependency)
        for reference in references:
            try:
                resolved = reference.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise RootfsStageError(f"guest runtime dependency is absent: {reference}") from error
            if not resolved.is_file() or not is_within(resolved, root.resolve(strict=True)):
                raise RootfsStageError("guest runtime dependency escapes the rootfs")
            pending.append(resolved)
    return records


def stage_rootfs(
    *,
    base_rootfs: Path,
    output: Path,
    init: Path,
    fdb_bundle_manifest: Path,
    fdb_lock: Path,
    fdbcli: Path,
    dax_smoke: Path,
    fuse_smoke: Path,
    fdb_smoke: Path,
    fuse_overlay_manifest: Path,
    target_runtime_sysroot: Path,
) -> Path:
    base = base_rootfs.resolve(strict=True)
    if not base.is_dir():
        raise RootfsStageError("base guest rootfs is not a directory")
    destination = require_output(output)
    bundle = load_object(fdb_bundle_manifest, "FDB bundle manifest")
    errors = verify_fdb_bundle.verify_bundle(fdb_bundle_manifest, fdb_lock)
    if errors:
        raise RootfsStageError("FDB bundle verification failed: " + "; ".join(errors))
    fuse = load_object(fuse_overlay_manifest, "FUSE overlay manifest")
    if fuse.get("schema") != "hf3fs.fuse-riscv-overlay.v1":
        raise RootfsStageError("FUSE overlay schema is unsupported")

    shutil.copytree(base, destination, symlinks=True)
    for directory in (
        "opt/3fs/bin",
        "opt/3fs/etc",
        "opt/foundationdb/bin",
        "opt/foundationdb/lib",
        "var/lib/fdb",
        "var/log/fdb",
        "etc",
    ):
        (destination / directory).mkdir(parents=True, exist_ok=True)
    busybox = destination / "bin/busybox"
    if not busybox.is_file():
        raise RootfsStageError("base guest rootfs has no busybox")
    for name in APPLET_NAMES:
        link = destination / "bin" / name
        if not link.exists() and not link.is_symlink():
            link.symlink_to("busybox")

    staged_init = destination / "init"
    if staged_init.exists() or staged_init.is_symlink():
        staged_init.unlink()
    install_file(init, staged_init, executable=True)
    (destination / "etc/passwd").write_text("root:x:0:0:root:/root:/bin/sh\n", encoding="utf-8")
    (destination / "etc/group").write_text("root:x:0:\n", encoding="utf-8")

    source_sysroot = Path(str(bundle["sysroot"])).resolve(strict=True)
    runtime_sources: list[Path] = []
    for key in ("libfdb_c",):
        runtime_sources.append(Path(str(bundle[key]["path"])))
    runtime_sources.extend(Path(str(record["path"])) for record in bundle["dependencies"])
    for source in runtime_sources:
        copy_sysroot_file(source, source_sysroot, destination)

    fuse_root = Path(str(fuse["root"])).resolve(strict=True)
    for relative in (
        "lib/riscv64-linux-gnu/libfuse3.so.3",
        "bin/fusermount3",
    ):
        copy_sysroot_file(fuse_root / relative, fuse_root, destination)

    stage_runtime_closure(
        (
            Path(str(bundle["fdbserver"]["path"])),
            Path(str(bundle["libfdb_c"]["path"])),
            fdbcli,
            dax_smoke,
            fuse_smoke,
            fdb_smoke,
        ),
        target_runtime_sysroot,
        destination,
    )

    binaries = {
        "busybox": busybox,
        "cxl_dax_smoke": install_file(
            dax_smoke, destination / "opt/3fs/bin/cxl_dax_smoke", executable=True
        ),
        "fuse_mount_smoke": install_file(
            fuse_smoke, destination / "opt/3fs/bin/fuse_mount_smoke", executable=True
        ),
        "fdb_client_smoke": install_file(
            fdb_smoke, destination / "opt/3fs/bin/fdb_client_smoke", executable=True
        ),
        "fdbserver": install_file(
            Path(str(bundle["fdbserver"]["path"])),
            destination / "opt/foundationdb/bin/fdbserver",
            executable=True,
        ),
        "fdbcli": install_file(
            fdbcli, destination / "opt/foundationdb/bin/fdbcli", executable=True
        ),
    }
    binary_records = verify_guest_closure(destination, binaries)

    manifest = destination.parent / f"{destination.name}-rootfs-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": MANIFEST_SCHEMA,
                "root": str(destination),
                "base_rootfs": str(base),
                "fdb_bundle_manifest": str(fdb_bundle_manifest.resolve(strict=True)),
                "fuse_overlay_manifest": str(fuse_overlay_manifest.resolve(strict=True)),
                "target_runtime_sysroot": str(target_runtime_sysroot.resolve(strict=True)),
                "binaries": binary_records,
                "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-rootfs", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--init", type=Path, default=DEPLOY_DIR / "guest/init")
    parser.add_argument("--fdb-bundle-manifest", required=True, type=Path)
    parser.add_argument("--fdb-lock", type=Path, default=FDB_DIR / "fdb-riscv.lock.json")
    parser.add_argument("--fdbcli", required=True, type=Path)
    parser.add_argument("--dax-smoke", required=True, type=Path)
    parser.add_argument("--fuse-smoke", required=True, type=Path)
    parser.add_argument("--fdb-smoke", required=True, type=Path)
    parser.add_argument("--fuse-overlay-manifest", required=True, type=Path)
    parser.add_argument("--target-runtime-sysroot", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        manifest = stage_rootfs(
            base_rootfs=arguments.base_rootfs,
            output=arguments.output,
            init=arguments.init,
            fdb_bundle_manifest=arguments.fdb_bundle_manifest,
            fdb_lock=arguments.fdb_lock,
            fdbcli=arguments.fdbcli,
            dax_smoke=arguments.dax_smoke,
            fuse_smoke=arguments.fuse_smoke,
            fdb_smoke=arguments.fdb_smoke,
            fuse_overlay_manifest=arguments.fuse_overlay_manifest,
            target_runtime_sysroot=arguments.target_runtime_sysroot,
        )
    except (RootfsStageError, OSError, KeyError, TypeError, ValueError) as error:
        print(f"RISC-V guest rootfs staging failed: {error}", file=sys.stderr)
        return 2
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
