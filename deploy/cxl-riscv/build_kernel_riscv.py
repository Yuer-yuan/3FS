#!/usr/bin/env python3
"""Build a 3FS-owned RISC-V guest kernel from an unchanged Linux checkout."""

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
import sys
from collections.abc import Mapping, Sequence


DEPLOY_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DEPLOY_DIR.parents[1]
DEFAULT_FRAGMENT = DEPLOY_DIR / "kernel.fragment"
CONFIG_ASSIGNMENT = re.compile(r"^(CONFIG_[A-Z0-9_]+)=.*$")
CONFIG_DISABLED = re.compile(r"^# (CONFIG_[A-Z0-9_]+) is not set$")
MANIFEST_SCHEMA = "hf3fs.cxl-riscv-kernel.v1"

if str(DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(DEPLOY_DIR))
import guest_image


class KernelBuildError(RuntimeError):
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


def require_build_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise KernelBuildError("kernel build directory must be absolute")
    project = PROJECT_ROOT.resolve(strict=True)
    build = path.resolve(strict=False)
    approved = project / "out/cxl-riscv/kernel/builds"
    if not is_within(build, approved) or build == approved:
        raise KernelBuildError("kernel build directory escapes components/3FS/out")
    if build.exists():
        raise KernelBuildError("kernel build directory already exists")
    return build


def config_key(line: str) -> str | None:
    assignment = CONFIG_ASSIGNMENT.fullmatch(line)
    disabled = CONFIG_DISABLED.fullmatch(line)
    if assignment is not None:
        return assignment.group(1)
    if disabled is not None:
        return disabled.group(1)
    return None


def merge_config(base_text: str, fragment_text: str) -> str:
    replacements: dict[str, str] = {}
    for line in fragment_text.splitlines():
        key = config_key(line)
        if key is not None:
            if key in replacements:
                raise KernelBuildError(f"duplicate kernel fragment key: {key}")
            replacements[key] = line
    if not replacements:
        raise KernelBuildError("kernel fragment contains no options")
    result: list[str] = []
    seen: set[str] = set()
    for line in base_text.splitlines():
        key = config_key(line)
        if key in replacements:
            if key not in seen:
                result.append(replacements[key])
                seen.add(key)
            continue
        result.append(line)
    for key, line in replacements.items():
        if key not in seen:
            result.append(line)
    return "\n".join(result) + "\n"


def build_commands(
    *, make: Path, source: Path, build: Path, cross_prefix: Path, jobs: int
) -> tuple[list[str], list[str]]:
    common = [
        str(make),
        "-C",
        str(source),
        f"O={build}",
        "ARCH=riscv",
        f"CROSS_COMPILE={cross_prefix}",
    ]
    return common + ["olddefconfig"], common + [f"-j{jobs}", "Image"]


def run(command: Sequence[str], log: Path, environment: Mapping[str, str]) -> None:
    with log.open("a", encoding="utf-8") as output:
        output.write("COMMAND " + json.dumps(list(command)) + "\n")
        output.flush()
        completed = subprocess.run(
            list(command),
            check=False,
            stdout=output,
            stderr=subprocess.STDOUT,
            env=dict(environment),
        )
        output.write(f"EXIT {completed.returncode}\n")
    if completed.returncode != 0:
        raise KernelBuildError(
            f"kernel command failed with exit {completed.returncode}: {' '.join(command)}"
        )


def tool_environment(cross_prefix: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["LANG"] = "C"
    environment["LC_ALL"] = "C"
    environment.pop("LD_LIBRARY_PATH", None)
    toolchain_root = cross_prefix.parent.parent.parent
    host_libraries = toolchain_root / "usr/lib/x86_64-linux-gnu"
    if host_libraries.is_dir():
        environment["LD_LIBRARY_PATH"] = str(host_libraries.resolve(strict=True))
    return environment


def build_kernel(
    *,
    linux_source: Path,
    base_config: Path,
    fragment: Path,
    build_directory: Path,
    cross_prefix: Path,
    make: Path,
    jobs: int,
    print_only: bool = False,
) -> Path | dict[str, list[str]]:
    if jobs <= 0:
        raise KernelBuildError("kernel jobs must be positive")
    source = linux_source.resolve(strict=True)
    base = base_config.resolve(strict=True)
    fragment_path = fragment.resolve(strict=True)
    make_path = make.resolve(strict=True)
    if not (source / "Makefile").is_file():
        raise KernelBuildError("Linux source Makefile is absent")
    if not base.is_file() or not fragment_path.is_file():
        raise KernelBuildError("kernel base config or fragment is absent")
    if not make_path.is_file() or not os.access(make_path, os.X_OK):
        raise KernelBuildError("make is not executable")
    prefix = Path(os.path.normpath(cross_prefix))
    for suffix in ("gcc", "ld", "objcopy"):
        tool = Path(str(prefix) + suffix)
        if not tool.is_file() or not os.access(tool, os.X_OK):
            raise KernelBuildError(f"kernel cross tool is absent: {tool}")
    build = require_build_directory(build_directory)
    olddefconfig, image_command = build_commands(
        make=make_path, source=source, build=build, cross_prefix=prefix, jobs=jobs
    )
    if print_only:
        return {"olddefconfig": olddefconfig, "image": image_command}

    build.mkdir(parents=True)
    log = build / "kernel-build.log"
    config = build / ".config"
    config.write_text(
        merge_config(
            base.read_text(encoding="utf-8"),
            fragment_path.read_text(encoding="utf-8"),
        ),
        encoding="utf-8",
    )
    environment = tool_environment(prefix)
    try:
        run(olddefconfig, log, environment)
        config_errors = guest_image.validate_kernel_config(config, fragment_path)
        if config_errors:
            raise KernelBuildError("kernel configuration rejected: " + "; ".join(config_errors))
        run(image_command, log, environment)
        image = build / "arch/riscv/boot/Image"
        if not image.is_file():
            raise KernelBuildError("kernel Image was not produced")
        manifest = build / "kernel-manifest.json"
        record = {
            "schema": MANIFEST_SCHEMA,
            "linux_source": str(source),
            "linux_makefile_sha256": sha256_file(source / "Makefile"),
            "base_config": str(base),
            "base_config_sha256": sha256_file(base),
            "fragment": str(fragment_path),
            "fragment_sha256": sha256_file(fragment_path),
            "config": str(config),
            "config_sha256": sha256_file(config),
            "image": str(image),
            "image_sha256": sha256_file(image),
            "cross_prefix": str(prefix),
            "commands": [olddefconfig, image_command],
            "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        manifest.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return manifest
    except Exception as error:
        failure = build / "kernel-failure.json"
        failure.write_text(
            json.dumps(
                {
                    "schema": MANIFEST_SCHEMA,
                    "status": "failed",
                    "error": str(error),
                    "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--linux-source", required=True, type=Path)
    parser.add_argument("--base-config", required=True, type=Path)
    parser.add_argument("--fragment", type=Path, default=DEFAULT_FRAGMENT)
    parser.add_argument("--build-dir", required=True, type=Path)
    parser.add_argument("--cross-prefix", required=True, type=Path)
    parser.add_argument("--make", type=Path, default=Path("/usr/bin/make"))
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--print-only", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        result = build_kernel(
            linux_source=arguments.linux_source,
            base_config=arguments.base_config,
            fragment=arguments.fragment,
            build_directory=arguments.build_dir,
            cross_prefix=arguments.cross_prefix,
            make=arguments.make,
            jobs=arguments.jobs,
            print_only=arguments.print_only,
        )
    except (KernelBuildError, OSError, subprocess.SubprocessError, UnicodeError) as error:
        print(f"RISC-V kernel build failed: {error}", file=sys.stderr)
        return 2
    if isinstance(result, dict):
        print(json.dumps(result, indent=2))
    else:
        print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
