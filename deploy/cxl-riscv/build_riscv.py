#!/usr/bin/env python3
"""Build the platform probes or complete 3FS applications from frozen inputs."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence

import full_deps
import preflight
import prepare_compiler_rt

PROBES = ("cxl_dax_smoke", "fuse_mount_smoke", "fdb_client_smoke")
APPLICATIONS = ("mgmtd_main", "meta_main", "storage_main", "hf3fs_fuse_main", "admin_cli", "cxl-fabricd")
PROTECTED_OPTIONS = {
    "CMAKE_TOOLCHAIN_FILE", "CMAKE_SYSROOT", "HF3FS_RISCV_SYSROOT", "CMAKE_HOME_DIRECTORY",
    "CMAKE_C_COMPILER", "CMAKE_CXX_COMPILER", "CMAKE_MAKE_PROGRAM", "CMAKE_C_COMPILER_TARGET",
    "CMAKE_CXX_COMPILER_TARGET", "CMAKE_SYSTEM_PROCESSOR", "CMAKE_SYSTEM_NAME",
    "HF3FS_ENABLE_CXL", "HF3FS_ENABLE_RDMA", "ENABLE_FUSE_APPLICATION",
    "CMAKE_TRY_COMPILE_TARGET_TYPE",
}


def build_command(inputs: preflight.Inputs, build_dir: Path, jobs: int, profile: str,
                  closure: Mapping | None = None, cmake_options: Mapping[str, str] | None = None,
                  extra_targets: Sequence[str] = ()) -> list[str]:
    if jobs <= 0:
        raise ValueError("jobs must be positive")
    if not build_dir.is_absolute():
        raise ValueError("build directory must be absolute")
    if profile not in {"platform-smoke", "full-3fs"}:
        raise ValueError("unknown build profile")
    if profile == "platform-smoke" and (cmake_options or extra_targets):
        raise ValueError("extra CMake options and targets require full-3fs")
    if profile == "full-3fs" and (not closure or closure.get("status") != "passed" or
                                  closure.get("schema") != "hf3fs.cxl-source-closure.v1"):
        raise ValueError("full-3fs requires passing phase-1 source closure")
    for target in extra_targets:
        if not re.fullmatch(r"[A-Za-z0-9_.+-]+", target):
            raise ValueError("invalid extra target")
    for name in cmake_options or {}:
        if not re.fullmatch(r"[A-Z0-9_]+", name) or name in PROTECTED_OPTIONS:
            raise ValueError(f"invalid or protected CMake option: {name}")
    source = inputs.project_root if profile == "full-3fs" else inputs.project_root / "deploy/cxl-riscv/smoke"
    command = [str(inputs.cmake), "-S", str(source), "-B", str(build_dir), "-G", "Ninja",
               f"-DCMAKE_MAKE_PROGRAM={inputs.ninja}",
               f"-DCMAKE_TOOLCHAIN_FILE={inputs.project_root}/deploy/cxl-riscv/toolchain-riscv64.cmake",
               f"-DHF3FS_RISCV_SYSROOT={inputs.sysroot}", "-DCMAKE_BUILD_TYPE=Release"]
    if profile == "platform-smoke":
        command += [f"-DHF3FS_RISCV_FDB_INCLUDE={inputs.fdb_include}",
                    f"-DHF3FS_RISCV_FDB_CLIENT={inputs.fdb_client}",
                    f"-DHF3FS_RISCV_FUSE_INCLUDE={inputs.fuse_include}",
                    f"-DHF3FS_RISCV_FUSE_LIBRARY={inputs.fuse_library}"]
    else:
        command += ["-DHF3FS_ENABLE_CXL=ON", "-DHF3FS_ENABLE_RDMA=OFF", "-DENABLE_FUSE_APPLICATION=ON",
                    "-DCMAKE_TRY_COMPILE_TARGET_TYPE=EXECUTABLE",
                    "-DSHUFFLE_METHOD=stdshuffle", "-DFOLLY_BOOST_LINK_STATIC=ON", "-DPORTABLE=ON",
                    f"-DHF3FS_FDB_ROOT={inputs.sysroot}/usr", f"-DHF3FS_FUSE3_ROOT={inputs.sysroot}/usr"]
        # Runtime-only optional Folly probes cannot run during cross configure.
        # Disable these fast paths until the guest establishes their support.
        for feature in ("FOLLY_HAVE_UNALIGNED_ACCESS", "FOLLY_HAVE_WEAK_SYMBOLS", "FOLLY_HAVE_LINUX_VDSO",
                        "FOLLY_HAVE_WCHAR_SUPPORT", "HAVE_VSNPRINTF_ERRORS"):
            command.append(f"-D{feature}=0")
        command += [f"-D{name}={value}" for name, value in sorted((cmake_options or {}).items())]
    return command


def compile_command(inputs: preflight.Inputs, build_dir: Path, jobs: int, profile: str,
                    extra_targets: Sequence[str] = ()) -> list[str]:
    targets = PROBES if profile == "platform-smoke" else (*APPLICATIONS, *PROBES)
    return [str(inputs.cmake), "--build", str(build_dir), "--parallel", str(jobs), "--target",
            *dict.fromkeys((*targets, *extra_targets))]


def load_inputs(path: Path) -> preflight.Inputs:
    report = json.loads(path.read_text())
    if report.get("status") != "passed" or report.get("schema_version") != preflight.SCHEMA_VERSION:
        raise ValueError("preflight did not pass")
    values = {}
    for field in dataclasses.fields(preflight.Inputs):
        record = report["resolved"][field.name]
        actual = Path(record["path"]).resolve(strict=True)
        if record.get("sha256") and full_deps.sha256(actual) != record["sha256"]:
            raise ValueError(f"preflight artifact changed: {field.name}")
        values[field.name] = actual
    return preflight.Inputs(**values)


def execute(args: argparse.Namespace) -> Path:
    inputs = load_inputs(args.preflight)
    build = args.build_dir.resolve()
    if not build.is_relative_to(inputs.project_root / "build"):
        raise ValueError("build output must stay inside components/3FS/build")
    options = {}
    for option in args.cmake_option:
        name, separator, value = option.partition("=")
        if not separator or name in options:
            raise ValueError("CMake options must be unique NAME=VALUE entries")
        options[name] = value
    closure = json.loads(args.source_closure.read_text()) if args.source_closure else None
    dependency_evidence = {}
    environment = dict(os.environ, HF3FS_RISCV_CC=str(inputs.c_compiler),
                       HF3FS_RISCV_CXX=str(inputs.cxx_compiler), HF3FS_RISCV_SYSROOT=str(inputs.sysroot))
    if args.profile == "full-3fs":
        if not closure or closure.get("source") != full_deps.source_identity(inputs.project_root):
            raise ValueError("source closure does not match current source, including nested modifications")
        if not args.dependency_contract or not args.rust_toolchain or not args.cargo_home:
            raise ValueError("full-3fs requires dependency contract, private Rust toolchain and Cargo home")
        contract = full_deps.load_contract(args.dependency_contract)
        dependency_evidence = {"contract_sha256": full_deps.sha256(args.dependency_contract),
                               "libraries": full_deps.validate_sysroot(inputs.sysroot, contract)}
        dependency_evidence["compiler_rt"] = prepare_compiler_rt.verify()
        options["HF3FS_RISCV_COMPILER_RT"] = dependency_evidence["compiler_rt"]["path"]
        arrow_source = options.get("HF3FS_ARROW_SOURCE_DIR")
        if not arrow_source:
            raise ValueError("full-3fs requires a patched, pre-provisioned Arrow source tree")
        dependency_evidence["arrow"] = full_deps.validate_arrow(Path(arrow_source))
        dependency_evidence["jemalloc_configure_sha256"] = full_deps.sha256(
            inputs.project_root / "third_party/jemalloc/configure")
        rust = args.rust_toolchain.resolve(strict=True)
        cargo_home = args.cargo_home.resolve(strict=True)
        for directory in (rust, cargo_home):
            if not directory.is_relative_to(inputs.project_root / "out/cxl-riscv"):
                raise ValueError("Rust and Cargo directories must remain private to 3FS")
        target = contract["rust_target"]
        if not (rust / "lib/rustlib" / target / "lib").is_dir():
            raise ValueError("private Rust toolchain has no GNU RISC-V target standard library")
        environment.update(CARGO_HOME=str(cargo_home), RUSTC=str(rust / "bin/rustc"),
                           PATH=str(rust / "bin") + os.pathsep + environment.get("PATH", ""),
                           CARGO_BUILD_JOBS=str(min(args.jobs, 8)), CARGO_TARGET_DIR=str(build / "cargo-target"),
                           RUSTDOC=str(rust / "bin/rustdoc"),
                           CARGO_TARGET_RISCV64GC_UNKNOWN_LINUX_GNU_LINKER=str(inputs.c_compiler),
                           CC_riscv64gc_unknown_linux_gnu=str(inputs.c_compiler),
                           CXX_riscv64gc_unknown_linux_gnu=str(inputs.cxx_compiler),
                           AR_riscv64gc_unknown_linux_gnu="/usr/bin/llvm-ar",
                           BINDGEN_EXTRA_CLANG_ARGS_riscv64gc_unknown_linux_gnu=
                           f"--target=riscv64-linux-gnu --sysroot={inputs.sysroot}")
        options["HF3FS_CARGO_EXECUTABLE"] = str(rust / "bin/cargo")
        dependency_evidence["rustc_version"] = subprocess.check_output(
            [str(rust / "bin/rustc"), "--version"], text=True).strip()
        dependency_evidence["cargo_lock_sha256"] = full_deps.sha256(inputs.project_root / "Cargo.lock")
        metadata = subprocess.check_output(
            [str(rust / "bin/cargo"), "metadata", "--locked", "--offline", "--format-version", "1",
             "--filter-platform", target], cwd=inputs.project_root, env=environment)
        dependency_evidence["cargo_metadata_sha256"] = hashlib.sha256(metadata).hexdigest()
    configure = build_command(inputs, build, args.jobs, args.profile, closure, options, args.extra_target)
    compile = compile_command(inputs, build, args.jobs, args.profile, args.extra_target)
    build.mkdir(parents=True, exist_ok=True)
    (build / "tmp").mkdir(exist_ok=True)
    environment["TMPDIR"] = str(build / "tmp")
    manifest = {"schema": "hf3fs.riscv-build.v1", "status": "configuring",
                "profile": args.profile, "preflight_sha256": full_deps.sha256(args.preflight),
                "source_closure_sha256": full_deps.sha256(args.source_closure) if args.source_closure else None,
                "configure_argv": configure, "build_argv": compile, "dependencies": dependency_evidence,
                "compiler_version": subprocess.check_output([str(inputs.cxx_compiler), "--version"], text=True)}
    (build / "build-inputs.json").write_text(json.dumps(manifest, indent=2) + "\n")
    subprocess.run(configure, env=environment, check=True)
    manifest["status"] = "configured" if args.configure_only else "building"
    (build / "build-inputs.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if args.configure_only:
        return build / "build-inputs.json"
    subprocess.run(compile, env=environment, check=True)
    targets = PROBES if args.profile == "platform-smoke" else (*APPLICATIONS, *PROBES)
    artifacts = []
    for name in dict.fromkeys((*targets, *args.extra_target)):
        path = build / "bin" / name
        info = preflight.inspect_elf(path)
        if not info.is_riscv64_little_endian or any("ibverbs" in lib or "rdmacm" in lib for lib in info.needed):
            raise ValueError(f"invalid CXL-only RISC-V executable: {name}")
        artifacts.append({"name": name, "path": str(path), "sha256": full_deps.sha256(path),
                          "needed": list(info.needed), "machine": info.machine})
    runtime_artifacts = []
    if args.profile == "full-3fs":
        path = build / "third_party/jemalloc/lib/libjemalloc.so.2"
        info = preflight.inspect_elf(path)
        if not info.is_riscv64_little_endian:
            raise ValueError("built jemalloc runtime is not RISC-V")
        runtime_artifacts.append({"name": path.name, "path": str(path), "sha256": full_deps.sha256(path),
                                  "needed": list(info.needed), "machine": info.machine})
    if closure and closure["source"] != full_deps.source_identity(inputs.project_root):
        raise ValueError("source changed during the build")
    manifest.update(status="passed", artifacts=artifacts, runtime_artifacts=runtime_artifacts)
    destination = build / "build-manifest.json"
    destination.write_text(json.dumps(manifest, indent=2) + "\n")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--profile", choices=("platform-smoke", "full-3fs"), required=True)
    parser.add_argument("--source-closure", type=Path)
    parser.add_argument("--dependency-contract", type=Path)
    parser.add_argument("--rust-toolchain", type=Path)
    parser.add_argument("--cargo-home", type=Path)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--cmake-option", action="append", default=[])
    parser.add_argument("--extra-target", action="append", default=[])
    parser.add_argument("--configure-only", action="store_true")
    try:
        print(execute(parser.parse_args()))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"build-riscv: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
