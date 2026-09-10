#!/usr/bin/env python3
"""Generate the immutable, runner-owned phase-1 CXL runtime manifest."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from collections.abc import Mapping, Sequence
from typing import Any


DEPLOY_DIR = Path(__file__).resolve().parent
DEFAULT_TOPOLOGY = DEPLOY_DIR / "phase1_topology.json"
TOPOLOGY_SCHEMA = "hf3fs.cxl-phase1-topology.v1"
MANIFEST_SCHEMA = "hf3fs.cxl-runtime-manifest.v1"
RANGE_KINDS = (
    "EndpointDirectory",
    "LaneDirectory",
    "CursorPages",
    "FrameRings",
    "RpcPayloadCells",
    "AllocationDirectory",
    "BulkArenas",
    "EvidenceCounters",
)


class ManifestError(RuntimeError):
    pass


def _align_up(value: int, alignment: int) -> int:
    if value < 0 or alignment <= 0:
        raise ManifestError("invalid alignment input")
    return (value + alignment - 1) // alignment * alignment


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_topology(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ManifestError(f"cannot read phase-1 topology: {error}") from error
    if not isinstance(value, dict) or value.get("schema") != TOPOLOGY_SCHEMA:
        raise ManifestError("unsupported phase-1 topology schema")
    if not isinstance(value.get("roles"), dict) or not isinstance(value.get("scenarios"), dict):
        raise ManifestError("phase-1 topology has no role/scenario maps")
    return value


def override_role_addresses(topology: Mapping[str, Any], overrides: Sequence[str]) -> dict[str, Any]:
    result = copy.deepcopy(topology)
    for override in overrides:
        role, separator, address = override.partition("=")
        if not separator or role not in result["roles"]:
            raise ManifestError(f"invalid role address override: {override}")
        record = result["roles"][role]
        if record.get("endpoint") is None or not address.startswith("CXL://"):
            raise ManifestError(f"role address override is not a mapped CXL participant: {override}")
        record["address"] = address
    return result


def select_roles(
    topology: Mapping[str, Any], scenario: str, replication_factor: int, clients: int
) -> list[str]:
    scenarios = topology["scenarios"]
    if scenario not in scenarios:
        raise ManifestError(f"unknown phase-1 scenario: {scenario}")
    if replication_factor not in (1, 2, 3):
        raise ManifestError("replication factor must be 1, 2 or 3")
    if clients not in (1, 2, 3, 4, 10):
        raise ManifestError("client count must be 1, 2, 3, 4 or 10")
    declaration = scenarios[scenario]
    roles = list(declaration["base_roles"])
    if declaration.get("storage_roles"):
        roles.extend(f"storage-{index}" for index in range(replication_factor))
    elif replication_factor != 1:
        raise ManifestError("echo supports replication factor 1 only")
    if declaration.get("client_roles"):
        roles.extend(f"client-{index}" for index in range(clients))
    if len(roles) != len(set(roles)):
        raise ManifestError("scenario contains a duplicate role")
    missing = [role for role in roles if role not in topology["roles"]]
    if missing:
        raise ManifestError("topology is missing roles: " + ", ".join(missing))
    return roles


def _layout_ranges(
    *,
    total_region_bytes: int,
    endpoint_count: int,
    lane_count: int,
    queue_depth: int,
    cell_bytes: int,
    allocation_slots_per_endpoint: int,
) -> list[dict[str, int | str]]:
    if total_region_bytes <= 0 or total_region_bytes % 4096:
        raise ManifestError("region bytes must be positive and 4 KiB aligned")
    if endpoint_count <= 0 or lane_count < endpoint_count:
        raise ManifestError("lane count must cover every endpoint ID")
    if queue_depth <= 0 or cell_bytes <= 0 or cell_bytes % 64:
        raise ManifestError("queue depth must be positive and cell bytes 64-byte aligned")
    if allocation_slots_per_endpoint <= 0:
        raise ManifestError("allocation slots per endpoint must be positive")

    lengths = [
        _align_up(endpoint_count * 64, 4096),
        lane_count * 2 * 64,
        lane_count * 4 * 4096,
        lane_count * 2 * queue_depth * 64,
        lane_count * 2 * queue_depth * cell_bytes,
        _align_up(endpoint_count * allocation_slots_per_endpoint * 64, 4096),
    ]
    offset = 4096
    ranges: list[dict[str, int | str]] = []
    for kind, length in zip(RANGE_KINDS[:6], lengths, strict=True):
        ranges.append({"kind": kind, "offset": offset, "length": length})
        offset += length

    evidence_bytes = 4096
    bulk_bytes = total_region_bytes - offset - evidence_bytes
    bulk_bytes = bulk_bytes // 64 * 64
    if bulk_bytes < endpoint_count * 1024 * 1024:
        raise ManifestError("CXL region leaves less than 1 MiB of bulk arena per endpoint")
    ranges.append({"kind": "BulkArenas", "offset": offset, "length": bulk_bytes})
    offset += bulk_bytes
    ranges.append({"kind": "EvidenceCounters", "offset": offset, "length": evidence_bytes})
    if offset + evidence_bytes > total_region_bytes:
        raise ManifestError("CXL layout exceeds the declared region")
    return ranges


def build_manifest(
    topology: Mapping[str, Any],
    *,
    scenario: str,
    replication_factor: int = 1,
    clients: int = 1,
    session_generation: int = 1,
    authority_generation: int = 1,
    total_region_bytes: int = 256 * 1024 * 1024,
    lane_count: int = 128,
    queue_depth: int = 8,
    cell_bytes: int = 64 * 1024,
    allocation_slots_per_endpoint: int = 256,
) -> dict[str, Any]:
    if session_generation <= 0 or authority_generation <= 0:
        raise ManifestError("session and authority generations must be positive")
    selected = select_roles(topology, scenario, replication_factor, clients)
    roles = topology["roles"]
    mapped = [(name, roles[name]) for name in selected if roles[name].get("endpoint") is not None]
    endpoints = [record["endpoint"] for _, record in mapped]
    addresses = [record["address"] for _, record in mapped]
    if any(not isinstance(endpoint, int) or endpoint <= 0 for endpoint in endpoints):
        raise ManifestError("mapped roles require positive integer endpoint IDs")
    if any(not isinstance(address, str) or not address.startswith("CXL://") for address in addresses):
        raise ManifestError("mapped roles require CXL addresses")
    if len(endpoints) != len(set(endpoints)) or len(addresses) != len(set(addresses)):
        raise ManifestError("active endpoint IDs and local addresses must be unique")

    authority = roles["fabric-authority"]
    if "fabric-authority" not in selected or authority.get("endpoint") not in endpoints:
        raise ManifestError("scenario must contain the fabric authority")
    endpoint_count = max(endpoints)
    ranges = _layout_ranges(
        total_region_bytes=total_region_bytes,
        endpoint_count=endpoint_count,
        lane_count=lane_count,
        queue_depth=queue_depth,
        cell_bytes=cell_bytes,
        allocation_slots_per_endpoint=allocation_slots_per_endpoint,
    )
    routes = [
        {"address": record["address"], "plane": "Data", "targetEndpoint": record["endpoint"]}
        for _, record in mapped
        if record.get("serves_data")
    ]
    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "totalRegionBytes": total_region_bytes,
        "sessionGeneration": session_generation,
        "lifecycleRecordOffset": ranges[-1]["offset"],
        "manifestSha256": "0" * 64,
        "endpointCount": endpoint_count,
        "laneCount": lane_count,
        "authorityEndpoint": authority["endpoint"],
        "authorityGeneration": authority_generation,
        "ranges": ranges,
        "participants": [
            {"endpoint": record["endpoint"], "localAddress": record["address"]}
            for _, record in mapped
        ],
        "routes": routes,
    }
    identity = {
        "topology_schema": topology["schema"],
        "scenario": scenario,
        "replication_factor": replication_factor,
        "clients": clients,
        "queue_depth": queue_depth,
        "cell_bytes": cell_bytes,
        "manifest": manifest,
    }
    manifest["manifestSha256"] = _canonical_hash(identity)
    return manifest


def write_manifest(path: Path, manifest: Mapping[str, Any], replace_generated: bool = False) -> None:
    destination = Path(os.path.normpath(path))
    if not destination.is_absolute():
        raise ManifestError("manifest output must be absolute")
    if destination.exists():
        try:
            old = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ManifestError("refusing to replace an unreadable manifest") from error
        if not replace_generated or old.get("schema") != MANIFEST_SCHEMA:
            raise ManifestError("refusing to replace an unowned manifest")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=destination.parent, prefix=destination.name + ".tmp-", delete=False
    ) as handle:
        temporary = Path(handle.name)
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(destination)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topology", type=Path, default=DEFAULT_TOPOLOGY)
    parser.add_argument("--scenario", choices=("echo", "storage", "chain", "io500"), required=True)
    parser.add_argument("--replication-factor", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--clients", type=int, choices=(1, 2, 3, 4, 10), default=1)
    parser.add_argument("--session-generation", type=int, required=True)
    parser.add_argument("--authority-generation", type=int, default=1)
    parser.add_argument("--region-bytes", type=int, default=256 * 1024 * 1024)
    parser.add_argument("--lane-count", type=int, default=128)
    parser.add_argument("--queue-depth", type=int, default=8)
    parser.add_argument("--cell-bytes", type=int, default=64 * 1024)
    parser.add_argument("--allocation-slots-per-endpoint", type=int, default=256)
    parser.add_argument(
        "--role-address",
        action="append",
        default=[],
        metavar="ROLE=CXL://IP:PORT",
        help="override a mapped role address for a run-owned network",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replace-generated", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        topology = override_role_addresses(load_topology(args.topology), args.role_address)
        manifest = build_manifest(
            topology,
            scenario=args.scenario,
            replication_factor=args.replication_factor,
            clients=args.clients,
            session_generation=args.session_generation,
            authority_generation=args.authority_generation,
            total_region_bytes=args.region_bytes,
            lane_count=args.lane_count,
            queue_depth=args.queue_depth,
            cell_bytes=args.cell_bytes,
            allocation_slots_per_endpoint=args.allocation_slots_per_endpoint,
        )
        write_manifest(args.output, manifest, args.replace_generated)
    except ManifestError as error:
        print(f"phase1-manifest: {error}", file=os.sys.stderr)
        return 2
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
