#!/usr/bin/env python3
"""Assemble and validate the private, hash-locked full 3FS target sysroot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from collections.abc import Mapping

DEPLOY = Path(__file__).resolve().parent
PROJECT = DEPLOY.parents[1]
sys.path.insert(0, str(DEPLOY / "fdb"))
import prepare_fdb_environment as packages
import preflight


def sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def source_identity(root: Path) -> dict:
    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", "-C", str(root), *args])

    untracked = []
    for name in git("ls-files", "--others", "--exclude-standard", "-z").decode().split("\0"):
        if not name:
            continue
        path = root / name
        if path.is_symlink():
            digest = hashlib.sha256(os.readlink(path).encode()).hexdigest()
        elif path.is_file():
            digest = sha256(path)
        else:
            continue
        untracked.append({"path": name, "mode": path.lstat().st_mode & 0o777, "sha256": digest})
    submodules = []
    for line in git("submodule", "status").decode().splitlines():
        fields = line[1:].split()
        if not fields:
            continue
        nested = {"state": line[0], "head": fields[0], "path": fields[1]}
        if line[0] != "-":
            nested["source"] = source_identity(root / fields[1])
        submodules.append(nested)
    return {
        "head": git("rev-parse", "HEAD").decode().strip(),
        "tracked_diff_sha256": hashlib.sha256(git("diff", "--binary", "--no-ext-diff", "HEAD")).hexdigest(),
        "untracked": sorted(untracked, key=lambda item: item["path"]),
        "tombstones": sorted(filter(None, git("diff", "--name-only", "--diff-filter=D", "HEAD", "-z").decode().split("\0"))),
        "submodules": submodules,
    }


def contained_file(root: Path, relative: str) -> Path:
    declared = Path(relative)
    if declared.is_absolute() or ".." in declared.parts:
        raise ValueError(f"target path must be relative: {relative}")
    resolved = (root / declared).resolve(strict=True)
    if not resolved.is_relative_to(root.resolve(strict=True)) or not resolved.is_file():
        raise ValueError(f"target library escapes sysroot: {relative}")
    return resolved


def check_target_library(path: Path) -> None:
    with path.open("rb") as source:
        magic = source.read(8)
    if magic == b"!<arch>\n":
        result = subprocess.run(
            ["readelf", "-h", str(path)], check=True, capture_output=True, text=True,
            env={**os.environ, "LC_ALL": "C"},
        )
        machines = re.findall(r"^\s*Machine:\s*(.+)$", result.stdout, re.MULTILINE)
        if not machines or any(machine.strip() != "RISC-V" for machine in machines):
            raise ValueError(f"archive has non-RISC-V or missing objects: {path}")
    elif magic.startswith(b"\x7fELF"):
        if not preflight.inspect_elf(path).is_riscv64_little_endian:
            raise ValueError(f"target library is not RISC-V: {path}")
    else:
        raise ValueError(f"target library is not an ELF or self-contained archive: {path}")


def load_contract(path: Path) -> dict:
    contract = json.loads(path.read_text())
    if contract.get("schema") != "hf3fs.full-deps.v1" or contract.get("target") != "riscv64-linux-gnu":
        raise ValueError("unsupported full dependency contract")
    if not contract.get("required_libraries") or not contract.get("packages"):
        raise ValueError("empty full dependency contract")
    seen = set()
    for record in contract["packages"]:
        errors = packages.package_record_errors(record, expected_architectures={"riscv64", "all"})
        if errors or record["package"] in seen:
            raise ValueError(f"invalid locked package: {record.get('package')}: {errors}")
        seen.add(record["package"])
    return contract


def validate_sysroot(root: Path, contract: Mapping) -> dict:
    libraries = {}
    for relative in contract["required_libraries"]:
        path = contained_file(root, relative)
        check_target_library(path)
        libraries[relative] = {"resolved_path": str(path), "sha256": sha256(path)}
    return libraries


def normalize_links(root: Path) -> None:
    for path in root.rglob("*"):
        if not path.is_symlink():
            continue
        target = os.readlink(path)
        if target.startswith("/"):
            destination = root / target.lstrip("/")
            # Preserve guest-root meaning without resolving against the host.
            path.unlink()
            path.symlink_to(os.path.relpath(destination, path.parent))


def prepare_compilers(sysroot: Path, output: Path, cc: Path, cxx: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    result = {}
    for name, compiler in (("riscv64-clang", cc), ("riscv64-clang++", cxx)):
        entry = compiler.absolute()
        compiler = compiler.resolve(strict=True)
        # Clang chooses its C++ driver from argv[0], even when clang++ is a
        # symlink to clang. Resolve the executable for hashing, not its name.
        arguments = [str(entry), "--target=riscv64-linux-gnu", f"--sysroot={sysroot}",
                     f"--gcc-install-dir={sysroot}/usr/lib/gcc-cross/riscv64-linux-gnu/13"]
        path = output / name
        # Linker selection is invalid for compile-only -Werror invocations.
        program = ("#!/usr/bin/python3\nimport os, sys\nargs = sys.argv[1:]\n"
                   "if not any(a in args for a in ('-c', '-E', '-S', '-fsyntax-only', '-M', '-MM')):\n"
                   "    args = ['-fuse-ld=lld', *args]\n"
                   f"os.execv({str(compiler)!r}, {arguments!r} + args)\n")
        path.write_text(program)
        path.chmod(0o755)
        result[name] = {"path": str(path), "sha256": sha256(path), "compiler": str(compiler),
                        "compiler_sha256": sha256(compiler)}
    return result


def validate_arrow(source: Path) -> dict:
    source = source.resolve(strict=True)
    if not source.is_relative_to(PROJECT.resolve()):
        raise ValueError("Arrow source escapes the 3FS checkout")
    head = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if head != "b7d2f7ffca66c868bd2fce5b3749c6caa002a7f0":
        raise ValueError("Arrow source commit differs from the contract")
    diff = subprocess.check_output(["git", "-C", str(source), "diff", "--binary", "--no-ext-diff", "--no-color", "HEAD"])
    patch = DEPLOY / "arrow-cross.patch"
    if diff != patch.read_bytes():
        raise ValueError("Arrow source changes differ from the recorded cross patch")
    archives = {}
    for line in (source / "cpp/thirdparty/export.sh").read_text().splitlines():
        match = re.fullmatch(r"export (ARROW_[A-Z0-9_]+_URL)=(.+)", line)
        if not match:
            continue
        variable, declared = match.groups()
        path = Path(declared).resolve(strict=True)
        if not path.is_file() or not path.is_relative_to(PROJECT.resolve()):
            raise ValueError(f"Arrow archive is not a private pre-provisioned file: {variable}")
        archives[variable] = {"path": str(path), "sha256": sha256(path)}
    if not archives:
        raise ValueError("empty offline Arrow archive manifest")
    return {"source": str(source), "head": head, "patch_sha256": sha256(patch), "archives": archives}


def prepare_arrow(source: Path, output: Path) -> Path:
    destination = output / "arrow-source"
    if not destination.exists():
        subprocess.run(["git", "clone", "--no-hardlinks", "--no-checkout", str(source), str(destination)], check=True)
        subprocess.run(["git", "-C", str(destination), "checkout", "--detach",
                        "b7d2f7ffca66c868bd2fce5b3749c6caa002a7f0"], check=True)
        (destination / "cpp/thirdparty/export.sh").write_bytes((source / "cpp/thirdparty/export.sh").read_bytes())
        subprocess.run(["git", "-C", str(destination), "apply", str(DEPLOY / "arrow-cross.patch")], check=True)
    evidence = validate_arrow(destination)
    manifest = output / "arrow-manifest.json"
    manifest.write_text(json.dumps(evidence, indent=2) + "\n")
    return manifest


def prepare(args: argparse.Namespace) -> Path:
    contract = load_contract(args.contract)
    output = args.output.resolve()
    if not output.is_relative_to(PROJECT.resolve() / "out/cxl-riscv/full-deps"):
        raise ValueError("output escapes components/3FS/out/cxl-riscv/full-deps")
    output.mkdir(parents=True, exist_ok=True)
    sources = [args.base_sysroot.resolve(strict=True), args.fdb_sysroot.resolve(strict=True),
               args.fuse_root.resolve(strict=True)]
    for source in sources:
        if not source.is_dir() or not source.is_relative_to(PROJECT.resolve() / "out/cxl-riscv"):
            raise ValueError(f"input must be an existing private G0 dependency tree: {source}")
    identity = {"contract_sha256": sha256(args.contract), "inputs": [str(p) for p in sources]}
    identifier = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
    destination = output / f"sysroot-{identifier}"
    manifest = destination / "full-deps-manifest.json"
    if destination.exists():
        if not manifest.exists():
            raise ValueError(f"preserved incomplete dependency preparation: {destination}")
        previous = json.loads(manifest.read_text())
        if validate_sysroot(destination / "root", contract) != previous["libraries"]:
            raise ValueError("prepared sysroot differs from its manifest")
        return manifest
    archives = [(r, packages._download_package(r, output / "downloads", allow_download=args.allow_download))
                for r in contract["packages"]]
    destination.mkdir()
    root = destination / "root"
    log = destination / "prepare.log"
    with log.open("w") as stream:
        subprocess.run(["cp", "-a", "--reflink=auto", str(sources[0]), str(root)],
                       check=True, stdout=stream, stderr=subprocess.STDOUT)
        for source in sources[1:]:
            subprocess.run(["cp", "-a", "--reflink=auto", str(source) + "/.", str(root)],
                           check=True, stdout=stream, stderr=subprocess.STDOUT)
    for record, archive in archives:
        packages._verify_deb_metadata(Path("/usr/bin/dpkg-deb"), archive, record, log=log)
        packages._run(["/usr/bin/dpkg-deb", "--extract", str(archive), str(root)], log=log)
    normalize_links(root)
    libraries = validate_sysroot(root, contract)
    record = {"schema": "hf3fs.full-deps-manifest.v1", "sysroot": str(root),
              **identity, "libraries": libraries, "packages": contract["packages"]}
    manifest.write_text(json.dumps(record, indent=2) + "\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=DEPLOY / "full-deps.json")
    parser.add_argument("--output", type=Path, default=PROJECT / "out/cxl-riscv/full-deps")
    parser.add_argument("--base-sysroot", type=Path, required=True)
    parser.add_argument("--fdb-sysroot", type=Path, required=True)
    parser.add_argument("--fuse-root", type=Path, required=True)
    parser.add_argument("--allow-download", action="store_true")
    parser.add_argument("--clang-cc", type=Path, default=Path("/usr/bin/clang"))
    parser.add_argument("--clang-cxx", type=Path, default=Path("/usr/bin/clang++"))
    parser.add_argument("--arrow-source", type=Path)
    args = parser.parse_args()
    try:
        manifest = prepare(args)
        record = json.loads(manifest.read_text())
        compilers = prepare_compilers(Path(record["sysroot"]), args.output.resolve() / "bin",
                                      args.clang_cc, args.clang_cxx)
        (args.output / "compiler-manifest.json").write_text(json.dumps(compilers, indent=2) + "\n")
        if args.arrow_source:
            prepare_arrow(args.arrow_source.resolve(strict=True), args.output.resolve())
        print(manifest)
    except (OSError, ValueError, packages.PreparationError, subprocess.CalledProcessError) as error:
        print(f"full-deps: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
