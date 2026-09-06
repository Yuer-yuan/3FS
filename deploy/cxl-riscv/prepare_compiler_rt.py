#!/usr/bin/env python3
"""Prepare the pinned RISC-V Clang builtins, without changing the sysroot."""

import argparse
import json
from pathlib import Path

import full_deps

CONTRACT = full_deps.DEPLOY / "compiler-rt.json"
LIBRARY = "usr/lib/llvm-18/lib/clang/18/lib/linux/libclang_rt.builtins-riscv64.a"


def location() -> Path:
    record = json.loads(CONTRACT.read_text())
    return full_deps.PROJECT / "out/cxl-riscv/full-deps" / f"compiler-rt-{record['sha256'][:12]}"


def verify() -> dict:
    directory = location()
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest["contract_sha256"] != full_deps.sha256(CONTRACT):
        raise ValueError("compiler runtime contract changed")
    library = full_deps.contained_file(directory / "root", LIBRARY)
    full_deps.check_target_library(library)
    if manifest["sha256"] != full_deps.sha256(library):
        raise ValueError("compiler runtime library changed")
    return {"path": str(library), "sha256": manifest["sha256"],
            "contract_sha256": manifest["contract_sha256"]}


def prepare(allow_download: bool) -> dict:
    directory = location()
    if directory.exists():
        return verify()
    record = json.loads(CONTRACT.read_text())
    errors = full_deps.packages.package_record_errors(record, expected_architectures={"riscv64"})
    if errors:
        raise ValueError(str(errors))
    archive = full_deps.packages._download_package(
        record, directory.parent / "downloads", allow_download=allow_download)
    directory.mkdir()
    log = directory / "prepare.log"
    full_deps.packages._verify_deb_metadata(Path("/usr/bin/dpkg-deb"), archive, record, log=log)
    full_deps.packages._run(["/usr/bin/dpkg-deb", "--extract", str(archive), str(directory / "root")], log=log)
    library = full_deps.contained_file(directory / "root", LIBRARY)
    full_deps.check_target_library(library)
    (directory / "manifest.json").write_text(json.dumps({
        "contract_sha256": full_deps.sha256(CONTRACT), "sha256": full_deps.sha256(library),
        "package": record, "path": str(library),
    }, indent=2) + "\n")
    return verify()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-download", action="store_true")
    print(json.dumps(prepare(parser.parse_args().allow_download), indent=2))
