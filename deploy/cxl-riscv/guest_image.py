#!/usr/bin/env python3
"""Validate the 3FS RISC-V guest contract and create an ext4 image."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence


DEPLOY_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DEPLOY_DIR.parents[1]
DEFAULT_FRAGMENT = DEPLOY_DIR / "kernel.fragment"
IMAGE_SCHEMA = "hf3fs.cxl-riscv-guest-image.v1"
CLONE_SCHEMA = "hf3fs.cxl-riscv-guest-image-clone.v1"
CONFIG_SCHEMA = "hf3fs.cxl-riscv-guest-config-image.v1"
CONFIG_VALUE = re.compile(r"^(CONFIG_[A-Z0-9_]+)=(.+)$")
CONFIG_DISABLED = re.compile(r"^# (CONFIG_[A-Z0-9_]+) is not set$")

PLATFORM_SMOKE_BINARIES = (
    "/opt/3fs/bin/cxl_dax_smoke",
    "/opt/3fs/bin/fuse_mount_smoke",
    "/opt/3fs/bin/fdb_client_smoke",
    "/opt/foundationdb/bin/fdbserver",
)
FULL_3FS_BINARIES = PLATFORM_SMOKE_BINARIES + (
    "/opt/3fs/bin/mgmtd_main",
    "/opt/3fs/bin/meta_main",
    "/opt/3fs/bin/storage_main",
    "/opt/3fs/bin/hf3fs_fuse_main",
    "/opt/3fs/bin/cxl-fabricd",
    "/opt/3fs/bin/admin_cli",
    "/usr/bin/lsblk",
    "/usr/sbin/blkid",
)


class GuestImageError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def parse_kernel_config(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        value_match = CONFIG_VALUE.fullmatch(line)
        disabled_match = CONFIG_DISABLED.fullmatch(line)
        if value_match is not None:
            key, value = value_match.groups()
        elif disabled_match is not None:
            key, value = disabled_match.group(1), "n"
        else:
            continue
        if key in result:
            raise GuestImageError(f"duplicate kernel configuration key: {key}")
        result[key] = value
    return result


def required_kernel_options(fragment: Path = DEFAULT_FRAGMENT) -> dict[str, str]:
    try:
        options = parse_kernel_config(fragment.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as error:
        raise GuestImageError(f"cannot read kernel fragment: {error}") from error
    if not options:
        raise GuestImageError("kernel fragment contains no requirements")
    return options


def validate_kernel_config(
    config: Path, fragment: Path = DEFAULT_FRAGMENT
) -> list[str]:
    required = required_kernel_options(fragment)
    try:
        actual = parse_kernel_config(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, GuestImageError) as error:
        return [f"cannot read guest kernel configuration: {error}"]
    return [
        f"{key} must be {expected}, found {actual.get(key, 'missing')}"
        for key, expected in required.items()
        if actual.get(key) != expected
    ]


def validate_elf_machine(machines: Mapping[str, str]) -> list[str]:
    return [
        f"{path} is not RISC-V"
        for path, machine in machines.items()
        if machine != "RISC-V"
    ]


def required_binaries(profile: str) -> tuple[str, ...]:
    if profile == "platform-smoke":
        return PLATFORM_SMOKE_BINARIES
    if profile == "full-3fs":
        return FULL_3FS_BINARIES
    raise ValueError("guest profile must be platform-smoke or full-3fs")


def _rootfs_path(root: Path, declared: str) -> Path:
    if not declared.startswith("/"):
        raise GuestImageError("guest path must be absolute")
    return root / declared.removeprefix("/")


def validate_rootfs(root: Path, profile: str) -> list[str]:
    """Validate required files without executing target code on the host."""

    try:
        import preflight
    except ImportError as error:
        raise GuestImageError(f"cannot load ELF verifier: {error}") from error

    errors: list[str] = []
    try:
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError):
        return ["guest rootfs is absent"]
    if not resolved_root.is_dir():
        return ["guest rootfs is not a directory"]
    for declared in required_binaries(profile):
        candidate = _rootfs_path(resolved_root, declared)
        try:
            actual = candidate.resolve(strict=True)
            actual.relative_to(resolved_root)
        except (OSError, RuntimeError, ValueError):
            errors.append(f"{declared} is absent or escapes the guest rootfs")
            continue
        if not actual.is_file():
            errors.append(f"{declared} is not a regular file")
            continue
        try:
            info = preflight.inspect_elf(actual)
        except (OSError, UnicodeError, ValueError):
            errors.append(f"{declared} is not a valid ELF")
            continue
        if not info.is_riscv64_little_endian:
            errors.append(f"{declared} is not RISC-V")
        for needed in info.needed:
            if needed == "libibverbs.so" or needed.startswith("libibverbs.so."):
                errors.append(f"{declared} links {needed}")
    return errors


def _owned_existing_image(output: Path, manifest: Path) -> bool:
    try:
        record = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(record, dict)
        and record.get("schema") == IMAGE_SCHEMA
        and record.get("image") == str(output)
        and record.get("sha256") == _sha256_file(output)
    )


def immutable_image_receipt(path: Path) -> dict[str, int]:
    status = path.stat()
    return {
        "device": status.st_dev,
        "inode": status.st_ino,
        "bytes": status.st_size,
        "mtime_ns": status.st_mtime_ns,
        "ctime_ns": status.st_ctime_ns,
        "mode": status.st_mode & 0o777,
    }


def rootfs_image_identity(root: Path, record: dict, image_bytes: int) -> tuple[str, dict]:
    """Bind cached image content while ignoring run-specific manifest paths."""
    resolved = root.resolve(strict=True)
    binaries = {}
    for name, artifact in record["binaries"].items():
        path = Path(artifact["path"]).resolve(strict=True)
        if not path.is_relative_to(resolved):
            raise GuestImageError("rootfs binary escapes cached image root: " + name)
        binaries[str(path.relative_to(resolved))] = artifact["sha256"]
    base_manifest_name = record.get("base_rootfs_manifest")
    base_manifest_sha256 = (
        _sha256_file(Path(base_manifest_name).resolve(strict=True)) if base_manifest_name else None)
    content = {
        "schema": "hf3fs.cxl-riscv-image-content.v1",
        "image_bytes": image_bytes,
        "base_rootfs_manifest_sha256": base_manifest_sha256,
        "binaries": binaries,
        "runtime_files": record.get("runtime_files", {}),
        "guest_tool_files": record.get("guest_tools", {}).get("files", {}),
        "bootstrap_files": record.get("bootstrap_files", {}),
        "io500_manifest_sha256": (record.get("io500_bundle") or {}).get("manifest_sha256"),
    }
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest(), content


def create_image(
    *,
    rootfs: Path,
    output: Path,
    image_bytes: int,
    truncate: Path,
    mkfs_ext4: Path,
    e2fsck: Path,
    replace_generated: bool = False,
) -> Path:
    if image_bytes <= 0:
        raise GuestImageError("guest image size must be positive")
    root = rootfs.resolve(strict=True)
    destination = Path(os.path.normpath(output))
    if not destination.is_absolute():
        raise GuestImageError("guest image output must be absolute")
    manifest = destination.with_suffix(destination.suffix + ".manifest.json")
    if destination.exists() and not (
        replace_generated and _owned_existing_image(destination, manifest)
    ):
        raise GuestImageError("refusing to replace an unowned guest image")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + f".tmp-{os.getpid()}")
    if temporary.exists():
        raise GuestImageError("guest image temporary path already exists")
    commands = (
        (str(truncate), "-s", str(image_bytes), str(temporary)),
        (
            str(mkfs_ext4),
            "-F",
            "-E",
            "lazy_itable_init=0,lazy_journal_init=0",
            "-d",
            str(root),
            str(temporary),
        ),
        (str(e2fsck), "-fn", str(temporary)),
    )
    for command in commands:
        subprocess.run(command, check=True)
    temporary.replace(destination)
    record = {
        "schema": IMAGE_SCHEMA,
        "image": str(destination),
        "sha256": _sha256_file(destination),
        "bytes": destination.stat().st_size,
        "rootfs": str(root),
        "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "commands": [list(command) for command in commands],
    }
    temporary_manifest = manifest.with_suffix(manifest.suffix + f".tmp-{os.getpid()}")
    temporary_manifest.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary_manifest.replace(manifest)
    return manifest


def ensure_cached_image(**options) -> tuple[dict, bool]:
    """Create an immutable content-addressed base image once, or reuse it cheaply."""
    output = Path(options["output"])
    manifest = output.with_suffix(output.suffix + ".manifest.json")
    if output.exists() or manifest.exists():
        try:
            record = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            record = None
        if (not output.is_file() or not isinstance(record, dict) or
                record.get("schema") != IMAGE_SCHEMA or record.get("image") != str(output) or
                not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", ""))) or
                record.get("immutable_receipt") != immutable_image_receipt(output) or
                immutable_image_receipt(output)["mode"] & 0o222):
            raise GuestImageError("cached guest image is incomplete or changed")
        return record, True
    created = create_image(**options)
    output.chmod(0o444)
    record = json.loads(created.read_text(encoding="utf-8"))
    record["immutable_receipt"] = immutable_image_receipt(output)
    created.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record, False


def create_config_image(
    *,
    output: Path,
    patches: Mapping[str, Path],
    image_bytes: int,
    truncate: Path,
    mkfs_ext4: Path,
    e2fsck: Path,
) -> Path:
    """Create a small read-only ext4 disk containing node-specific overlay files."""
    destination = Path(os.path.normpath(output))
    if not destination.is_absolute() or destination.exists() or image_bytes <= 0:
        raise GuestImageError("config image output must be a fresh absolute path with positive size")
    normalized: dict[str, Path] = {}
    for guest_path, source in patches.items():
        if not re.fullmatch(r"/[A-Za-z0-9_.+/-]+", guest_path) or ".." in Path(guest_path).parts:
            raise GuestImageError(f"invalid guest patch path: {guest_path}")
        host_path = source.resolve(strict=True)
        if not host_path.is_file():
            raise GuestImageError(f"invalid guest patch source: {source}")
        normalized[guest_path] = host_path
    if not normalized:
        raise GuestImageError("config image requires at least one file")

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_suffix(destination.suffix + f".root-{os.getpid()}")
    patch_hashes = {path: _sha256_file(source) for path, source in sorted(normalized.items())}
    try:
        staging.mkdir()
        for guest_path, source in normalized.items():
            target = staging / guest_path.removeprefix("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        manifest = create_image(
            rootfs=staging,
            output=destination,
            image_bytes=image_bytes,
            truncate=truncate,
            mkfs_ext4=mkfs_ext4,
            e2fsck=e2fsck,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    record = json.loads(manifest.read_text(encoding="utf-8"))
    record.update(schema=CONFIG_SCHEMA, patches=patch_hashes)
    manifest.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    destination.chmod(0o444)
    return manifest


def clone_patched_image(
    *,
    base_image: Path,
    base_sha256: str,
    output: Path,
    patches: Mapping[str, Path],
    copy: Path,
    debugfs: Path,
    e2fsck: Path,
) -> Path:
    """Clone a verified sparse ext4 base and inject small node-specific files."""
    base = base_image.resolve(strict=True)
    destination = Path(os.path.normpath(output))
    if not destination.is_absolute() or destination.exists():
        raise GuestImageError("patched image output must be a fresh absolute path")
    if not re.fullmatch(r"[0-9a-f]{64}", base_sha256):
        raise GuestImageError("base image SHA-256 is invalid")
    normalized = {}
    for guest_path, source in patches.items():
        if not re.fullmatch(r"/[A-Za-z0-9_.+/-]+", guest_path) or ".." in Path(guest_path).parts:
            raise GuestImageError(f"invalid guest patch path: {guest_path}")
        host_path = source.resolve(strict=True)
        if not host_path.is_file() or any(character.isspace() for character in str(host_path)):
            raise GuestImageError(f"invalid guest patch source: {source}")
        normalized[guest_path] = host_path
    if not normalized:
        raise GuestImageError("patched image requires at least one file")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + f".tmp-{os.getpid()}")
    script = destination.with_suffix(destination.suffix + f".debugfs-{os.getpid()}")
    verify = destination.with_suffix(destination.suffix + f".verify-{os.getpid()}")
    if any(path.exists() for path in (temporary, script, verify)):
        raise GuestImageError("patched image temporary path already exists")
    commands = []
    patch_hashes = {path: _sha256_file(source) for path, source in sorted(normalized.items())}
    try:
        clone = (str(copy), "--sparse=always", "--reflink=auto", "--", str(base), str(temporary))
        subprocess.run(clone, check=True)
        temporary.chmod(0o644)
        commands.append(list(clone))
        parents = sorted({str(parent) for path in normalized for parent in list(Path(path).parents)[:-1]},
                         key=lambda path: (path.count("/"), path))
        lines = [*(f"mkdir {parent}" for parent in parents if parent != "/")]
        for guest_path, source in sorted(normalized.items()):
            lines.extend((f"rm {guest_path}", f"write {source} {guest_path}"))
        script.write_text("\n".join(lines) + "\n", encoding="utf-8")
        patch_command = (str(debugfs), "-w", "-f", str(script), str(temporary))
        subprocess.run(patch_command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        commands.append(list(patch_command))
        for flags in ("-fy", "-fn"):
            check_command = (str(e2fsck), flags, str(temporary))
            completed = subprocess.run(check_command, check=False, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
            if completed.returncode != 0 and not (flags == "-fy" and completed.returncode == 1):
                raise subprocess.CalledProcessError(completed.returncode, check_command)
            commands.append(list(check_command))

        verify.mkdir()
        dump_lines = []
        outputs = {}
        for index, guest_path in enumerate(sorted(normalized)):
            host_path = verify / str(index)
            outputs[guest_path] = host_path
            dump_lines.append(f"dump {guest_path} {host_path}")
        script.write_text("\n".join(dump_lines) + "\n", encoding="utf-8")
        subprocess.run((str(debugfs), "-f", str(script), str(temporary)), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for guest_path, host_path in outputs.items():
            if not host_path.is_file() or _sha256_file(host_path) != patch_hashes[guest_path]:
                raise GuestImageError(f"guest image patch verification failed: {guest_path}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
        script.unlink(missing_ok=True)
        shutil.rmtree(verify, ignore_errors=True)

    identity = hashlib.sha256(json.dumps(
        {"base_sha256": base_sha256, "patches": patch_hashes}, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    record = {"schema": CLONE_SCHEMA, "image": str(destination), "bytes": destination.stat().st_size,
              "base_image": str(base), "base_sha256": base_sha256, "patches": patch_hashes,
              "content_identity_sha256": identity, "commands": commands,
              "created_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
    manifest = destination.with_suffix(destination.suffix + ".manifest.json")
    manifest.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rootfs", required=True, type=Path)
    parser.add_argument("--profile", required=True, choices=("platform-smoke", "full-3fs"))
    parser.add_argument("--kernel-config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--image-bytes", type=int, default=2 * 1024**3)
    parser.add_argument("--truncate", type=Path, default=Path("/usr/bin/truncate"))
    parser.add_argument("--mkfs-ext4", type=Path, default=Path("/usr/sbin/mkfs.ext4"))
    parser.add_argument("--e2fsck", type=Path, default=Path("/usr/sbin/e2fsck"))
    parser.add_argument("--replace-generated", action="store_true")
    arguments = parser.parse_args(argv)
    errors = validate_kernel_config(arguments.kernel_config)
    errors.extend(validate_rootfs(arguments.rootfs, arguments.profile))
    if errors:
        print(json.dumps({"status": "failed", "errors": errors}, indent=2))
        return 2
    manifest = create_image(
        rootfs=arguments.rootfs,
        output=arguments.output,
        image_bytes=arguments.image_bytes,
        truncate=arguments.truncate,
        mkfs_ext4=arguments.mkfs_ext4,
        e2fsck=arguments.e2fsck,
        replace_generated=arguments.replace_generated,
    )
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
