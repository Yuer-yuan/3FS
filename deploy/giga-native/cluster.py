#!/usr/bin/env python3
"""Run one exact-owned native 3FS CXL/IO500 case on giga."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import resource
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from typing import Callable

HERE = Path(__file__).resolve().parent
CXL_DEPLOY = HERE.parent / "cxl-riscv"
sys.path.insert(0, str(CXL_DEPLOY))

import full_stack
import make_phase1_manifest as manifests

import build
import io500
import runtime
import topology as topology_module
import storage_layout


HOSTNAME = "victoryang00-threadripper"
SCHEMA = "hf3fs.giga-native-run.v1"
COUNTERS = (
    "rdma_open_attempts", "tcp_receive_bytes", "tcp_data_plane_bytes",
    "core_tcp_bytes", "bootstrap_control_bytes", "bootstrap_serving_bytes",
    "cxl_rpc_requests", "cxl_rpc_responses", "cxl_rpc_bytes",
    "cxl_bulk_read_bytes", "cxl_bulk_write_bytes",
)
MIN_NOFILE = 65_536
TARGET_NOFILE = 1_048_576
STORAGE_PARENT = Path("/tmp")
LANE_COUNT = 256
CXL_POLL_PROFILES = {
    "adaptive": full_stack.CxlPolling(
        spin="0us", yields=0, sleep="0us", adaptive=True,
        idle_sleep_min="1us", idle_sleep_max="50us",
    ),
    "native-default": full_stack.CxlPolling(spin="20us", yields=8, sleep="10us"),
    "sleep-1ms": full_stack.CxlPolling(spin="0us", yields=0, sleep="1ms"),
    "sleep-10ms": full_stack.CxlPolling(spin="0us", yields=0, sleep="10ms"),
    "busy": full_stack.CxlPolling(spin="0us", yields=0, sleep="0us"),
}


class ClusterError(RuntimeError):
    pass


def _raise_nofile_limit() -> int:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = min(TARGET_NOFILE, hard)
    if target < MIN_NOFILE:
        raise ClusterError(f"RLIMIT_NOFILE hard limit is too low: {hard}")
    if soft < target:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    return target


@dataclass(frozen=True)
class ClusterConfig:
    repo: Path
    build_manifest: Path
    topology_name: str
    run_id: str
    profile: Path | None = None
    cxl_poll_profile: str = "adaptive"
    rpc_trace_methods: str | None = None


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _mountpoint_in(mountinfo: str, path: Path) -> bool:
    target = str(Path(path).absolute())
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) > 4 and fields[4].replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\") == target:
            return True
    return False


def _mount_active(path: Path) -> bool:
    return _mountpoint_in(Path("/proc/self/mountinfo").read_text(errors="replace"), path)


def _chains_ready(output: str, expected: int = 4) -> bool:
    return "OFFLINE" not in output and output.count("(SERVING-UPTODATE)") == expected


def _completed_routing_version(output: str) -> int:
    """Observe a completed native refresh, not just receipt of a candidate view.

    MgmtdClient.cc logs the version before updateRoutingInfo(), which may discard
    an incomplete view. The operation's success log is emitted after the store.
    """
    pending = completed = 0
    for line in output.splitlines():
        match = re.search(r'MgmtdClient: get new routing info version (\d+)', line)
        if match:
            pending = int(match[1])
        if 'MgmtdClient: discard incomplete routing info version' in line:
            pending = 0
        if re.search(r'MgmtdClientOp RefreshRoutingInfo .*\] succeeded\.', line):
            completed = max(completed, pending)
            pending = 0
    return completed


def _probe_storage_fallocate(path: Path) -> None:
    probe_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".hf3fs-fallocate-", dir=path, delete=False) as probe:
            probe_path = Path(probe.name)
        for arguments in (("-l", "1M"), ("-p", "-o", "0", "-l", "512K")):
            completed = subprocess.run(
                ["/usr/bin/fallocate", *arguments, str(probe_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            if completed.returncode:
                raise ClusterError(f"storage filesystem lacks fallocate support: {completed.stdout.strip()}")
    finally:
        if probe_path is not None:
            probe_path.unlink(missing_ok=True)


def preflight(repo: Path, selected: topology_module.Topology) -> dict:
    repo = Path(repo).resolve(strict=True)
    nofile = _raise_nofile_limit()
    if platform.node() != HOSTNAME:
        raise ClusterError(f"native runs require {HOSTNAME}, got {platform.node()}")
    if platform.machine() != "x86_64":
        raise ClusterError("native runs require x86_64")
    tools = {}
    for name in ("taskset", "numactl", "fusermount3", "mpirun.openmpi", "unshare", "mount", "fallocate"):
        path = shutil.which(name)
        if path is None:
            raise ClusterError(f"missing runtime tool: {name}")
        tools[name] = str(Path(path).resolve())
    fuse = Path("/dev/fuse")
    if not fuse.exists() or not os.access(fuse, os.R_OK | os.W_OK):
        raise ClusterError("/dev/fuse is not accessible")
    _probe_storage_fallocate(STORAGE_PARENT)
    free = {"/dev/shm": _free_bytes(Path("/dev/shm")), str(STORAGE_PARENT): _free_bytes(STORAGE_PARENT)}
    if free["/dev/shm"] < 8 * 1024**3:
        raise ClusterError("/dev/shm has less than 8 GiB free")
    if free[str(STORAGE_PARENT)] < 12 * 1024**3:
        raise ClusterError(f"{STORAGE_PARENT} has less than 12 GiB free")
    occupied = [port for port in selected.ports.values() if not runtime.port_is_free(port)]
    if occupied:
        raise ClusterError(f"required TCP ports are occupied: {occupied}")
    return {
        "hostname": platform.node(), "machine": platform.machine(),
        "topology": topology_module.validate_host_topology(selected=selected),
        "tools": tools, "free_bytes": free, "ports": selected.ports,
        "rlimit_nofile": nofile,
    }


def _session(run_id: str) -> int:
    return int.from_bytes(hashlib.sha256(run_id.encode()).digest()[:4], "big") or 1


def _native_manifest(clients: int, session: int, region_bytes: int, *, storage_count: int = 1) -> dict:
    return storage_layout.native_manifest(clients, storage_count, session, region_bytes)


def _listening_sockets(pid: int) -> list[dict]:
    inodes = set()
    for fd in Path(f"/proc/{pid}/fd").iterdir():
        try:
            link = os.readlink(fd)
        except FileNotFoundError:
            continue
        match = re.fullmatch(r"socket:\[(\d+)\]", link)
        if match:
            inodes.add(match[1])
    sockets = []
    for line in Path(f"/proc/{pid}/net/tcp").read_text().splitlines()[1:]:
        row = line.split()
        if row[3] == '0A' and row[9] in inodes:
            address, port = row[1].split(':')
            sockets.append(dict(address=socket.inet_ntoa(bytes.fromhex(address)[::-1]),
                                port=int(port, 16), inode=int(row[9])))
    return sorted(sockets, key=lambda item: item['port'])


def _replace_config_paths(
    directory: Path, *, old_directory: str, log_directory: Path, region: Path,
    fdb_cluster: Path, fdb_library: Path, storage: Path, manifest: Path,
    lock: Path, receipt: str,
) -> None:
    replacements = {
        old_directory: str(directory),
        full_stack.LOG: str(log_directory),
        "/dev/dax0.0": str(region),
        "/opt/3fs/etc/fdb.cluster": str(fdb_cluster),
        "/usr/lib/libfdb_c.so": str(fdb_library),
        f"{directory}/manifest.json": str(manifest),
        f"{directory}/authority.lock": str(lock),
    }
    for index in range(1, 5):
        replacements[f"/var/lib/3fs/storage/data{index}"] = str(storage / f"data{index}")
    for path in directory.iterdir():
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for source, destination in replacements.items():
            text = text.replace(source, destination)
        text = text.replace("region_type = 'Dax'", "region_type = 'File'")
        text = text.replace("region_offset = '2MB'", "region_offset = '0B'")
        text = text.replace("10.73.0.2", "127.0.0.1")
        listener = "[server.base.groups.listener]\n"
        if listener in text:
            # full_stack's first service group is the CXL data-plane group.
            # Publish only the manifest-owned loopback address so routing-info
            # refreshes cannot select an unlisted host-interface address.
            text = text.replace(listener, listener + "filter_list = ['lo']\n", 1)
        if path.name == "cxl-fabricd.toml":
            text = re.sub(r"^authority_receipt = '.*'$", f"authority_receipt = '{receipt}'", text, flags=re.M)
        path.write_text(text, encoding="utf-8")


class NativeCluster:
    def __init__(self, config: ClusterConfig):
        self.config = config
        self.repo = Path(config.repo).resolve(strict=True)
        self.topology = topology_module.topology(config.topology_name)
        self.profile = Path(config.profile or HERE / "io500-all-bounded-1s.ini").resolve(strict=True)
        self.manifest_record = build.verify_manifest(config.build_manifest)
        self.artifacts = {
            name: Path(value["path"]).resolve(strict=True)
            for name, value in self.manifest_record["artifacts"].items()
        }
        self.library_path = str(self.artifacts["libfdb_c"].parent)
        self.roots = runtime.RunRoots.create(self.repo, config.run_id, storage_parent=STORAGE_PARENT)
        self.ledger = runtime.ResultLedger(self.roots.result, {
            "schema": SCHEMA, "status": "running", "official": False,
            "expected_invalid": True, "run_id": config.run_id,
            "topology": config.topology_name, "ranks": self.topology.ranks,
            "storage_count": self.topology.storage_count, "replication_factor": 1,
            "storage_engine": {"name": "upstream-chunk-engine", "verified": False, "targets": []},
            "target_placements": [asdict(p) for p in storage_layout.target_placements(self.topology.storage_count)],
            "storage_nodes": [dict(asdict(n), llc=3-n.index) for n in self.topology.storage_nodes],
            "server_physical_cores": sum(len(v) for v in self.topology.server_cpus.values()),
            "deployment_sources": {str(p.relative_to(HERE)): runtime.sha256(p) for p in sorted(HERE.glob('*.py'))},
            "build_manifest": str(Path(config.build_manifest).resolve()),
            "build_manifest_sha256": runtime.sha256(config.build_manifest),
            "profile_sha256": runtime.sha256(self.profile),
            "cxl_polling": {
                "profile": config.cxl_poll_profile,
                "spin": CXL_POLL_PROFILES[config.cxl_poll_profile].spin,
                "yields": CXL_POLL_PROFILES[config.cxl_poll_profile].yields,
                "sleep": CXL_POLL_PROFILES[config.cxl_poll_profile].sleep,
                "adaptive": CXL_POLL_PROFILES[config.cxl_poll_profile].adaptive,
                "idle_sleep_min": CXL_POLL_PROFILES[config.cxl_poll_profile].idle_sleep_min,
                "idle_sleep_max": CXL_POLL_PROFILES[config.cxl_poll_profile].idle_sleep_max,
            },
            "rpc_trace": {
                "enabled": config.rpc_trace_methods is not None,
                "methods": config.rpc_trace_methods,
                "directory": str(self.roots.bundle / "diagnostics" / "rpc-trace"),
            },
            "start_order": [], "stop_order": [],
            "runtime_roots": {
                "volatile": str(self.roots.volatile),
                "storage": str(self.roots.storage),
                "bundle": str(self.roots.bundle),
            },
        })
        self.processes: list[runtime.OwnedProcess] = []
        self.mounts: list[Path] = []
        self.configs: list[Path] = []
        self.storage_configs: dict[int, Path] = {}
        self.storage_paths: dict[int, tuple[Path, ...]] = {}
        self.storage_processes: dict[int, runtime.OwnedProcess] = {}
        self.region = self.roots.volatile / "cxl-region.bin"
        self.fdb_cluster = self.roots.volatile / "fdb.cluster"
        self.session = _session(config.run_id)
        self.prepared = False

    def _env(self) -> dict[str, str]:
        value = dict(os.environ)
        value.pop("LD_PRELOAD", None)
        value.pop("HF3FS_RPC_TRACE_DIR", None)
        value.pop("HF3FS_RPC_TRACE_METHODS", None)
        value["LD_LIBRARY_PATH"] = self.library_path
        if self.config.rpc_trace_methods is not None:
            trace_dir = self.roots.bundle / "diagnostics" / "rpc-trace"
            trace_dir.mkdir(parents=True, exist_ok=True)
            value["HF3FS_RPC_TRACE_DIR"] = str(trace_dir)
            value["HF3FS_RPC_TRACE_METHODS"] = self.config.rpc_trace_methods
        return value

    def _record_order(self, field: str, role: str) -> None:
        values = list(self.ledger.value[field])
        values.append(role)
        self.ledger.update(**{field: values})

    def prepare(self) -> None:
        snapshot = preflight(self.repo, self.topology)
        topology_module.validate_profile(self.profile)
        self.region.touch(exist_ok=False)
        os.truncate(self.region, self.topology.region_bytes)
        helper = (
            "import mmap,os,sys; p=sys.argv[1]; n=int(sys.argv[2]); "
            "f=os.open(p,os.O_RDWR); m=mmap.mmap(f,n,flags=mmap.MAP_SHARED); "
            "[(m.__setitem__(i,0)) for i in range(0,n,mmap.PAGESIZE)]; "
            "m.flush(); m.close(); os.close(f); print(n//mmap.PAGESIZE)"
        )
        prefault = subprocess.run(
            ["/usr/bin/numactl", "--membind=1", sys.executable, "-c", helper,
             str(self.region), str(self.topology.region_bytes)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
        )
        if prefault.returncode or prefault.stdout.strip() != str(self.topology.region_bytes // 4096):
            raise ClusterError(f"node-1 region prefault failed: {prefault.stderr.strip()}")
        token = hashlib.sha256((self.config.run_id + "-fdb").encode()).hexdigest()[:32]
        self.fdb_cluster.write_text(f"hf3fs:{token}@127.0.0.1:4500\n", encoding="utf-8")
        for node in self.topology.storage_nodes:
            root = self.roots.storage if self.topology.storage_count == 1 else self.roots.storage / f"node-{node.node_id}"
            paths = tuple(root / f"data{i + 1}" for i in range(4 // self.topology.storage_count))
            for path in paths:
                path.mkdir(parents=True)
            self.storage_paths[node.node_id] = paths
        manifest_value = _native_manifest(self.topology.ranks, self.session, self.topology.region_bytes,
                                          storage_count=self.topology.storage_count)
        manifest_path = self.roots.bundle / "manifest.json"
        manifests.write_manifest(manifest_path, manifest_value)
        lock = self.roots.volatile / "authority.lock"
        receipt = f"hf3fs-giga-{self.config.run_id}-{self.session}"
        lock.write_text(receipt)
        config_root = self.roots.bundle / "config"
        config_root.mkdir()
        for node in range(self.topology.ranks + 1):
            stage = self.roots.volatile / f"render-node{node}"
            full_stack.render_config(
                stage, self.session, clients=self.topology.ranks, node=node,
                region_length=self.topology.region_bytes, lane_count=LANE_COUNT, cohort_config=True,
                polling=CXL_POLL_PROFILES[self.config.cxl_poll_profile],
            )
            generated = stage / full_stack.CONFIG.removeprefix("/")
            destination = config_root / f"node{node}"
            shutil.move(str(generated), destination)
            shutil.rmtree(stage)
            logs = self.roots.bundle / "logs" / f"node{node}"
            logs.mkdir(parents=True)
            _replace_config_paths(
                destination, old_directory=full_stack.CONFIG, log_directory=logs,
                region=self.region, fdb_cluster=self.fdb_cluster,
                fdb_library=self.artifacts["libfdb_c"], storage=self.roots.storage,
                manifest=manifest_path, lock=lock, receipt=receipt,
            )
            manifests.write_manifest(destination / "manifest.json", manifest_value, replace_generated=True)
            (destination / "authority.lock").write_text(receipt)
            self.configs.append(destination)
        adaptations = {}
        for node in self.topology.storage_nodes:
            destination = self.configs[0]
            if self.topology.storage_count > 1:
                destination = config_root / f"storage-{node.index}"
                adaptations[str(node.node_id)] = storage_layout.render_storage_config(
                    self.configs[0], destination, node, self.storage_paths[node.node_id],
                    self.roots.bundle / "logs" / f"storage-{node.index}")
            storage_layout.validate_storage_config(destination, node, self.storage_paths[node.node_id])
            self.storage_configs[node.node_id] = destination
        self.ledger.update(storage_config_adaptations=adaptations,
                           storage_paths={str(n): list(map(str, paths)) for n, paths in self.storage_paths.items()})
        rendered = {}
        for path in sorted(config_root.rglob("*")):
            if path.is_file():
                rendered[str(path.relative_to(config_root))] = runtime.sha256(path)
        runtime.atomic_json(self.roots.bundle / "rendered-config.json", rendered)
        self.mounts = [self.roots.volatile / f"mnt-client-{index}" for index in range(self.topology.ranks)]
        for mount in self.mounts:
            mount.mkdir()
        self.ledger.update(preflight=snapshot, manifest=manifest_value, region={
            "path": str(self.region), "bytes": self.topology.region_bytes,
            "memory_node": self.topology.memory_node, "prefault_pages": self.topology.region_bytes // 4096,
        })
        self.prepared = True

    def _start(self, role: str, executable: Path, arguments: list[str], cpus: tuple[int, ...]) -> runtime.OwnedProcess:
        argv = ["/usr/bin/taskset", "-c", ",".join(map(str, cpus)), str(executable), *arguments]
        process = runtime.OwnedProcess.start(
            role, argv, self.roots.bundle / "logs" / f"{role}.stdout.log",
            self.roots.bundle / "processes" / f"{role}.json", env=self._env(),
            cwd=self.repo / "target/build/giga-native-3fs", expected_executable=executable,
        )
        self.processes.append(process)
        self._record_order("start_order", role)
        return process

    def _wait(self, process: runtime.OwnedProcess, predicate: Callable[[], bool], description: str, seconds: float = 180) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if process.process.poll() is not None:
                raise ClusterError(f"{description} process exited with {process.process.returncode}")
            if predicate():
                return
            time.sleep(0.2)
        raise ClusterError(f"timed out waiting for {description}")

    def _port(self, port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            return False

    def _contains(self, path: Path, marker: str) -> bool:
        return path.is_file() and marker in path.read_text(errors="replace")

    def _run_command(self, role: str, argv: list[str], timeout: float = 180) -> str:
        started = time.monotonic_ns()
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        timeout_error = None
        try:
            completed = subprocess.run(
                argv, cwd=str(self.repo / "target/build/giga-native-3fs"), env=self._env(),
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            timeout_error = error
            partial = error.stdout or ''
            if isinstance(partial, bytes):
                partial = partial.decode(errors='replace')
            completed = subprocess.CompletedProcess(argv, -9, partial)
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        log = self.roots.bundle / "logs" / f"{role}.log"
        with log.open("a", encoding="utf-8") as output:
            output.write("COMMAND " + json.dumps(argv) + "\n")
            output.write(completed.stdout)
            output.write(f"\nEXIT {completed.returncode}\n")
        commands = list(self.ledger.value.get("commands", []))
        commands.append({"role": role, "argv": argv, "returncode": completed.returncode,
                         "host_start_ns": started, "host_end_ns": time.monotonic_ns(),
                         'child_user_seconds': after.ru_utime-before.ru_utime,
                         'child_system_seconds': after.ru_stime-before.ru_stime,
                         'children_peak_rss_kib': after.ru_maxrss, 'timed_out': timeout_error is not None})
        self.ledger.update(commands=commands)
        if timeout_error is not None:
            raise ClusterError(f'{role} exceeded {timeout}s; partial output retained in {log}') from timeout_error
        if completed.returncode:
            raise ClusterError(f"{role} exited with {completed.returncode}: {completed.stdout[-2000:]}")
        return completed.stdout

    def _admin(self, arguments: list[str], role: str = "admin") -> str:
        return self._run_command(role, [str(self.artifacts["admin_cli"]), "--cfg", str(self.configs[0] / "admin_cli.toml"), "--", *arguments])

    def _create_targets(self) -> None:
        """Select the upstream engine explicitly and retain each physical config."""
        archive = self.roots.bundle / "storage-targets"
        archive.mkdir(parents=True, exist_ok=False)
        placements = storage_layout.target_placements(self.topology.storage_count)
        records = []
        for index, target in enumerate(placements, 1):
            self._admin([
                "create-target", "--node-id", str(target.node_id),
                "--disk-index", str(target.disk_index), "--target-id", str(target.target_id),
                "--chain-id", str(target.chain_id), "--use-new-chunk-engine",
            ], f"admin-target-{index}")
            directory = self.storage_paths[target.node_id][target.disk_index] / str(target.target_id)
            physical = directory / "target.toml"
            saved = archive / f"{target.target_id}.toml"
            # Keep even a rejected config as evidence; never convert/reuse an old target.
            saved.write_bytes(physical.read_bytes())
            actual = tomllib.loads(saved.read_text())
            if actual.get("only_chunk_engine") is not True:
                raise ClusterError(f"target {target.target_id} did not enable the upstream chunk engine")
            if (actual.get("target_id") != target.target_id
                    or actual.get("chain_id") != target.chain_id
                    or actual.get("path") != str(directory)):
                raise ClusterError(f"target identity differs from the requested placement: {target.target_id}")
            records.append({**asdict(target), "only_chunk_engine": True,
                            "physical_config": str(physical),
                            "archive": str(saved.relative_to(self.roots.bundle)),
                            "sha256": runtime.sha256(saved)})
            self.ledger.update(storage_engine={"name": "upstream-chunk-engine",
                               "verified": len(records) == len(placements), "targets": list(records)})

    def start(self) -> None:
        if not self.prepared:
            raise ClusterError("cluster must be prepared before start")
        fdb_data = self.roots.volatile / "fdb-data"
        fdb_logs = self.roots.bundle / "logs" / "fdb"
        fdb_data.mkdir(); fdb_logs.mkdir(parents=True)
        fdb = self._start("fdbserver", self.artifacts["fdbserver"], [
            "-C", str(self.fdb_cluster), "-p", "127.0.0.1:4500", "-d", str(fdb_data),
            "-L", str(fdb_logs), "--locality-machineid", "g0", "--locality-zoneid", "g0",
            "--memory", "1024MiB", "--cache-memory", "128MiB", "--storage-memory", "128MiB",
        ], self.topology.server_cpus["fdbserver"])
        self._wait(fdb, lambda: self._port(4500), "FoundationDB", 120)
        deadline = time.monotonic() + 120
        while True:
            completed = subprocess.run(
                [str(self.artifacts["fdbcli"]), "-C", str(self.fdb_cluster), "--exec", "configure new single memory"],
                env=self._env(), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            if completed.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise ClusterError(f"FoundationDB configure failed: {completed.stdout[-2000:]}")
            time.sleep(1)
        self._run_command("fdb-smoke", [str(self.artifacts["fdb_client_smoke"]), "--cluster-file", str(self.fdb_cluster),
                          "--key", self.config.run_id, "--value", "native-fdb-ok"])
        fabric = self._start("cxl-fabricd", self.artifacts["cxl-fabricd"], ["--cfg", str(self.configs[0] / "cxl-fabricd.toml")], self.topology.server_cpus["cxl-fabricd"])
        fabric_log = self.roots.bundle / "logs" / "cxl-fabricd.stdout.log"
        self._wait(fabric, lambda: self._contains(fabric_log, "HF3FS_CXL_FABRIC_READY"), "CXL fabric", 120)
        token = (self.configs[0] / "token").read_text().strip()
        self._admin(["user-add", "--root", "--admin", "--token", token, "0", "root"], "admin-user-add")
        self._admin(["user-set-token", "--new", "0"], "admin-user-token")
        self._admin([
            "init-cluster", "--mgmtd", str(self.configs[0] / "mgmtd_main.toml"),
            "--meta", str(self.configs[0] / "meta_main.toml"), "--storage", str(self.configs[0] / "storage_main.toml"),
            "--fuse", str(self.configs[0] / "hf3fs_fuse_main.toml"), "--skip-config-check", "1", "524288", "1",
        ], "admin-init-cluster")
        for key, role, port in (("mgmtd", "mgmtd_main", 12501), ("meta", "meta_main", 12502)):
            process = self._start(role.removesuffix("_main"), self.artifacts[role], [
                "--app_cfg", str(self.configs[0] / f"{key}_main_app.toml"),
                "--launcher_cfg", str(self.configs[0] / f"{key}_main_launcher.toml"),
                "--cfg", str(self.configs[0] / f"{key}_main.toml"),
            ], self.topology.server_cpus[key])
            service_log = self.roots.bundle / "logs" / "node0" / f"{key}.log"
            self._wait(process, lambda p=port, path=service_log: self._port(p) and self._contains(path, "Start server finished"), key, 300)
        for node in self.topology.storage_nodes:
            directory = self.storage_configs[node.node_id]
            process = self._start(node.role, self.artifacts['storage_main'], [
                '--app_cfg', str(directory / 'storage_main_app.toml'),
                '--launcher_cfg', str(directory / 'storage_main_launcher.toml'),
                '--cfg', str(directory / 'storage_main.toml'),
            ], node.cpus)
            self.storage_processes[node.node_id] = process
            log_dir = 'node0' if self.topology.storage_count == 1 else f'storage-{node.index}'
            service_log = self.roots.bundle / 'logs' / log_dir / 'storage.log'
            self._wait(process, lambda p=node.port, path=service_log: self._port(p) and self._contains(path, 'Start server finished'), node.role, 300)
        deadline = time.monotonic() + 180
        pids = {node_id: p.pid for node_id, p in self.storage_processes.items()}
        while True:
            nodes = self._admin(["list-nodes"], "admin-list-nodes")
            try:
                node_records = storage_layout.validate_nodes(nodes, self.topology.storage_nodes, pids)
                break
            except ValueError:
                pass
            if time.monotonic() >= deadline:
                raise ClusterError("meta/storage heartbeats did not connect")
            time.sleep(2)
        core_records = []
        for node in self.topology.storage_nodes:
            path = self.roots.bundle / ('storage-core-config.toml' if node.index == 0 else f'storage-{node.index}-core-config.toml')
            self._admin(['get-config', '--node-id', str(node.node_id), '--output-file', str(path)], f'admin-core-proof-{node.node_id}')
            actual = tomllib.loads(path.read_text())
            expected = tomllib.loads((self.storage_configs[node.node_id] / 'storage_main.toml').read_text())
            storage_layout.validate_live_config(actual, expected)
            listeners = _listening_sockets(pids[node.node_id])
            addresses = storage_layout.validate_listeners(listeners, node, set(self.topology.ports.values()))
            core_records.append(dict(node_id=node.node_id, pid=pids[node.node_id], listeners=listeners,
                                     **addresses,
                                     core_config=str(path), sha256=runtime.sha256(path)))
        self.ledger.update(node_readiness=node_records, storage_core=core_records)
        self._create_targets()
        self._admin(["upload-chains", str(self.configs[0] / "chains.csv")], "admin-upload-chains")
        self._admin(["upload-chain-table", "1", str(self.configs[0] / "chain-table.csv"), "--desc", "giga-replica-1"], "admin-upload-table")
        table = self.roots.bundle / 'actual-chain-table.csv'
        self._admin(['dump-chain-table', '1', str(table)], 'admin-dump-chain-table')
        deadline = time.monotonic() + 180
        chain_attempts = 0
        while True:
            chain_attempts += 1
            chains = self._admin(["list-chains"], "admin-list-chains")
            targets = self._admin(['list-targets'], 'admin-list-targets')
            try:
                placement = storage_layout.validate_placement(targets, chains, table.read_text(), self.topology.storage_count)
                self.ledger.update(chain_readiness={"attempts": chain_attempts, "status": "serving", 'placement': placement})
                break
            except ValueError:
                pass
            if time.monotonic() >= deadline:
                raise ClusterError("storage targets did not reach SERVING state")
            time.sleep(1)
        self._admin(["mkdir", "--perm", "0755", "test"], "admin-mkdir")
        # Target health is the mgmtd view. The meta chain allocator uses its own
        # periodically refreshed view, so mounting clients must wait for that
        # publication too. Do not shorten the native refresh interval or retry
        # failed application writes to hide an incomplete startup.
        admin_log = self.roots.bundle / 'logs/node0/admin.log'
        meta_log = self.roots.bundle / 'logs/node0/meta.log'
        required_version = _completed_routing_version(admin_log.read_text(errors='replace'))
        if required_version <= 0:
            raise ClusterError('cannot establish published chain-table routing version')
        meta_process = next(p for p in self.processes if p.receipt.role == 'meta')
        wait_started = time.monotonic_ns()
        self._wait(meta_process,
            lambda: _completed_routing_version(meta_log.read_text(errors='replace')) >= required_version,
            'meta chain-table routing publication', 180)
        self.ledger.update(meta_routing_readiness=dict(required_version=required_version,
            observed_version=_completed_routing_version(meta_log.read_text(errors='replace')),
            wait_ns=time.monotonic_ns()-wait_started, evidence='completed native refresh in meta.log'))

    def mount_clients(self) -> None:
        for index, mount in enumerate(self.mounts):
            process = self._start(f"fuse-{index}", self.artifacts["hf3fs_fuse_main"], [
                "--launcher_cfg", str(self.configs[index + 1] / "hf3fs_fuse_main_launcher.toml"),
                f"--launcher_config.mountpoint={mount}",
            ], (self.topology.client_cpus[index],))
            self._wait(process, lambda path=mount: (path / "test").is_dir(), f"FUSE client {index}", 300)
        proof = self.mounts[0] / "test" / f"cross-client-{self.config.run_id}"
        payload = hashlib.sha256(self.config.run_id.encode()).digest() * 4096
        proof.write_bytes(payload)
        os.sync()
        for mount in self.mounts:
            if (mount / "test" / proof.name).read_bytes() != payload:
                raise ClusterError("cross-client FUSE readback differs")
        proof.unlink()
        self.ledger.update(cross_client={"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
        mappings = [runtime.numa_mapping(process.pid, self.region) for process in self.processes if process.receipt.role != "fdbserver"]
        self.ledger.update(numa_mappings=mappings, snapshot_before_io=self.snapshot("before-io"))

    def snapshot(self, stage: str) -> dict:
        processes = []
        for process in self.processes:
            if process.process.poll() is not None:
                continue
            status = Path(f"/proc/{process.pid}/status").read_text(errors="replace")
            stat = Path(f'/proc/{process.pid}/stat').read_text().rsplit(')', 1)[1].split()
            thread_affinity = {}
            for thread in Path(f'/proc/{process.pid}/task').iterdir():
                try:
                    thread_affinity[thread.name] = sorted(os.sched_getaffinity(int(thread.name)))
                except ProcessLookupError:
                    continue
            processes.append({"role": process.receipt.role, "pid": process.pid,
                              "cpus": re.findall(r"(?m)^Cpus_allowed_list:\s*(.*)$", status),
                              'cpu_user_ticks': int(stat[11]), 'cpu_system_ticks': int(stat[12]),
                              'clock_ticks_per_second': os.sysconf('SC_CLK_TCK'),
                              'rss_kib': re.findall(r'(?m)^VmRSS:\s*(\d+)', status),
                              'thread_affinity': thread_affinity,
                              "threads": len(list(Path(f"/proc/{process.pid}/task").iterdir()))})
        value = {"stage": stage, "host_monotonic_ns": time.monotonic_ns(), "processes": processes,
                 "mountinfo": [line for line in Path("/proc/self/mountinfo").read_text(errors="replace").splitlines()
                               if str(self.roots.volatile) in line],
                 "free_bytes": {"volatile": _free_bytes(self.roots.volatile), "storage": _free_bytes(self.roots.storage)}}
        runtime.atomic_json(self.roots.bundle / f"snapshot-{stage}.json", value)
        return value

    def _transport_records(self) -> list[dict]:
        records = []
        for path in (self.roots.bundle / "logs").rglob("*.log"):
            for match in re.finditer(r"(?m)^HF3FS_CXL_TRANSPORT_COUNTERS (\{[^\r\n]+\})", path.read_text(errors="replace")):
                value = json.loads(match.group(1))
                value["source_log"] = str(path.relative_to(self.roots.bundle))
                records.append(value)
        return records

    def stop(self) -> dict:
        errors = []
        actions = []
        for process in reversed(self.processes):
            role = process.receipt.role
            try:
                if role.startswith("fuse-"):
                    index = int(role.split("-", 1)[1])
                    mount = self.mounts[index]
                    if _mount_active(mount):
                        completed = subprocess.run(["/usr/bin/fusermount3", "-u", str(mount)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                        if completed.returncode:
                            subprocess.run(["/usr/bin/fusermount3", "-uz", str(mount)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
                        if _mount_active(mount):
                            subprocess.run(["/usr/bin/umount", "-l", str(mount)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
                        if _mount_active(mount):
                            raise ClusterError(f"FUSE mount remained active after unmount: {mount}")
                    try:
                        process.process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        pass
                actions.append(process.stop(30, 5))
                self._record_order("stop_order", role)
            except Exception as error:
                errors.append({"role": role, "type": type(error).__name__, "message": str(error)})
                self.ledger.fail("cleanup", error)
        records = self._transport_records()
        active_mounts = [str(path) for path in self.mounts if _mount_active(path)]
        occupied = [port for port in self.topology.ports.values() if not runtime.port_is_free(port)]
        clean = not errors and not active_mounts and not occupied and all(process.process.poll() is not None for process in self.processes)
        result = {"actions": actions, "errors": errors, "active_mounts": active_mounts,
                  "occupied_ports": occupied, "all_owned_processes_stopped": clean,
                  "transport_records": records}
        self.ledger.update(teardown=result)
        return result


def validate_transport(records: list[dict], participants: set[int], clients: int) -> dict:
    totals = dict.fromkeys(COUNTERS, 0)
    endpoints = set()
    for record in records:
        if record.get("schema") != "hf3fs.transport-counters.v1" or record.get("clean_shutdown") is not True:
            raise ClusterError("transport record lacks clean shutdown identity")
        endpoints.add(record.get("endpoint"))
        counters = record.get("counters", {})
        if any(type(counters.get(name)) is not int or counters[name] < 0 for name in COUNTERS):
            raise ClusterError("transport counter is absent or invalid")
        if counters["tcp_receive_bytes"] != sum(counters[name] for name in ("core_tcp_bytes", "bootstrap_control_bytes", "bootstrap_serving_bytes")):
            raise ClusterError("transport TCP bytes are not fully classified")
        for name in COUNTERS:
            totals[name] += counters[name]
    if endpoints != participants:
        raise ClusterError(f"transport endpoint coverage differs: {endpoints} != {participants}")
    for endpoint in range(16, 16 + clients):
        values = [record for record in records if record.get("endpoint") == endpoint]
        if not values or sum(item["counters"]["cxl_rpc_requests"] + item["counters"]["cxl_rpc_responses"] for item in values) <= 0:
            raise ClusterError(f"FUSE endpoint {endpoint} lacks CXL RPC activity")
    for name in ("rdma_open_attempts", "tcp_data_plane_bytes", "bootstrap_serving_bytes"):
        if totals[name] != 0:
            raise ClusterError(f"forbidden serving fallback counter is nonzero: {name}")
    for name in ("core_tcp_bytes", "bootstrap_control_bytes", "cxl_rpc_requests", "cxl_rpc_responses", "cxl_rpc_bytes", "cxl_bulk_read_bytes", "cxl_bulk_write_bytes"):
        if totals[name] <= 0:
            raise ClusterError(f"required serving counter is zero: {name}")
    return totals


def execute_cluster(config: ClusterConfig, workload: Callable[[NativeCluster], dict]) -> dict:
    cluster = NativeCluster(config)
    stage = "prepare"
    workload_result = None
    teardown = None
    try:
        cluster.prepare()
        stage = "start"
        cluster.start()
        stage = "mount"
        cluster.mount_clients()
        stage = "workload"
        workload_result = workload(cluster)
        cluster.ledger.update(workload=workload_result, snapshot_after_io=cluster.snapshot("after-io"))
    except Exception as error:
        cluster.ledger.fail(stage, error)
    finally:
        teardown = cluster.stop()
    try:
        if cluster.ledger.value.get("first_failure"):
            raise ClusterError(cluster.ledger.value["first_failure"]["message"])
        if not workload_result or workload_result.get("status") != "passed":
            raise ClusterError("workload did not pass")
        if not teardown or teardown.get("all_owned_processes_stopped") is not True:
            raise ClusterError("cluster teardown is incomplete")
        participants = {item["endpoint"] for item in cluster.ledger.value["manifest"]["participants"]}
        totals = validate_transport(teardown["transport_records"], participants, cluster.topology.ranks)
        storage_transport = storage_layout.validate_storage_transport(
            teardown['transport_records'], cluster.topology.storage_nodes,
            {nid: p.pid for nid, p in cluster.storage_processes.items()}, cluster.session,
            cluster.ledger.value['manifest']['manifestSha256'])
        cluster.ledger.update(status="passed", transport_totals=totals, storage_transport=storage_transport,
                              host_end_ns=time.monotonic_ns())
        runtime.safe_remove_owned_root(cluster.roots.volatile, Path("/dev/shm"), cluster.roots.owner_token)
        runtime.safe_remove_owned_root(cluster.roots.storage, STORAGE_PARENT, cluster.roots.owner_token)
    except Exception as error:
        cluster.ledger.fail("acceptance", error)
    return cluster.ledger.value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--repo", type=Path, required=True)
    result.add_argument("--build-manifest", type=Path)
    result.add_argument("--topology", choices=topology_module.TOPOLOGIES, required=True)
    result.add_argument("--run-id")
    result.add_argument("--profile", type=Path)
    result.add_argument("--cxl-poll-profile", choices=tuple(CXL_POLL_PROFILES), default="adaptive")
    result.add_argument("--rpc-trace-methods")
    result.add_argument("--preflight", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        selected = topology_module.topology(args.topology)
        if args.preflight:
            print(json.dumps(preflight(args.repo, selected), indent=2, sort_keys=True))
            return 0
        if args.build_manifest is None or args.run_id is None:
            raise ClusterError("--build-manifest and --run-id are required for a run")
        runtime.validate_run_id(args.run_id)
        result = execute_cluster(ClusterConfig(
            args.repo, args.build_manifest, args.topology, args.run_id, args.profile,
            cxl_poll_profile=args.cxl_poll_profile,
            rpc_trace_methods=args.rpc_trace_methods,
        ), io500.run)
        print(f"hf3fs-giga-run={result['status'].upper()} topology={args.topology} bundle={Path(args.repo).resolve() / 'target/results/giga-native-3fs' / args.run_id}")
        return 0 if result["status"] == "passed" else 2
    except Exception as error:
        print(f"hf3fs-giga-run=FAIL reason={error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
