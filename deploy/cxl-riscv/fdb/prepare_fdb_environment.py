#!/usr/bin/env python3
"""Prepare an isolated host/target dependency environment for the FDB port."""

from __future__ import annotations

import argparse
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
import urllib.parse
import urllib.request


FDB_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = FDB_DIR.parents[2]
DEFAULT_LOCK = FDB_DIR / "fdb-riscv.lock.json"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "out" / "cxl-riscv" / "fdb" / "deps"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
APPROVED_PACKAGE_HOSTS = {
    "archive.ubuntu.com",
    "ports.ubuntu.com",
}
BASE_IDENTITY_FILES = (
    "usr/bin/riscv64-linux-gnu-gcc-13",
    "usr/bin/riscv64-linux-gnu-g++-13",
    "usr/riscv64-linux-gnu/lib/ld-linux-riscv64-lp64d.so.1",
    "usr/riscv64-linux-gnu/lib/libc.so.6",
    "usr/riscv64-linux-gnu/lib/libstdc++.so.6.0.33",
)
TARGET_REQUIREMENTS = (
    "usr/include/zlib.h",
    "usr/lib/riscv64-linux-gnu/libz.a",
    "usr/include/openssl/ssl.h",
    "usr/lib/riscv64-linux-gnu/libssl.a",
    "usr/lib/riscv64-linux-gnu/libcrypto.a",
)
TARGET_RUNTIME_ALIASES = {
    "lib/ld-linux-riscv64-lp64d.so.1": (
        "usr/riscv64-linux-gnu/lib/ld-linux-riscv64-lp64d.so.1"
    ),
}


class PreparationError(RuntimeError):
    pass


def _is_within(candidate: Path, directory: Path) -> bool:
    try:
        candidate.relative_to(directory)
    except ValueError:
        return False
    return True


def require_output_root(
    output_root: Path,
    *,
    project_root: Path = PROJECT_ROOT,
    resolve_project: bool = True,
) -> Path:
    if not output_root.is_absolute():
        raise PreparationError("dependency output root must be absolute")
    project = (
        project_root.resolve(strict=True)
        if resolve_project
        else Path(os.path.normpath(project_root))
    )
    output = Path(os.path.normpath(output_root.resolve(strict=False)))
    approved = project / "out" / "cxl-riscv" / "fdb" / "deps"
    if not _is_within(output, approved):
        raise PreparationError(
            "dependency output root escapes components/3FS/out/cxl-riscv/fdb/deps"
        )
    return output


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def package_record_errors(
    record: Mapping[str, Any], *, expected_architectures: set[str]
) -> list[str]:
    errors: list[str] = []
    for field in ("package", "version", "architecture", "url", "sha256"):
        if not isinstance(record.get(field), str) or not record[field]:
            errors.append(f"package record {field} is missing")
    if record.get("architecture") not in expected_architectures:
        errors.append("package architecture is not allowed")
    digest = record.get("sha256")
    if not isinstance(digest, str) or not HEX64.fullmatch(digest):
        errors.append("package SHA-256 is malformed")
    size = record.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        errors.append("package size is malformed")
    url = record.get("url")
    if isinstance(url, str):
        parsed = urllib.parse.urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in APPROVED_PACKAGE_HOSTS
            or not parsed.path.endswith(".deb")
            or parsed.query
            or parsed.fragment
        ):
            errors.append("package URL is not an approved Ubuntu archive URL")
    return list(dict.fromkeys(errors))


def load_lock(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PreparationError(f"cannot read dependency lock: {error}") from error
    if not isinstance(value, dict):
        raise PreparationError("dependency lock must be a JSON object")
    dependencies = value.get("dependencies")
    if not isinstance(dependencies, dict):
        raise PreparationError("dependency lock has no dependencies object")
    groups = (
        ("target_packages", {"riscv64"}),
        ("host_packages", {"amd64", "all"}),
    )
    for name, architectures in groups:
        records = dependencies.get(name)
        if not isinstance(records, list) or not records:
            raise PreparationError(f"dependency lock has no {name}")
        seen: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                raise PreparationError(f"{name} contains a non-object record")
            errors = package_record_errors(
                record, expected_architectures=architectures
            )
            if errors:
                raise PreparationError(
                    f"invalid {name} record: " + "; ".join(errors)
                )
            package = str(record["package"])
            if package in seen:
                raise PreparationError(f"duplicate locked package: {package}")
            seen.add(package)
    return value


def _download_package(
    record: Mapping[str, Any], downloads: Path, *, allow_download: bool
) -> Path:
    expected_hash = str(record["sha256"])
    expected_size = int(record["size"])
    destination = downloads / f"{expected_hash}.deb"
    if destination.exists():
        if (
            not destination.is_file()
            or destination.stat().st_size != expected_size
            or _sha256_file(destination) != expected_hash
        ):
            raise PreparationError(
                f"cached package differs from lock: {record['package']}"
            )
        return destination
    if not allow_download:
        raise PreparationError(
            f"locked package is absent; rerun with --allow-download: {record['package']}"
        )

    downloads.mkdir(parents=True, exist_ok=True)
    temporary = downloads / f".{expected_hash}.part-{os.getpid()}"
    request = urllib.request.Request(
        str(record["url"]), headers={"User-Agent": "hf3fs-cxl-riscv-g0/1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.geturl() != record["url"]:
                raise PreparationError(
                    f"package URL redirected unexpectedly: {record['package']}"
                )
            with temporary.open("xb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
        if temporary.stat().st_size != expected_size:
            raise PreparationError(f"package size differs from lock: {record['package']}")
        if _sha256_file(temporary) != expected_hash:
            raise PreparationError(
                f"package SHA-256 differs from lock: {record['package']}"
            )
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def _run(command: Sequence[str], *, log: Path) -> str:
    completed = subprocess.run(
        list(command),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "LANG": "C", "LC_ALL": "C"},
    )
    with log.open("a", encoding="utf-8") as destination:
        destination.write("COMMAND " + json.dumps(list(command)) + "\n")
        destination.write(completed.stdout)
        if completed.stdout and not completed.stdout.endswith("\n"):
            destination.write("\n")
        destination.write(f"EXIT {completed.returncode}\n")
    if completed.returncode != 0:
        raise PreparationError(
            f"command failed with exit {completed.returncode}: "
            + " ".join(command)
        )
    return completed.stdout.strip()


def _verify_deb_metadata(
    dpkg_deb: Path,
    archive: Path,
    record: Mapping[str, Any],
    *,
    log: Path,
) -> None:
    output = _run(
        [
            str(dpkg_deb),
            "--field",
            str(archive),
            "Package",
            "Version",
            "Architecture",
        ],
        log=log,
    ).splitlines()
    expected = [
        f"Package: {record['package']}",
        f"Version: {record['version']}",
        f"Architecture: {record['architecture']}",
    ]
    if output != expected:
        raise PreparationError(
            f"Debian package metadata differs from lock: {record['package']}"
        )


def _base_identity(base_sysroot: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in BASE_IDENTITY_FILES:
        artifact = base_sysroot / relative
        if not artifact.is_file():
            raise PreparationError(f"base sysroot artifact is absent: {relative}")
        result[relative] = _sha256_file(artifact.resolve(strict=True))
    return result


def ensure_target_runtime_aliases(sysroot: Path) -> dict[str, str]:
    """Install loader-visible relative aliases inside the prepared sysroot."""

    root = sysroot.resolve(strict=True)
    if not root.is_dir():
        raise PreparationError("prepared target sysroot is not a directory")
    for visible, source_relative in TARGET_RUNTIME_ALIASES.items():
        source = root / source_relative
        if not source.is_file():
            raise PreparationError(
                f"target runtime alias source is absent: {source_relative}"
            )
        destination = root / visible
        destination.parent.mkdir(parents=True, exist_ok=True)
        relative_target = os.path.relpath(source, destination.parent)
        if destination.is_symlink():
            try:
                resolved = destination.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise PreparationError(
                    f"target runtime alias is broken: {visible}"
                ) from error
            if os.readlink(destination) != relative_target or resolved != source.resolve(
                strict=True
            ):
                raise PreparationError(
                    f"target runtime alias differs from contract: {visible}"
                )
        elif destination.exists():
            raise PreparationError(
                f"target runtime alias destination is occupied: {visible}"
            )
        else:
            destination.symlink_to(relative_target)
    return dict(TARGET_RUNTIME_ALIASES)


def _record_runtime_aliases(manifest: Path, aliases: Mapping[str, str]) -> None:
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PreparationError(f"cannot read existing environment manifest: {error}") from error
    if not isinstance(value, dict) or value.get("schema") != "hf3fs.fdb-riscv-environment.v1":
        raise PreparationError("existing environment manifest schema is unsupported")
    if value.get("runtime_aliases") == aliases:
        return
    value["runtime_aliases"] = dict(aliases)
    temporary = manifest.with_name(f".{manifest.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(manifest)


def _write_host_wrappers(host_root: Path) -> tuple[Path, Path]:
    wrapper_directory = host_root / "bin"
    wrapper_directory.mkdir(parents=True, exist_ok=True)
    mono = wrapper_directory / "mono"
    mcs = wrapper_directory / "mcs"
    mono.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "launcher_dir=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd)\n"
        "host_root=$(CDPATH= cd -- \"$launcher_dir/..\" && pwd)\n"
        "export MONO_GAC_PREFIX=\"$host_root/usr\"\n"
        "exec \"$host_root/usr/bin/mono-sgen\" "
        "--config \"$host_root/etc/mono/config\" \"$@\"\n",
        encoding="utf-8",
    )
    mcs.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "launcher_dir=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd)\n"
        "host_root=$(CDPATH= cd -- \"$launcher_dir/..\" && pwd)\n"
        "exec \"$launcher_dir/mono\" "
        "\"$host_root/usr/lib/mono/4.5/mcs.exe\" \"$@\"\n",
        encoding="utf-8",
    )
    mono.chmod(0o755)
    mcs.chmod(0o755)
    return mono, mcs


def prepare(
    *,
    lock: Mapping[str, Any],
    base_sysroot: Path,
    output_root: Path,
    dpkg_deb: Path,
    allow_download: bool,
) -> Path:
    base = base_sysroot.resolve(strict=True)
    if not base.is_dir():
        raise PreparationError("base sysroot is not a directory")
    dpkg = dpkg_deb.resolve(strict=True)
    if not dpkg.is_file() or not os.access(dpkg, os.X_OK):
        raise PreparationError("dpkg-deb is not executable")
    dependencies = lock["dependencies"]
    lock_digest = _sha256_json(dependencies)
    base_identity = _base_identity(base)
    environment_id = f"environment-{lock_digest[:12]}"
    final = output_root / environment_id
    if final.exists():
        manifest = final / "fdb-environment-manifest.json"
        if not manifest.is_file():
            raise PreparationError(f"existing environment is incomplete: {final}")
        aliases = ensure_target_runtime_aliases(final / "sysroot")
        _record_runtime_aliases(manifest, aliases)
        return manifest

    output_root.mkdir(parents=True, exist_ok=True)
    downloads = output_root / "downloads"
    records: list[tuple[str, Mapping[str, Any], Path]] = []
    for group in ("target_packages", "host_packages"):
        for record in dependencies[group]:
            archive = _download_package(
                record, downloads, allow_download=allow_download
            )
            records.append((group, record, archive))

    temporary = Path(
        tempfile.mkdtemp(prefix=f".{environment_id}-work-", dir=output_root)
    )
    log = temporary / "prepare.log"
    target_sysroot = temporary / "sysroot"
    host_root = temporary / "host-tools"
    shutil.copytree(base, target_sysroot, symlinks=True)
    host_root.mkdir()
    package_manifest: list[dict[str, Any]] = []
    for group, record, archive in records:
        _verify_deb_metadata(dpkg, archive, record, log=log)
        destination = target_sysroot if group == "target_packages" else host_root
        _run([str(dpkg), "--extract", str(archive), str(destination)], log=log)
        package_manifest.append(
            {
                "group": group,
                "package": record["package"],
                "version": record["version"],
                "architecture": record["architecture"],
                "url": record["url"],
                "sha256": record["sha256"],
                "size": record["size"],
            }
        )

    for relative in TARGET_REQUIREMENTS:
        if not (target_sysroot / relative).is_file():
            raise PreparationError(f"prepared sysroot artifact is absent: {relative}")
    runtime_aliases = ensure_target_runtime_aliases(target_sysroot)
    mono, mcs = _write_host_wrappers(host_root)
    mono_version = _run([str(mono), "--version"], log=log).splitlines()[0]
    mcs_version = _run([str(mcs), "--version"], log=log).splitlines()[0]

    final_sysroot = final / "sysroot"
    final_host_root = final / "host-tools"
    manifest_data = {
        "schema": "hf3fs.fdb-riscv-environment.v1",
        "dependency_lock_sha256": lock_digest,
        "base_sysroot": str(base),
        "base_identity": base_identity,
        "target_sysroot": str(final_sysroot),
        "runtime_aliases": runtime_aliases,
        "host_tools": {
            "mono": str(final_host_root / "bin" / "mono"),
            "mcs": str(final_host_root / "bin" / "mcs"),
            "mono_version": mono_version,
            "mcs_version": mcs_version,
        },
        "packages": package_manifest,
    }
    manifest = temporary / "fdb-environment-manifest.json"
    manifest.write_text(
        json.dumps(manifest_data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.rename(final)
    return final / manifest.name


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--base-sysroot", required=True, type=Path)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--dpkg-deb", required=True, type=Path)
    parser.add_argument("--allow-download", action="store_true")
    parser.add_argument("--print-only", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        lock = load_lock(arguments.lock)
        output_root = require_output_root(arguments.output_root)
        if arguments.print_only:
            print(
                json.dumps(
                    {
                        "base_sysroot": str(arguments.base_sysroot),
                        "output_root": str(output_root),
                        "packages": lock["dependencies"],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        manifest = prepare(
            lock=lock,
            base_sysroot=arguments.base_sysroot,
            output_root=output_root,
            dpkg_deb=arguments.dpkg_deb,
            allow_download=arguments.allow_download,
        )
        print(manifest)
        return 0
    except (PreparationError, OSError, KeyError, TypeError, ValueError) as error:
        print(f"FDB dependency preparation failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
