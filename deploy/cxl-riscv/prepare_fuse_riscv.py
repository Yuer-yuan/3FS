#!/usr/bin/env python3
"""Prepare a hash-locked RISC-V FUSE3 overlay without installing packages."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any


DEPLOY_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DEPLOY_DIR.parents[1]
DEFAULT_LOCK = DEPLOY_DIR / "fuse-riscv.lock.json"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "out/cxl-riscv/fuse"
LOCK_SCHEMA = "hf3fs.fuse-riscv-lock.v1"
MANIFEST_SCHEMA = "hf3fs.fuse-riscv-overlay.v1"
REQUIRED_PATHS = (
    "usr/include/fuse3/fuse_lowlevel.h",
    "usr/include/fuse3/fuse_opt.h",
    "usr/lib/riscv64-linux-gnu/libfuse3.so",
    "lib/riscv64-linux-gnu/libfuse3.so.3",
    "bin/fusermount3",
)

if str(DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(DEPLOY_DIR))
import preflight


class FusePreparationError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def is_within(candidate: Path, directory: Path) -> bool:
    try:
        candidate.relative_to(directory)
    except ValueError:
        return False
    return True


def require_output_root(path: Path) -> Path:
    if not path.is_absolute():
        raise FusePreparationError("FUSE output root must be absolute")
    project = PROJECT_ROOT.resolve(strict=True)
    output = path.resolve(strict=False)
    approved = project / "out/cxl-riscv/fuse"
    if not is_within(output, approved):
        raise FusePreparationError("FUSE output root escapes components/3FS/out")
    return output


def load_lock(path: Path) -> dict[str, Any]:
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FusePreparationError(f"cannot read FUSE lock: {error}") from error
    if not isinstance(lock, dict) or lock.get("schema") != LOCK_SCHEMA:
        raise FusePreparationError("FUSE lock schema is unsupported")
    packages = lock.get("packages")
    if not isinstance(packages, list) or not packages:
        raise FusePreparationError("FUSE lock package list is empty")
    names: set[str] = set()
    for record in packages:
        if not isinstance(record, Mapping):
            raise FusePreparationError("FUSE package record is not an object")
        name = record.get("package")
        url = record.get("url")
        digest = record.get("sha256")
        size = record.get("size")
        parsed = urllib.parse.urlparse(str(url))
        if (
            not isinstance(name, str)
            or not name
            or name in names
            or record.get("architecture") != "riscv64"
            or record.get("version") != lock.get("version")
            or parsed.scheme != "https"
            or parsed.hostname != "ports.ubuntu.com"
            or not parsed.path.startswith("/ubuntu-ports/pool/main/f/fuse3/")
            or not parsed.path.endswith("_riscv64.deb")
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
        ):
            raise FusePreparationError("FUSE package record is malformed")
        names.add(name)
    if names != {"libfuse3-dev", "libfuse3-3", "fuse3"}:
        raise FusePreparationError("FUSE package set is incomplete")
    return lock


def download(record: Mapping[str, Any], downloads: Path, allow: bool) -> Path:
    digest = str(record["sha256"])
    size = int(record["size"])
    destination = downloads / f"{digest}.deb"
    if destination.exists():
        if not destination.is_file() or destination.stat().st_size != size or sha256_file(destination) != digest:
            raise FusePreparationError(f"cached FUSE package differs from lock: {record['package']}")
        return destination
    if not allow:
        raise FusePreparationError(
            f"locked FUSE package is absent; rerun with --allow-download: {record['package']}"
        )
    downloads.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.part-{os.getpid()}")
    request = urllib.request.Request(str(record["url"]), headers={"User-Agent": "hf3fs-cxl-g0/1"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            if response.geturl() != record["url"]:
                raise FusePreparationError("FUSE package URL redirected unexpectedly")
            with temporary.open("xb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
        if temporary.stat().st_size != size or sha256_file(temporary) != digest:
            raise FusePreparationError(f"downloaded FUSE package differs from lock: {record['package']}")
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def package_metadata(dpkg_deb: Path, archive: Path) -> tuple[str, str, str]:
    completed = subprocess.run(
        [str(dpkg_deb), "--field", str(archive), "Package", "Version", "Architecture"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        env={**os.environ, "LANG": "C", "LC_ALL": "C"},
    )
    values: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            values[key] = value
    return values.get("Package", ""), values.get("Version", ""), values.get("Architecture", "")


def validate_overlay(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative in REQUIRED_PATHS:
        declared = root / relative
        try:
            resolved = declared.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise FusePreparationError(f"prepared FUSE path is absent: {relative}") from error
        if not resolved.is_file() or not is_within(resolved, root.resolve(strict=True)):
            raise FusePreparationError(f"prepared FUSE path escapes its overlay: {relative}")
        hashes[relative] = sha256_file(resolved)
    for relative in ("lib/riscv64-linux-gnu/libfuse3.so.3", "bin/fusermount3"):
        info = preflight.inspect_elf((root / relative).resolve(strict=True))
        if not info.is_riscv64_little_endian:
            raise FusePreparationError(f"prepared FUSE artifact is not RISC-V: {relative}")
    return hashes


def normalize_overlay_symlinks(root: Path) -> None:
    """Make the package's guest-root absolute development link relocatable."""

    link = root / "usr/lib/riscv64-linux-gnu/libfuse3.so"
    if not link.is_symlink() or os.readlink(link) != "/lib/riscv64-linux-gnu/libfuse3.so.3":
        raise FusePreparationError("libfuse3 development symlink differs from the lock contract")
    link.unlink()
    link.symlink_to("../../../lib/riscv64-linux-gnu/libfuse3.so.3")


def prepare(lock: Mapping[str, Any], output_root: Path, dpkg_deb: Path, allow_download: bool) -> Path:
    output = require_output_root(output_root)
    dpkg = dpkg_deb.resolve(strict=True)
    if not dpkg.is_file() or not os.access(dpkg, os.X_OK):
        raise FusePreparationError("dpkg-deb is not executable")
    overlay_id = f"overlay-{lock['version']}-{sha256_json(lock['packages'])[:12]}"
    final = output / overlay_id
    manifest = final / "fuse-overlay-manifest.json"
    if final.exists():
        if not manifest.is_file():
            raise FusePreparationError("existing FUSE overlay is incomplete")
        validate_overlay(final / "root")
        return manifest

    archives: list[tuple[Mapping[str, Any], Path]] = []
    for record in lock["packages"]:
        archives.append((record, download(record, output / "downloads", allow_download)))
    output.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{overlay_id}-", dir=output))
    root = temporary / "root"
    root.mkdir()
    try:
        for record, archive in archives:
            metadata = package_metadata(dpkg, archive)
            expected = (record["package"], record["version"], record["architecture"])
            if metadata != expected:
                raise FusePreparationError(f"FUSE package metadata differs from lock: {record['package']}")
            subprocess.run([str(dpkg), "--extract", str(archive), str(root)], check=True)
        normalize_overlay_symlinks(root)
        hashes = validate_overlay(root)
        final_root = final / "root"
        record = {
            "schema": MANIFEST_SCHEMA,
            "version": lock["version"],
            "root": str(final_root),
            "include": str(final_root / "usr/include/fuse3"),
            "library": str(final_root / "usr/lib/riscv64-linux-gnu/libfuse3.so"),
            "fusermount3": str(final_root / "bin/fusermount3"),
            "files": hashes,
            "packages": list(lock["packages"]),
            "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        (temporary / "fuse-overlay-manifest.json").write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.rename(final)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--dpkg-deb", type=Path, default=Path("/usr/bin/dpkg-deb"))
    parser.add_argument("--allow-download", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        manifest = prepare(
            load_lock(arguments.lock), arguments.output_root, arguments.dpkg_deb, arguments.allow_download
        )
    except (FusePreparationError, OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"FUSE RISC-V preparation failed: {error}", file=sys.stderr)
        return 2
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
