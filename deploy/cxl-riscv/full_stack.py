"""Two-guest G0-B application startup and finite FUSE read/write proof."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from string import Template

import make_phase1_manifest as manifests

DEPLOY = Path(__file__).resolve().parent
PROJECT = DEPLOY.parents[1]
CONFIG = "/opt/3fs/etc/full-stack"
LOG = "/var/log/3fs"
BIN = "/opt/3fs/bin"
TOKEN = "AADHHSOs8QA92iRe2wB1fmuL"
APPLICATIONS = ("cxl-fabricd", "mgmtd_main", "meta_main", "storage_main", "hf3fs_fuse_main", "admin_cli")
REGION_OFFSET = 2 * 1024 * 1024  # G0-A's completed DAX probe retains the first alignment unit.
REGION_LENGTH = 254 * 1024 * 1024
SERVER_CPU_LISTS = {
    "fdbserver": "0",
    "cxl-fabricd": "1-3",
    "mgmtd_main": "1-3",
    "meta_main": "1-2",
    "storage_main": "2-3",
}
SERVER_CPU_MASKS = {"cxl-fabricd": "e", "mgmtd_main": "e", "meta_main": "6", "storage_main": "c"}


def cpu_affinity_command() -> str:
    """Validate every resident thread mask and emit one bounded record per process."""
    roles = " ".join(f"{name}:$hf3fs_pid_{name.replace('-', '_')}" for name in SERVER_CPU_MASKS)
    expected = ";; ".join(f"{name}) hf3fs_expected={cpus}" for name, cpus in SERVER_CPU_LISTS.items())
    return (
        f"for hf3fs_role_pid in fdbserver:$(cat /run/fdbserver.pid) {roles}; do "
        "hf3fs_role=${hf3fs_role_pid%:*}; hf3fs_pid=${hf3fs_role_pid#*:}; "
        f"case $hf3fs_role in {expected};; *) exit 1;; esac; "
        "hf3fs_threads=$(/bin/busybox awk -v p=$hf3fs_pid -v e=$hf3fs_expected '"
        "FNR==1{n++}$1==\"Pid:\"&&$2==p{l=1}$1==\"Cpus_allowed_list:\"&&$2!=e{b=1}"
        "END{if(!n||!l||b)exit 1;print n}' /proc/$hf3fs_pid/task/*/status) || exit 1; "
        "printf 'HF3FS_CXL_CPU role=%s pid=%s threads=%s cpus=%s checked=true\\n' "
        "$hf3fs_role $hf3fs_pid $hf3fs_threads $hf3fs_expected; done"
    )


def validate_cpu_affinity(records: list[dict]) -> bool:
    if not records or any(not isinstance(r, dict) for r in records):
        return False
    if {r.get("role") for r in records} != set(SERVER_CPU_LISTS):
        return False
    pids = set()
    for role, allowed in SERVER_CPU_LISTS.items():
        matches = [r for r in records if r.get("role") == role]
        if len(matches) != 1:
            return False
        record = matches[0]
        pid = record.get("pid")
        if type(pid) is not int or pid <= 0 or pid in pids:
            return False
        pids.add(pid)
        if (type(record.get("threads")) is not int or record["threads"] <= 0 or
                record.get("cpus") != allowed or record.get("checked") is not True):
            return False
    return True


def parse_cpu_affinity(output: str) -> list[dict]:
    records = [dict(role=role, pid=int(pid), threads=int(threads), cpus=cpus, checked=True)
               for role, pid, threads, cpus in re.findall(
                   r"(?m)^HF3FS_CXL_CPU role=(\S+) pid=(\d+) threads=(\d+) cpus=([^\s]+) checked=true\r*$",
                   output)]
    if not validate_cpu_affinity(records):
        raise ValueError("resident guest CPU affinity is absent or overlaps")
    return records


def network_args(node: int, port: int) -> list[str]:
    if node not in (0, 1) or not 0 < port < 65536:
        raise ValueError("invalid guest network identity")
    mode = "listen" if node == 0 else "connect"
    return ["-netdev", f"socket,id=control,{mode}=127.0.0.1:{port}",
            "-device", f"virtio-net-pci,netdev=control,bus=pcie.0,mac=52:54:00:73:00:{node + 2:02x}"]


def render_config(root: Path, session: int, *, clients: int = 1, node: int = 0,
                  region_length: int = REGION_LENGTH, lane_count: int = 128) -> dict:
    if clients not in (1, 10) or not 0 <= node <= clients or region_length % (1024 * 1024):
        raise ValueError("invalid full-stack cohort or DAX geometry")
    directory = root / CONFIG.removeprefix("/")
    directory.mkdir(parents=True, exist_ok=False)
    # Keep sub-RPC queue handoffs responsive. The bounded cohort pools and
    # explicit server CPU partition below prevent this polling cadence from
    # competing with hundreds of runnable service threads.
    poll_sleep = "1ms"
    values = dict(FS_CLUSTER="g0_full", ADDRESS="10.73.0.2", MGMTD_PORT="12501", META_PORT="12502",
                  STORAGE_PORT="12503", CXL_MANIFEST=f"{CONFIG}/manifest.json", CXL_REGION="/dev/dax0.0",
                  FDB_TEST_CLUSTER="/opt/3fs/etc/fdb.cluster", FDB_CLIENT_LIB="/usr/lib/libfdb_c.so",
                  TOKEN=TOKEN, TOKEN_FILE=f"{CONFIG}/token",
                  TARGETS=", ".join(f"'/var/lib/3fs/storage/data{i}'" for i in range(1, 5)))
    values.update({name + "_LOG": f"{LOG}/{name.lower()}.log" for name in ("MGMTD", "META", "STORAGE", "FUSE", "ADMIN")})
    for source in (PROJECT / "tests/fuse/config").glob("*.toml"):
        text = Template(source.read_text()).substitute(values)
        text = text.replace("region_type = 'File'", "region_type = 'Dax'")
        text = text.replace("region_length = '256MB'", f"region_offset = '2MB'\nregion_length = '{region_length // 1024**2}MB'")
        if source.name == "hf3fs_fuse_main_launcher.toml" and node > 0:
            text = text.replace("endpoint = 16\n", f"endpoint = {15 + node}\n")
        if "[cxl]" in text:
            text = text.replace("[cxl]\n", "[cxl]\nattach_timeout = '120s'\nshutdown_timeout = '120s'\n"
                                "heartbeat_interval = '1s'\nauthority_stale_timeout = '120s'\n"
                                f"poll_spin = '0us'\npoll_yields = 0\npoll_sleep = '{poll_sleep}'\n")
        # TCG needs longer request deadlines; geometry and wire framing stay fixed.
        parents = re.findall(r"^\[([^\[\]\n]+)\.io_worker\.cxlsocket\]$", text, re.MULTILINE)
        text = re.sub(r"^\[([^\[\]\n]+)\.io_worker\.cxlsocket\]$",
                      lambda match: f"[{match[1]}.io_worker]\ndata_connect_timeout = '120s'\n"
                                    f"tcp_connect_timeout = '30s'\n{match[0]}", text, flags=re.MULTILINE)
        for parent in parents:
            if parent.endswith("groups"):
                continue
            text += f"\n[{parent}]\ndefault_timeout = '120s'\n"
            if clients == 10:
                text += (f"\n[{parent}.thread_pool]\nnum_proc_threads = 1\nnum_io_threads = 1\n"
                         "num_bg_threads = 1\nnum_connect_threads = 1\n")
        # StorageMessenger supplies an explicit timeout from RetryConfig, so
        # net_client.default_timeout alone does not change storage RPC waits.
        for parent in sorted({p.removesuffix(".net_client") for p in parents if p.endswith(".net_client")}):
            text += (f"\n[{parent}.retry]\ninit_wait_time = '120s'\nmax_wait_time = '120s'\n"
                     "max_retry_time = '360s'\n")
        if source.name == "hf3fs_fuse_main.toml":
            text = "max_threads = 8\nnotify_inval_threads = 2\n" + text
            # MetaClient likewise uses its own request and total retry budget.
            text += "\n[meta.retry_default]\nrpc_timeout = '120s'\nretry_total_time = '360s'\n"
        if source.name == "meta_main.toml":
            # These finite qualification runs start from empty storage and do
            # not need asynchronous physical space reclamation. Under TCG the
            # 200 ms GC scanner can starve foreground Meta/FDB requests even
            # during single-client mount readiness.
            text = text.replace("[server.meta.gc]\n", "[server.meta.gc]\n"
                                "enable = false\nscan_interval = '60s'\n")
        if clients == 10 and source.name == "storage_main.toml":
            # Each concurrent chunk update owns a pool slot until commit.
            # Twenty initial 512 KiB operations must not queue behind two
            # 4 MiB slots. Keep the 16 MiB budget with finer allocation units.
            text = text.replace("big_buffer_size = '8MB'", "big_buffer_size = '4MB'")
            text = text.replace("buffer_count = 2\n", "buffer_count = 24\n")
            text = text.replace("\nbuffer_size = '4MB'", "\nbuffer_size = '512KB'")
            text = text.replace("[server.aio_read_worker]\n",
                                "[server.aio_read_worker]\nnum_threads = 4\n")
            text += ("\n[server.base.thread_pool]\nnum_proc_threads = 10\nnum_io_threads = 10\n"
                     "num_bg_threads = 1\nnum_connect_threads = 2\n"
                     "\n[server.base.independent_thread_pool]\nnum_proc_threads = 1\nnum_io_threads = 1\n"
                     "num_bg_threads = 1\nnum_connect_threads = 1\n"
                     "\n[server.storage.write_worker]\nnum_threads = 20\nbg_num_threads = 2\n"
                     "\n[server.sync_worker]\nnum_threads = 1\n"
                     "\n[server.coroutines_pool_read]\nthreads_num = 1\n"
                     "\n[server.coroutines_pool_update]\nthreads_num = 4\n"
                     "\n[server.coroutines_pool_sync]\nthreads_num = 1\n"
                     "\n[server.coroutines_pool_default]\nthreads_num = 1\n")
        elif clients == 10 and source.name in ("mgmtd_main.toml", "meta_main.toml"):
            proc_threads = 4 if source.name == "meta_main.toml" else 2
            io_threads = 4 if source.name == "meta_main.toml" else 2
            text += (f"\n[server.base.thread_pool]\nnum_proc_threads = {proc_threads}\n"
                     f"num_io_threads = {io_threads}\n"
                     "num_bg_threads = 1\nnum_connect_threads = 1\n"
                     "\n[server.base.independent_thread_pool]\nnum_proc_threads = 1\nnum_io_threads = 1\n"
                     "num_bg_threads = 1\nnum_connect_threads = 1\n")
            if source.name == "meta_main.toml":
                # Reconfiguring every two guest seconds repeatedly prints the
                # full immutable configuration and competes with Meta/FDB work
                # under TCG. Ten seconds matches the normal client cadence.
                text = text.replace("auto_extend_client_session_interval = '2s'",
                                    "auto_extend_client_session_interval = '10s'")
                text = text.replace("auto_heartbeat_interval = '2s'",
                                    "auto_heartbeat_interval = '10s'")
                text = text.replace("auto_refresh_interval = '2s'",
                                    "auto_refresh_interval = '10s'")
        if source.name == "mgmtd_main.toml":
            # A full standard run can keep the single-primary TCG cohort alive
            # for several hours. Keep lease validation enabled while ensuring
            # emulator scheduling cannot expire the only management service.
            text = text.replace("[server.service]\n", "[server.service]\nlease_length = '43200s'\n"
                                "heartbeat_timestamp_valid_window = '43200s'\nheartbeat_fail_interval = '43200s'\n")
        (directory / source.name).write_text(text)
    for name, node in (("mgmtd", 1), ("meta", 50), ("storage", 10000)):
        (directory / f"{name}_main_app.toml").write_text(f"allow_empty_node_id = false\nnode_id = {node}\n")
    (directory / "token").write_text(TOKEN + "\n")
    topology = manifests.override_role_addresses(manifests.load_topology(manifests.DEFAULT_TOPOLOGY), [
        "fabric-authority=CXL://10.73.0.2:12499", "mgmtd=CXL://10.73.0.2:12501",
        "meta=CXL://10.73.0.2:12502", "storage-0=CXL://10.73.0.2:12503",
        "admin=CXL://10.73.0.2:12507", "client-0=CXL://10.73.0.3:12516",
    ])
    if clients == 10:
        for role in ("fabric-authority", "mgmtd", "meta", "storage-0", "admin", "fdb"):
            topology["roles"][role]["guest"] = 0
        for index in range(clients):
            topology["roles"][f"client-{index}"] = dict(
                endpoint=16 + index, guest=1 + index,
                address=f"CXL://10.73.0.{index + 3}:12516", serves_data=False)
    manifest = manifests.build_manifest(topology, scenario="storage", session_generation=session,
                                        clients=clients, total_region_bytes=region_length, lane_count=lane_count)
    manifests.write_manifest(directory / "manifest.json", manifest)
    receipt = f"hf3fs-g0-b-{session}"
    (directory / "authority.lock").write_text(receipt)
    (directory / "cxl-fabricd.toml").write_text(
        "[cxl]\nenabled = true\nmode = 'InitializeAuthority'\nregion_type = 'Dax'\n"
        f"region_path = '/dev/dax0.0'\nregion_offset = '2MB'\nregion_length = '{region_length // 1024**2}MB'\nendpoint = 1\nendpoint_generation = 1\n"
        "heartbeat_interval = '1s'\nauthority_stale_timeout = '120s'\nshutdown_timeout = '120s'\n"
        f"poll_spin = '0us'\npoll_yields = 0\npoll_sleep = '{poll_sleep}'\n"
        f"manifest_path = '{CONFIG}/manifest.json'\nauthority_owner_lock = '{CONFIG}/authority.lock'\n"
        f"authority_receipt = '{receipt}'\n")
    (directory / "chains.csv").write_text("ChainId,TargetId\n" + "".join(f"10000{i:02}001,10000{i:02}001\n" for i in range(1, 5)))
    (directory / "chain-table.csv").write_text("ChainId\n" + "".join(f"10000{i:02}001\n" for i in range(1, 5)))
    return {"manifest_sha256": hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest(),
            "manifest": manifest, "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()}}


def stage_roots(base: Path, run: Path, session: int, *, clients: int = 1,
                region_length: int = REGION_LENGTH, lane_count: int = 128) -> tuple[list[Path], dict]:
    roots = [run / f"node{node}-staging" for node in range(clients + 1)]
    nodes = {}

    def stage(node: int) -> dict:
        root = roots[node]
        shutil.copytree(base, root, symlinks=True)
        return render_config(root, session, clients=clients, node=node,
                             region_length=region_length, lane_count=lane_count)

    with ThreadPoolExecutor(max_workers=clients + 1) as pool:
        records = list(pool.map(stage, range(clients + 1)))
    record = records[0]
    for node, current in enumerate(records):
        if current["manifest_sha256"] != record["manifest_sha256"]:
            raise ValueError("guest manifest/configuration identities differ")
        record = current
        nodes[node] = current["files"]
    if clients > 1:
        record["guest_files"] = nodes
    return roots, record


def validate_evidence(evidence: dict) -> list[str]:
    errors = []
    if evidence.get("status") != "passed":
        errors.append("full 3FS application smoke did not pass")
    if evidence.get("region_type") != "Dax" or evidence.get("server_guest") != 0 or evidence.get("fuse_guest") != 1:
        errors.append("full 3FS smoke did not cross guest DAX mappings")
    if evidence.get("region_offset") != REGION_OFFSET or evidence.get("region_length") != REGION_LENGTH:
        errors.append("application DAX range is not isolated from the platform probe")
    if set(evidence.get("ready", [])) != set(APPLICATIONS):
        errors.append("full 3FS application readiness is incomplete")
    if evidence.get("bytes", 0) != 2097152 or not evidence.get("sha256") or evidence.get("sha256") != evidence.get("readback_sha256"):
        errors.append("full 3FS finite write/read/compare evidence is incomplete")
    if evidence.get("marker") != "HF3FS_CXL_FUSE_SMOKE_OK" or evidence.get("clean_retirement") is not True:
        errors.append("full 3FS completion/retirement markers are absent")
    return errors


def execute(consoles: list, run: Path, timeout: int, *, clients: int = 1,
            region_length: int = REGION_LENGTH, workload=None, prepare_workload=None) -> dict:
    record = dict(status="failed", region_type="Dax", region_offset=REGION_OFFSET, region_length=region_length,
                  server_guest=0, fuse_guest=1, ready=[], clean_retirement=False, transport_records=[])
    commands = []
    owned = []
    unresponsive = set()
    command_lock = threading.Lock()
    # Launcher configuration and FUSE client initialization can each consume
    # the configured 360-second retry window. Keep a separate finite host
    # deadline for the larger cohort without changing RPC or lease limits.
    fuse_startup_budget = min(timeout, 720) if clients > 1 else 120
    record["fuse_startup_budget_seconds"] = fuse_startup_budget

    def command(node: int, text: str, deadline: int | None = None) -> str:
        if node in unresponsive:
            raise RuntimeError(f"guest {node} console is unresponsive after a command timeout")
        with command_lock:
            sequence = len(commands)
            entry = {"node": node, "command": text, "log": str(run / f"full-command-{sequence:03}.log")}
            commands.append(entry)
            (run / "full-commands.json").write_text(json.dumps(commands, indent=2) + "\n")
        try:
            output = consoles[node].shell_command(text, deadline or timeout)
        except Exception as error:
            if isinstance(error, TimeoutError):
                unresponsive.add(node)
                record["unresponsive_guests"] = sorted(unresponsive)
            Path(entry["log"]).write_text(str(error))
            raise
        Path(entry["log"]).write_text(output)
        with command_lock:
            for match in re.finditer(r"(?m)^HF3FS_CXL_TRANSPORT_COUNTERS (\{[^\r\n]+\})", output):
                counter = dict(guest=node, **json.loads(match[1]))
                if counter not in record["transport_records"]:
                    record["transport_records"].append(counter)
        return output

    def start(name: str, node: int, arguments: str) -> None:
        variable = "hf3fs_pid_" + name.replace("-", "_")
        affinity = f"/bin/busybox taskset {SERVER_CPU_MASKS[name]} " if node == 0 and name in SERVER_CPU_MASKS else ""
        command(node, f"{affinity}{BIN}/{name} {arguments} >{LOG}/{name}.stdout 2>{LOG}/{name}.stderr & {variable}=$!")
        owned.append((name, node, variable))

    def port_ready(port: int, variable: str, name: str) -> None:
        ready = f"grep -q 'Start server finished' {LOG}/{name}.log && nc -z -w 1 10.73.0.2 {port}"
        command(0, f"attempt=0; until {ready}; do "
                f"kill -0 ${variable} || break; attempt=$((attempt+1)); [ $attempt -lt 120 ] || break; sleep 1; done; "
                f"if kill -0 ${variable} && {ready}; then true; else "
                f"tail -100 {LOG}/{name}_main.stderr {LOG}/{name}.log; false; fi")

    def admin(arguments: str) -> str:
        return command(0, f"{BIN}/admin_cli --cfg {CONFIG}/admin_cli.toml -- {arguments}")

    def capture_failure_logs() -> None:
        """Preserve bounded causal logs before shutdown messages displace them."""
        snapshots = []

        def capture(node: int) -> None:
            try:
                # Printing every raw tail can saturate a TCG UART and make the
                # guest appear unresponsive during teardown. Search the whole
                # file for causal RPC/transport lines, then retain a small raw
                # tail for context. Each pipeline is allowed to have no match.
                logs = (f"{LOG}/meta.log {LOG}/storage.log {LOG}/mgmtd.log "
                        f"{LOG}/meta_main.stderr {LOG}/storage_main.stderr"
                        if node == 0 else f"{LOG}/fuse.log {LOG}/hf3fs_fuse_main.stderr")
                command(node, f"printf '\\nHF3FS_FAILURE_CAUSES\\n'; for log in {logs}; do "
                        "[ -f \"$log\" ] || continue; tail -c 16384 \"$log\" | "
                        "grep -E 'receive request|Mkdirs|MetaServiceOp|Op (stat|mkdirs|create)|"
                        "StorageService|[Bb]atchWrite|handleUpdate|Committed local|allocate buffer|"
                        "ChannelIsLocked|Transaction::|CRITICAL|Transport.cc:.*WARNING|"
                        "FDBTransaction.cc:.*WARNING|Operation.h:.*WARNING|timed out|RPC::Timeout|"
                        "write .*fail|read .*fail' | grep -v 'Monitor.cc:150' | tail -60 || true; done; "
                        f"for log in {logs}; do [ -f \"$log\" ] || continue; "
                        "printf 'HF3FS_FAILURE_LOG_TAIL %s\\n' \"$log\"; "
                        "tail -c 4096 \"$log\" | tail -5; done", 30)
                with command_lock:
                    snapshots.append(node)
            except Exception as error:
                with command_lock:
                    record.setdefault("failure_log_snapshot_errors", []).append(
                        {"guest": node, "error": str(error)})

        with ThreadPoolExecutor(max_workers=clients + 1) as pool:
            list(pool.map(capture, range(clients + 1)))
        record["failure_log_snapshot_guests"] = sorted(snapshots)

    try:
        if clients in (1, 10):
            # FDB already runs on CPU 0. All later server-side children,
            # including transient admin clients, inherit this console mask.
            command(0, "test \"$(cat /sys/devices/system/cpu/online)\" = 0-3 && "
                    "/bin/busybox taskset -p e $$ && "
                    "test \"$(/bin/busybox awk '/^Cpus_allowed_list:/{print $2}' "
                    "/proc/$$/status)\" = 1-3")
        for node in range(clients + 1):
            command(node, f"ulimit -Hn 65536 && ulimit -Sn 65536 && mkdir -p {LOG} /var/lib/3fs/storage/data1 /var/lib/3fs/storage/data2 "
                    "/var/lib/3fs/storage/data3 /var/lib/3fs/storage/data4 /mnt/3fs && "
                    "mkdir -p /dev/shm && mount -t tmpfs -o mode=1777,size=64M tmpfs /dev/shm && "
                    f"ip addr add 10.73.0.{node + 2}/24 dev eth0 && ip link set eth0 up")
        if prepare_workload is not None:
            record["workload_preflight"] = prepare_workload(consoles, command, run)
            if record["workload_preflight"].get("status") != "passed":
                raise RuntimeError(record["workload_preflight"].get("first_failure", "workload preflight failed"))
        command(0, "blkid -s UUID /dev/vda && lsblk -o MOUNTPOINT,MODEL --json")
        start("cxl-fabricd", 0, f"--cfg {CONFIG}/cxl-fabricd.toml")
        command(0, f"attempt=0; while ! grep -q HF3FS_CXL_FABRIC_READY {LOG}/cxl-fabricd.stdout; do "
                "kill -0 $hf3fs_pid_cxl_fabricd || break; attempt=$((attempt+1)); [ $attempt -lt 120 ] || break; sleep 1; done; "
                f"grep HF3FS_CXL_FABRIC_READY {LOG}/cxl-fabricd.stdout")
        record["ready"].append("cxl-fabricd")
        admin(f"user-add --root --admin --token {TOKEN} 0 root")
        admin("user-set-token --new 0")
        admin(f"init-cluster --mgmtd {CONFIG}/mgmtd_main.toml --meta {CONFIG}/meta_main.toml "
              f"--storage {CONFIG}/storage_main.toml --fuse {CONFIG}/hf3fs_fuse_main.toml --skip-config-check 1 524288 1")
        record["ready"].append("admin_cli")
        for name, port in (("mgmtd", 12501), ("storage", 12503), ("meta", 12502)):
            start(name + "_main", 0, f"--app_cfg {CONFIG}/{name}_main_app.toml "
                  f"--launcher_cfg {CONFIG}/{name}_main_launcher.toml --cfg {CONFIG}/{name}_main.toml")
            port_ready(port, f"hf3fs_pid_{name}_main", name)
            record["ready"].append(name + "_main")
        # Wait for both heartbeats before creating targets; query retries do not
        # repeat cluster mutations and every admin process retires its endpoint.
        query = f"{BIN}/admin_cli --cfg {CONFIG}/admin_cli.toml -- list-nodes >{LOG}/nodes.txt"
        query = (f"{{ {query}; hf3fs_query_rc=$?; cat {LOG}/nodes.txt >>{LOG}/admin-query.stdout; "
                 "test $hf3fs_query_rc -eq 0; }")
        alive = "kill -0 $hf3fs_pid_mgmtd_main && kill -0 $hf3fs_pid_storage_main && kill -0 $hf3fs_pid_meta_main"
        connected = (f"grep -q '50 .*META .*HEARTBEAT_CONNECTED' {LOG}/nodes.txt && "
                     f"grep -q '10000 .*STORAGE .*HEARTBEAT_CONNECTED' {LOG}/nodes.txt")
        command(0, f"attempt=0; while [ $attempt -lt 60 ]; do {alive} || break; "
                f"{query} && {connected} && break; attempt=$((attempt+1)); sleep 2; done; "
                f"cat {LOG}/nodes.txt; {alive} && {connected}")
        # Exercise the retained Core TCP plane explicitly; a workload which
        # never calls Core cannot supply positive Core transport evidence.
        admin(f"get-config --node-id 10000 --output-file {LOG}/storage-core-config.toml")
        for disk in range(1, 5):
            target = f"10000{disk:02}001"
            admin(f"create-target --node-id 10000 --disk-index {disk - 1} --target-id {target} --chain-id {target}")
        admin(f"upload-chains {CONFIG}/chains.csv")
        admin(f"upload-chain-table 1 {CONFIG}/chain-table.csv --desc g0-replica-1")
        admin("list-chains")
        admin("mkdir --perm 0755 test")
        for node in range(1, clients + 1):
            start("hf3fs_fuse_main", node, f"--launcher_cfg {CONFIG}/hf3fs_fuse_main_launcher.toml --launcher_config.mountpoint=/mnt/3fs")

        def wait_fuse(node: int) -> None:
            command(node, "attempt=0; while [ ! -d /mnt/3fs/test ]; do kill -0 $hf3fs_pid_hf3fs_fuse_main || break; "
                f"attempt=$((attempt+1)); [ $attempt -lt {fuse_startup_budget} ] || break; sleep 1; done; test -d /mnt/3fs/test",
                fuse_startup_budget if clients > 1 else None)

        with ThreadPoolExecutor(max_workers=clients) as pool:
            list(pool.map(wait_fuse, range(1, clients + 1)))
        record["ready"].append("hf3fs_fuse_main")
        if clients > 1:
            # Exercise the first mutating FUSE Meta RPC before starting a
            # multi-hour workload. The second client observation proves that
            # success is visible outside the creating guest.
            preflight_dir = "/mnt/3fs/.hf3fs-cxl-metadata-preflight"
            created = command(1, f"mkdir {preflight_dir} && printf 'HF3FS_CXL_META_CREATE_OK\\n'", 480)
            observed = command(2, f"test -d {preflight_dir} && printf 'HF3FS_CXL_META_OBSERVE_OK\\n'", 480)
            removed = command(1, f"/bin/busybox rmdir {preflight_dir} && "
                              "printf 'HF3FS_CXL_META_REMOVE_OK\\n'", 480)
            markers = ("HF3FS_CXL_META_CREATE_OK", "HF3FS_CXL_META_OBSERVE_OK", "HF3FS_CXL_META_REMOVE_OK")
            outputs = (created, observed, removed)
            if any(marker not in output for marker, output in zip(markers, outputs)):
                raise ValueError("cross-client FUSE metadata preflight evidence is incomplete")
            record["metadata_preflight"] = {
                "status": "passed", "creator_guest": 1, "observer_guest": 2,
                "path": preflight_dir, "markers": list(markers),
            }
        if clients == 1:
            output = command(1, "dd if=/dev/urandom of=/var/lib/3fs/input bs=65536 count=32 && "
                         "dd if=/var/lib/3fs/input of=/mnt/3fs/test/smoke.bin bs=65536 conv=fsync && "
                         "dd if=/mnt/3fs/test/smoke.bin of=/var/lib/3fs/output bs=65536 iflag=direct && "
                         "test $(stat -c %s /var/lib/3fs/input) -eq 2097152 && "
                         "test $(stat -c %s /var/lib/3fs/output) -eq 2097152 && "
                         "cmp /var/lib/3fs/input /var/lib/3fs/output && sha256sum /var/lib/3fs/input /var/lib/3fs/output && "
                         "rm /mnt/3fs/test/smoke.bin && printf 'HF3FS_CXL_%s bytes=2097152\\n' FUSE_SMOKE_OK")
            hashes = re.findall(r"(?m)^([0-9a-f]{64})\s+/var/lib/3fs/(?:input|output)\s*$", output)
            if len(hashes) != 2 or hashes[0] != hashes[1] or "\nHF3FS_CXL_FUSE_SMOKE_OK bytes=2097152" not in output:
                raise ValueError("guest write/read completion evidence is incomplete")
            record.update(bytes=2097152, sha256=hashes[0], readback_sha256=hashes[1], marker="HF3FS_CXL_FUSE_SMOKE_OK")
        else:
            record["cpu_affinity"] = dict(before_io=parse_cpu_affinity(command(0, cpu_affinity_command())))
            if workload is None:
                record["clients"] = concurrent_io(consoles, command, clients)
            else:
                record["workload"] = workload(consoles, command, run)
                if record["workload"].get("status") != "passed":
                    raise RuntimeError(record["workload"].get("first_failure", "workload failed"))
            record["cpu_affinity"]["after_io"] = parse_cpu_affinity(command(0, cpu_affinity_command()))
    except Exception as error:
        record["first_failure"] = str(error)
    finally:
        if "first_failure" in record:
            capture_failure_logs()
        # FDB is owned by the outer G0 runner and stays alive through this cleanup.
        for name in ("hf3fs_fuse_main", "meta_main", "storage_main", "mgmtd_main", "cxl-fabricd"):
            matching = [item for item in owned if item[0] == name]

            def stop_owned(item) -> None:
                owned_name, node, variable = item
                try:
                    if node > 0:
                        if "first_failure" in record:
                            prefix = ("hf3fs_was_mounted=0; if grep -q ' /mnt/3fs ' /proc/mounts; then "
                                      "hf3fs_was_mounted=1; fusermount3 -uz /mnt/3fs || true; fi; "
                                      f"if kill -0 ${variable} 2>/dev/null; then kill ${variable}; fi; ")
                            allowed = 'test "$rc" -eq 0 || test "$rc" -eq 143'
                        else:
                            prefix = ("hf3fs_was_mounted=0; if grep -q ' /mnt/3fs ' /proc/mounts; then "
                                      "hf3fs_was_mounted=1; fusermount3 -u /mnt/3fs; "
                                      f"elif kill -0 ${variable} 2>/dev/null; then kill ${variable}; fi; ")
                            allowed = 'test "$rc" -eq 0 || { test $hf3fs_was_mounted -eq 0 && test "$rc" -eq 143; }'
                    else:
                        prefix = f"kill ${variable}; "
                        allowed = 'test "$rc" -eq 0 || test "$rc" -eq 143'
                    command(node, prefix + f"attempt=0; while kill -0 ${variable} 2>/dev/null; do "
                            "attempt=$((attempt+1)); [ $attempt -lt 1200 ] || break; sleep 0.1; done; "
                            f"if kill -0 ${variable} 2>/dev/null; then kill -9 ${variable}; wait ${variable}; false; "
                            f"else wait ${variable}; rc=$?; printf 'HF3FS_PROCESS_EXIT name={name} rc=%s\\n' $rc; {allowed}; fi", 180)
                except Exception as error:
                    with command_lock:
                        record.setdefault("first_failure", f"application teardown: {error}")

            with ThreadPoolExecutor(max_workers=max(1, len(matching))) as pool:
                list(pool.map(stop_owned, matching))
        try:
            command(0, f"grep HF3FS_CXL_FABRIC_RETIRED {LOG}/cxl-fabricd.stdout")
            with ThreadPoolExecutor(max_workers=clients) as pool:
                list(pool.map(lambda node: command(node, "! grep -q ' /mnt/3fs ' /proc/mounts"),
                              range(1, clients + 1)))
            record["clean_retirement"] = "first_failure" not in record
        except Exception as error:
            record.setdefault("first_failure", str(error))

        def collect(node: int) -> None:
            try:
                command(node, f"grep -h '^HF3FS_CXL_TRANSPORT_COUNTERS ' {LOG}/*.stdout || true", 60)
            except Exception as error:
                with command_lock:
                    record.setdefault("first_failure", f"counter collection: {error}")
            if "first_failure" not in record:
                try:
                    command(node, f"for log in {LOG}/*; do printf '\\nHF3FS_LOG_FILE %s\\n' \"$log\"; "
                            "tail -30 \"$log\"; done", 30)
                except Exception:
                    pass
        with ThreadPoolExecutor(max_workers=clients + 1) as pool:
            list(pool.map(collect, range(clients + 1)))
    if "first_failure" not in record:
        record["status"] = "passed"
    (run / "full-stack.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


def observe_io_markers(console, node: int, since: int) -> dict:
    observations = {}
    with console.condition:
        for marker in ("BEGIN", "END"):
            # Observed guest output can end in CR-CR-LF. Echoed shell input
            # must never count as a completion or start marker.
            match = re.compile(rf"(?m)^HF3FS_CXL_IO_{marker} node={node}\r*$").search(console.output, since)
            if not match:
                raise ValueError(f"client {node} lacks {marker} observation")
            observations[marker.lower() + "_observed_ns"] = next(
                when for end, when in console.output_chunks if end >= match.end())
    return observations


def concurrent_io(consoles: list, command, clients: int) -> list[dict]:
    """All FUSE mounts are ready before any timed workload is submitted."""
    barrier = threading.Barrier(clients)

    def run(node):
        command(node, "dd if=/dev/urandom of=/var/lib/3fs/input bs=65536 count=32")
        barrier.wait(timeout=120)
        since = len(consoles[node].output)
        # Direct writes keep the 64 KiB workload granularity. Buffered writes
        # can become 1 MiB FUSE requests; ten such requests exceed the fixed
        # RPC budget in the TCG cohort. G0 retains its buffered-write coverage.
        output = command(node,
            f"printf 'HF3FS_CXL_IO_%s node={node}\\n' BEGIN && "
            f"dd if=/var/lib/3fs/input of=/mnt/3fs/test/client{node}.bin bs=65536 oflag=direct conv=fsync && "
            f"dd if=/mnt/3fs/test/client{node}.bin of=/var/lib/3fs/output bs=65536 iflag=direct && "
            "test $(stat -c %s /var/lib/3fs/output) -eq 2097152 && "
            "cmp /var/lib/3fs/input /var/lib/3fs/output && sha256sum /var/lib/3fs/input /var/lib/3fs/output && "
            f"printf 'HF3FS_CXL_IO_%s node={node}\\n' END && "
            f"printf 'HF3FS_CXL_%s node={node} bytes=2097152\\n' FUSE_SMOKE_OK")
        hashes = re.findall(r"(?m)^([0-9a-f]{64})\s+/var/lib/3fs/(?:input|output)\s*$", output)
        if len(hashes) != 2 or hashes[0] != hashes[1] or f"\nHF3FS_CXL_FUSE_SMOKE_OK node={node} " not in output:
            raise ValueError(f"client {node} did not complete its finite FUSE verification")
        observations = observe_io_markers(consoles[node], node, since)
        return dict(guest=node, endpoint=15 + node, bytes=2097152, sha256=hashes[0],
                    io=dict(block_bytes=65536, write="direct", read="direct"),
                    readback_sha256=hashes[1], marker="HF3FS_CXL_FUSE_SMOKE_OK", **observations)

    with ThreadPoolExecutor(max_workers=clients) as pool:
        results = list(pool.map(run, range(1, clients + 1)))
        # Another independently mounted client must see each completed file.
        def neighbor(record):
            reader = record["guest"] % clients + 1
            output = command(reader, f"dd if=/mnt/3fs/test/client{record['guest']}.bin "
                             "of=/var/lib/3fs/neighbor bs=65536 iflag=direct && sha256sum /var/lib/3fs/neighbor")
            hashes = re.findall(r"(?m)^([0-9a-f]{64})\s+/var/lib/3fs/neighbor\s*$", output)
            if hashes != [record["sha256"]]:
                raise ValueError(f"client {reader} cannot verify its neighbor's completed file")
            record.update(neighbor_guest=reader, neighbor_sha256=hashes[0])
        list(pool.map(neighbor, results))
    return results
