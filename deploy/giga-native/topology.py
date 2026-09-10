#!/usr/bin/env python3
"""Fixed CPU, LLC, port and IO500 profile contracts for giga."""
from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path
import re


SERVER_CPUS = {
    "fdbserver": (18,),
    "cxl-fabricd": (19,),
    "mgmtd": (20,),
    "meta": (21,),
    "storage": (22, 23),
}
CLIENT_CPUS = (1, 7, 13)
CLIENT_L3 = (0, 1, 2)
SERVER_L3 = 3
MEMORY_NODE = 1
REGION_BYTES = 1 << 30
BASE_PORTS = {
    "fdb": 4500,
    "fabric-bootstrap": 12499,
    "mgmtd": 12501,
    "meta": 12502,
    "storage": 12503,
    "admin": 12507,
    "client-0": 12516,
    "client-1": 12517,
    "client-2": 12518,
}
FIND_PHASES = ("find-easy", "find", "find-hard")
EXPECTED_PHASES = (
    "ior-easy-write", "ior-rnd4K-write", "mdtest-easy-write", "ior-rnd1MB-write",
    "mdworkbench-create", "find-easy", "ior-hard-write", "mdtest-hard-write", "find",
    "ior-rnd4K-read", "ior-rnd1MB-read", "find-hard", "mdworkbench-bench", "ior-easy-read",
    "mdtest-easy-stat", "ior-hard-read", "mdtest-hard-stat", "mdworkbench-delete",
    "mdtest-easy-delete", "mdtest-hard-read", "mdtest-hard-delete", "ior-rnd4K-easy-read",
)


@dataclass(frozen=True)
class Topology:
    name: str
    client_cpus: tuple[int, ...]
    client_l3: tuple[int, ...]
    server_cpus: dict[str, tuple[int, ...]]
    ports: dict[str, int]
    memory_node: int = MEMORY_NODE
    region_bytes: int = REGION_BYTES

    @property
    def ranks(self) -> int:
        return len(self.client_cpus)


def topology(name: str) -> Topology:
    counts = {"1c1s": 1, "2c1s": 2, "3c1s": 3}
    if name not in counts:
        raise ValueError("topology must be 1c1s, 2c1s, or 3c1s")
    count = counts[name]
    return Topology(
        name=name,
        client_cpus=CLIENT_CPUS[:count],
        client_l3=CLIENT_L3[:count],
        server_cpus=dict(SERVER_CPUS),
        ports=dict(BASE_PORTS),
    )


def read_ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    with Path(path).open(encoding="utf-8") as source:
        parser.read_file(source)
    return parser


def validate_profile(path: Path) -> dict:
    config = read_ini(path)
    required = {
        ("debug", "stonewall-time", "1"),
        ("ior-easy", "transferSize", "1m"),
        ("ior-easy", "blockSize", "64m"),
        ("ior-hard", "segmentCount", "256"),
        ("ior-rnd4K", "randomPrefill", "0"),
        ("ior-rnd4K", "blockSize", "67108864"),
        ("ior-rnd1MB", "randomPrefill", "0"),
        ("ior-rnd1MB", "blockSize", "67108864"),
        ("mdtest-easy", "n", "64"),
        ("mdtest-hard", "n", "32"),
    }
    for section, option, expected in required:
        if config.get(section, option, fallback=None) != expected:
            raise ValueError(f"bounded profile changed: {section}.{option}")
    for phase in EXPECTED_PHASES:
        if not config.has_section(phase) or not config.getboolean(phase, "run", fallback=False):
            raise ValueError(f"required IO500 phase disabled: {phase}")
    return {
        "stonewall_seconds": 1,
        "phases": list(EXPECTED_PHASES),
        "expected_invalid": True,
    }


def render_io500_config(
    source: Path,
    output: Path,
    datadir: Path,
    resultdir: Path,
    ranks: int,
) -> None:
    if ranks not in (1, 2, 3):
        raise ValueError("native IO500 requires one, two, or three ranks")
    validate_profile(source)
    config = read_ini(source)
    config["global"]["datadir"] = str(datadir)
    config["global"]["resultdir"] = str(resultdir)
    for section in FIND_PHASES:
        config[section]["nproc"] = str(ranks)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as destination:
        config.write(destination, space_around_delimiters=True)


def parse_linux_list(value: str) -> set[int]:
    result: set[int] = set()
    value = value.strip()
    if not value:
        return result
    if not re.fullmatch(r"\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*", value):
        raise ValueError(f"invalid Linux CPU list: {value!r}")
    for item in value.split(","):
        bounds = [int(part) for part in item.split("-")]
        if len(bounds) == 1:
            result.add(bounds[0])
        elif bounds[0] <= bounds[1]:
            result.update(range(bounds[0], bounds[1] + 1))
        else:
            raise ValueError(f"reversed Linux CPU range: {item}")
    return result


def _read_int(path: Path) -> int:
    return int(path.read_text().strip())


def validate_host_topology(sysfs_root: Path = Path("/sys")) -> dict:
    sysfs_root = Path(sysfs_root)
    cpu_root = sysfs_root / "devices/system/cpu"
    online = parse_linux_list((cpu_root / "online").read_text())
    selected = set(CLIENT_CPUS)
    for values in SERVER_CPUS.values():
        selected.update(values)
    if not selected <= online:
        raise ValueError(f"required CPUs are offline: {sorted(selected - online)}")

    records = []
    physical = set()
    for cpu in sorted(selected):
        root = cpu_root / f"cpu{cpu}"
        core = _read_int(root / "topology/core_id")
        package = _read_int(root / "topology/physical_package_id")
        cache = _read_int(root / "cache/index3/id")
        identity = (package, core)
        if identity in physical:
            raise ValueError("selected CPUs include SMT siblings")
        physical.add(identity)
        expected_l3 = CLIENT_L3[CLIENT_CPUS.index(cpu)] if cpu in CLIENT_CPUS else SERVER_L3
        if cache != expected_l3:
            raise ValueError(f"CPU {cpu} L3 changed: {cache} != {expected_l3}")
        records.append({"cpu": cpu, "core": core, "package": package, "l3": cache})

    node_cpus = parse_linux_list(
        (sysfs_root / f"devices/system/node/node{MEMORY_NODE}/cpulist").read_text()
    )
    if node_cpus:
        raise ValueError(f"CXL memory node {MEMORY_NODE} unexpectedly has CPUs")
    return {
        "online": sorted(online),
        "selected": records,
        "memory_node": MEMORY_NODE,
        "memory_node_cpus": [],
    }
