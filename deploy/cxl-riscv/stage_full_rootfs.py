#!/usr/bin/env python3
"""Add the verified complete 3FS build to a fresh copy of a G0 rootfs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import build_riscv
import full_deps
import guest_image
import prepare_guest_tools
import prepare_io500
import stage_guest_rootfs as stage


ROOTFS_CACHE_SCHEMA = "hf3fs.cxl-riscv-rootfs-payload.v1"


def _artifact_hashes(items: list[dict]) -> list[dict[str, str]]:
    return sorted(({"name": item["name"], "sha256": item["sha256"]} for item in items),
                  key=lambda item: item["name"])


def payload_identity(*, base_manifest: Path, build: dict, tools: dict,
                     io500_manifest: Path | None, init: Path,
                     strip_debug_tool: Path | None) -> tuple[str, dict]:
    """Describe inputs which determine rootfs bytes, excluding provenance-only paths."""
    content = {
        "schema": ROOTFS_CACHE_SCHEMA,
        "base_manifest_sha256": full_deps.sha256(base_manifest),
        "artifacts": _artifact_hashes(build.get("artifacts", [])),
        "runtime_artifacts": _artifact_hashes(build.get("runtime_artifacts", [])),
        "runtime_libraries": sorted(
            ({"name": name, "sha256": item["sha256"]}
             for name, item in build.get("dependencies", {}).get("libraries", {}).items()),
            key=lambda item: item["name"]),
        "guest_tools": tools,
        "io500_manifest_sha256": full_deps.sha256(io500_manifest) if io500_manifest else None,
        "init_sha256": full_deps.sha256(init),
        "strip_debug_tool_sha256": full_deps.sha256(strip_debug_tool) if strip_debug_tool else None,
    }
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest(), content


def _candidate_payload_identity(record: dict) -> str | None:
    cached = record.get("rootfs_cache", {})
    if cached.get("schema") == ROOTFS_CACHE_SCHEMA and isinstance(cached.get("payload_identity"), str):
        return cached["payload_identity"]
    try:
        build_path = Path(record["build_manifest"])
        if full_deps.sha256(build_path) != record["build_manifest_sha256"]:
            return None
        build = stage.load_object(build_path, "cached rootfs build manifest")
        content = {
            "schema": ROOTFS_CACHE_SCHEMA,
            "base_manifest_sha256": full_deps.sha256(Path(record["base_rootfs_manifest"])),
            "artifacts": _artifact_hashes(build.get("artifacts", [])),
            "runtime_artifacts": _artifact_hashes(build.get("runtime_artifacts", [])),
            "runtime_libraries": sorted(
                ({"name": name, "sha256": item["sha256"]}
                 for name, item in build.get("dependencies", {}).get("libraries", {}).items()),
                key=lambda item: item["name"]),
            "guest_tools": record["guest_tools"],
            "io500_manifest_sha256": (record.get("io500_bundle") or {}).get("manifest_sha256"),
            "init_sha256": record["bootstrap_files"]["init"],
            "strip_debug_tool_sha256": (record.get("strip_debug") or {}).get("tool_sha256"),
        }
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return None
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validate_cached_payload(record: dict, identity: str) -> Path | None:
    if (record.get("schema") != stage.MANIFEST_SCHEMA or record.get("profile") != "full-3fs"
            or _candidate_payload_identity(record) != identity):
        return None
    try:
        root = Path(record["root"]).resolve(strict=True)
        if not root.is_dir():
            return None
        for item in record["binaries"].values():
            path = Path(item["path"]).resolve(strict=True)
            if not path.is_relative_to(root) or full_deps.sha256(path) != item["sha256"]:
                return None
        for relative, digest in record["runtime_files"].items():
            path = (root / relative).resolve(strict=True)
            if not path.is_relative_to(root) or full_deps.sha256(path) != digest:
                return None
        if full_deps.sha256(root / "init") != record["bootstrap_files"]["init"]:
            return None
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return None
    return root


def _reuse_cached_payload(output: Path, identity: str, build_manifest: Path, closure: Path,
                          candidates: list[Path]) -> Path | None:
    for manifest_path in sorted(candidates, reverse=True):
        try:
            record = stage.load_object(manifest_path, "cached full rootfs manifest")
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        root = _validate_cached_payload(record, identity)
        if root is None:
            continue
        output.symlink_to(root, target_is_directory=True)
        records = {}
        for name, item in record["binaries"].items():
            relative = Path(item["path"]).resolve(strict=True).relative_to(root)
            records[name] = dict(item, path=str(output / relative))
        record.update({
            "root": str(output),
            "build_manifest": str(build_manifest.resolve()),
            "build_manifest_sha256": full_deps.sha256(build_manifest),
            "source_closure": str(closure.resolve()),
            "source_closure_sha256": full_deps.sha256(closure),
            "binaries": records,
            "rootfs_cache": {"schema": ROOTFS_CACHE_SCHEMA, "cache_hit": True,
                             "payload_identity": identity, "source_manifest": str(manifest_path.resolve())},
        })
        manifest = output.parent / f"{output.name}-rootfs-manifest.json"
        manifest.write_text(json.dumps(record, indent=2) + "\n")
        return manifest
    return None


def verify_build(manifest_path: Path, closure_path: Path) -> dict:
    manifest = stage.load_object(manifest_path, "3FS build manifest")
    closure = stage.load_object(closure_path, "phase-1 source closure")
    if (manifest.get("schema") != "hf3fs.riscv-build.v1" or manifest.get("status") != "passed"
            or manifest.get("profile") != "full-3fs"):
        raise ValueError("full RISC-V build did not pass")
    if (closure.get("schema") != "hf3fs.cxl-source-closure.v1" or closure.get("status") != "passed"
            or closure.get("gate") != "phase1"):
        raise ValueError("phase-1 source closure did not pass")
    if manifest.get("source_closure_sha256") != full_deps.sha256(closure_path):
        raise ValueError("build and source closure do not match")
    if closure.get("source") != full_deps.source_identity(stage.PROJECT_ROOT):
        raise ValueError("current source differs from the full build")
    artifacts = manifest.get("artifacts", [])
    names = [item["name"] for item in artifacts]
    if len(set(names)) != len(names) or not set((*build_riscv.APPLICATIONS, *build_riscv.PROBES)) <= set(names):
        raise ValueError("full build artifact list is incomplete or duplicated")
    for item in [*artifacts, *manifest.get("runtime_artifacts", [])]:
        path = Path(item["path"]).resolve(strict=True)
        if not path.is_relative_to(stage.PROJECT_ROOT / "build"):
            raise ValueError("build artifact is outside the approved checkout")
        if full_deps.sha256(path) != item["sha256"]:
            raise ValueError(f"build artifact changed: {item['name']}")
        stage.inspect_required_elf(path, item["name"])
    return manifest


def execute(base_manifest: Path, build_manifest: Path, closure: Path, sysroot: Path, output: Path,
            strip_debug_tool: Path | None = None, io500_manifest: Path | None = None) -> Path:
    build = verify_build(build_manifest, closure)
    base = stage.load_object(base_manifest, "base rootfs manifest")
    if base.get("schema") != stage.MANIFEST_SCHEMA:
        raise ValueError("base rootfs manifest schema is unsupported")
    source = Path(base["root"]).resolve(strict=True)
    for name, record in base["binaries"].items():
        path = Path(record["path"]).resolve(strict=True)
        if not path.is_relative_to(source) or full_deps.sha256(path) != record["sha256"]:
            raise ValueError(f"base rootfs artifact changed: {name}")
    runtime = sysroot.resolve(strict=True)
    for record in build["dependencies"]["libraries"].values():
        path = Path(record["resolved_path"]).resolve(strict=True)
        if not path.is_relative_to(runtime) or full_deps.sha256(path) != record["sha256"]:
            raise ValueError("target sysroot differs from full build dependencies")
    destination = stage.require_output(output)
    init_source = Path(__file__).resolve().parent / "guest/init"
    tools = prepare_guest_tools.verify()
    benchmark = prepare_io500.verify(io500_manifest) if io500_manifest else None
    tool = strip_debug_tool.resolve(strict=True) if strip_debug_tool else None
    identity, identity_content = payload_identity(
        base_manifest=base_manifest.resolve(), build=build, tools=tools,
        io500_manifest=io500_manifest.resolve() if io500_manifest else None,
        init=init_source, strip_debug_tool=tool)
    cached = _reuse_cached_payload(
        destination, identity, build_manifest, closure,
        list(destination.parent.glob("*-rootfs-manifest.json")))
    if cached is not None:
        return cached
    shutil.copytree(source, destination, symlinks=True)
    staged_init = destination / "init"
    if staged_init.exists() or staged_init.is_symlink():
        staged_init.unlink()
    stage.install_file(init_source, staged_init, executable=True)
    binaries = {name: destination / Path(item["path"]).relative_to(source)
                for name, item in base["binaries"].items()}
    for item in build.get("runtime_artifacts", []):
        binaries[item["name"]] = stage.install_file(
            Path(item["path"]), destination / "usr/lib" / item["name"])
    for item in build["artifacts"]:
        target = destination / "opt/3fs/bin" / item["name"]
        if target.exists():
            target.unlink()  # Only the fresh run-owned copy can be replaced.
        binaries[item["name"]] = stage.install_file(Path(item["path"]), target, executable=True)
    stage.stage_runtime_closure(list(binaries.values()), runtime, destination)
    tool_root = Path(tools["root"])
    for relative in tools["binaries"]:
        binaries[Path(relative).name] = stage.install_file(
            full_deps.contained_file(tool_root, relative), destination / relative, executable=True)
    stage.stage_runtime_closure([binaries[Path(name).name] for name in tools["binaries"]], tool_root, destination)
    if io500_manifest:
        binaries.update(prepare_io500.install(io500_manifest, destination))
    runtime_files = {}
    stripped = {}
    for path in sorted(destination.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as source_file:
            if source_file.read(4) != b"\x7fELF":
                continue
        relative = str(path.relative_to(destination))
        stage.inspect_required_elf(path, relative)
        # Adopted official benchmark artifacts retain their exact reference hashes.
        if tool and not relative.startswith("opt/io500/") and not (
                benchmark and relative == prepare_io500.LOADER.lstrip("/")):
            stripped[relative] = {"before_sha256": full_deps.sha256(path)}
            subprocess.run([str(tool), "--strip-debug", str(path)], check=True)
        runtime_files[relative] = full_deps.sha256(path)
    records = stage.verify_guest_closure(destination, binaries)
    errors = guest_image.validate_rootfs(destination, "full-3fs")
    if errors:
        raise ValueError("; ".join(errors))
    for applet in ("dd", "cmp", "sha256sum", "head", "tail", "test", "wc", "stat", "timeout", "hostname"):
        target = destination / "bin" / applet
        if not target.exists() and not target.is_symlink():
            target.symlink_to("busybox")
    manifest = destination.parent / f"{destination.name}-rootfs-manifest.json"
    manifest.write_text(json.dumps({
        "schema": stage.MANIFEST_SCHEMA, "profile": "full-3fs", "root": str(destination),
        "base_rootfs_manifest": str(base_manifest.resolve()),
        "build_manifest": str(build_manifest.resolve()), "build_manifest_sha256": full_deps.sha256(build_manifest),
        "source_closure": str(closure.resolve()), "source_closure_sha256": full_deps.sha256(closure),
        "target_runtime_sysroot": str(runtime), "binaries": records,
        "guest_tools": tools,
        "io500_bundle": dict(manifest=str(io500_manifest.resolve()),
                              manifest_sha256=full_deps.sha256(io500_manifest),
                              record=benchmark) if io500_manifest else None,
        "runtime_files": runtime_files,
        "bootstrap_files": {"init": full_deps.sha256(staged_init)},
        "strip_debug": {"tool": str(tool), "tool_sha256": full_deps.sha256(tool), "files": stripped} if tool else None,
        "rootfs_cache": {"schema": ROOTFS_CACHE_SCHEMA, "cache_hit": False,
                         "payload_identity": identity, "content": identity_content},
    }, indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("base-manifest", "build-manifest", "source-closure", "sysroot", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--strip-debug-tool", type=Path)
    parser.add_argument("--io500-manifest", type=Path)
    args = parser.parse_args()
    print(execute(args.base_manifest, args.build_manifest, args.source_closure, args.sysroot, args.output,
                  args.strip_debug_tool, args.io500_manifest))


if __name__ == "__main__":
    main()
