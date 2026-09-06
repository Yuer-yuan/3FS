#!/usr/bin/env python3
"""Prepare hash-locked target disk inspection tools in a private overlay."""

import argparse
import json
from pathlib import Path

import full_deps
import stage_guest_rootfs as stage

CONTRACT = full_deps.DEPLOY / "guest-tools.json"


def load_contract() -> dict:
    record = json.loads(CONTRACT.read_text())
    if record.get("schema") != "hf3fs.guest-tools.v1" or record.get("target") != "riscv64-linux-gnu":
        raise ValueError("invalid guest tool contract")
    for package in record["packages"]:
        errors = full_deps.packages.package_record_errors(package, expected_architectures={"riscv64"})
        if errors:
            raise ValueError(str(errors))
    return record


def location() -> Path:
    return full_deps.PROJECT / "out/cxl-riscv/full-deps" / f"guest-tools-{full_deps.sha256(CONTRACT)[:12]}"


def elf_files(root: Path) -> dict:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as source:
            if source.read(4) != b"\x7fELF":
                continue
        stage.inspect_required_elf(path, path.name)
        result[str(path.relative_to(root))] = full_deps.sha256(path)
    return result


def verify() -> dict:
    contract = load_contract()
    root = location() / "root"
    manifest = json.loads((location() / "manifest.json").read_text())
    if manifest.get("contract_sha256") != full_deps.sha256(CONTRACT) or manifest.get("files") != elf_files(root):
        raise ValueError("guest tool overlay differs from its manifest")
    for binary in contract["binaries"]:
        stage.inspect_required_elf(full_deps.contained_file(root, binary), binary)
    return {**manifest, "root": str(root), "binaries": contract["binaries"]}


def prepare(allow_download: bool) -> dict:
    contract = load_contract()
    directory = location()
    if directory.exists():
        return verify()
    archives = [(package, full_deps.packages._download_package(
        package, directory.parent / "downloads", allow_download=allow_download)) for package in contract["packages"]]
    directory.mkdir()
    root = directory / "root"
    log = directory / "prepare.log"
    for package, archive in archives:
        full_deps.packages._verify_deb_metadata(Path("/usr/bin/dpkg-deb"), archive, package, log=log)
        full_deps.packages._run(["/usr/bin/dpkg-deb", "--extract", str(archive), str(root)], log=log)
    full_deps.normalize_links(root)
    (directory / "manifest.json").write_text(json.dumps({
        "contract_sha256": full_deps.sha256(CONTRACT), "packages": contract["packages"], "files": elf_files(root),
    }, indent=2) + "\n")
    return verify()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-download", action="store_true")
    print(json.dumps(prepare(parser.parse_args().allow_download), indent=2))
