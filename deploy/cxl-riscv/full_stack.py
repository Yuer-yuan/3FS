"""Two-guest G0-B application startup and finite FUSE read/write proof."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from string import Template

import diagnostics
import guest_jobs
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
    "cxl-fabricd": "3",
    "mgmtd_main": "1",
    "meta_main": "2-3",
    "storage_main": "3",
}
SERVER_CPU_MASKS = {"cxl-fabricd": "8", "mgmtd_main": "2", "meta_main": "c", "storage_main": "8"}


@dataclass(frozen=True)
class CxlPolling:
    spin: str
    yields: int
    sleep: str
    adaptive: bool = False
    idle_sleep_min: str = "1us"
    idle_sleep_max: str = "50us"


def render_polling(polling: CxlPolling) -> str:
    return (
        f"poll_spin = '{polling.spin}'\npoll_yields = {polling.yields}\n"
        f"poll_sleep = '{polling.sleep}'\npoll_adaptive = {'true' if polling.adaptive else 'false'}\n"
        f"poll_idle_sleep_min = '{polling.idle_sleep_min}'\n"
        f"poll_idle_sleep_max = '{polling.idle_sleep_max}'\n"
    )


def live_diagnostic_commands(node: int, phase: str) -> list[tuple[str, str]]:
    """Return short snapshots that fit the guest serial console input buffer."""
    roles = ("fdbserver|cxl-fabricd|mgmtd_main|meta_main|storage_main|hf3fs_fuse_main|"
             "io500|io500-linebuf|mpiexec.hydra|hydra_pmi_proxy")
    select = ("for p in /proc/[0-9]*; do pid=${p##*/}; [ -r $p/comm ]||continue; "
              f"comm=$(cat $p/comm); case $comm in {roles}) ;; *) continue;; esac; ")
    server_select = ("for rp in fdbserver:$(cat /run/fdbserver.pid) "
                     "cxl-fabricd:$hf3fs_pid_cxl_fabricd mgmtd_main:$hf3fs_pid_mgmtd_main "
                     "meta_main:$hf3fs_pid_meta_main storage_main:$hf3fs_pid_storage_main; do "
                     "comm=${rp%:*}; pid=${rp#*:}; p=/proc/$pid; [ -r $p/status ]||continue; ")
    logs = (f"{LOG}/mgmtd.log {LOG}/meta.log {LOG}/storage.log "
            f"{LOG}/mgmtd_main.stderr {LOG}/meta_main.stderr {LOG}/storage_main.stderr "
            "/var/log/fdb/fdbserver-console.log /var/log/fdb/trace.*.xml"
            if node == 0 else f"{LOG}/fuse.log {LOG}/hf3fs_fuse_main.stderr")
    commands = [
        ("system", f"printf 'HF3FS_LIVE_SYSTEM node={node} phase={phase}\\n'; "
         "cat /proc/uptime /proc/loadavg; printf 'HF3FS_LIVE_TCP\\n'; cat /proc/net/tcp"),
    ]
    if node == 0:
        commands.extend([
            ("service_events", f"printf 'HF3FS_LIVE_SERVICE_EVENTS\\n'; "
             f"tail -c 262144 {LOG}/meta.log | "
             "grep -E 'receive request|Mkdirs|Op (stat|mkdirs|create)|"
             "FDBTransaction.cc:.*(WARNING|ERROR)|Operation.h:.*(WARNING|ERROR)|"
             "RPC::Timeout|timed out|HF3FS_CXL_SOCKET_CLOSE|HF3FS_FDB_FUTURE|"
             "HF3FS_RPC_SERVER_LONG|HF3FS_RPC_LATE_RESPONSE|HF3FS_RPC_CREATE|"
             "HF3FS_RPC_META|HF3FS_RPC_PROCESSOR_QUEUE|HF3FS_RPC_PROCESSOR_DISPATCH' | tail -80 || true"),
            ("rpc_meta", "printf 'HF3FS_LIVE_RPC_META\\n'; for f in /var/log/3fs/rpc-*.trace; do "
             "[ -f $f ]||continue; printf '%s ' $f; head -c 256 $f; done"),
            ("process", server_select +
             "printf 'HF3FS_LIVE_PROCESS pid=%s comm=%s wchan=' $pid $comm; "
             "cat $p/wchan 2>/dev/null; printf '\\n'; "
             "grep -E '^(State|Threads|Cpus_allowed_list|voluntary_ctxt_switches|"
             "nonvoluntary_ctxt_switches):' $p/status 2>/dev/null; done"),
            ("server_threads", "for p in /proc/$(cat /run/fdbserver.pid) "
             "/proc/$hf3fs_pid_meta_main; do [ -r $p/status ]||continue; "
             "for t in $p/task/[0-9]*; do tid=${t##*/}; "
             "printf 'HF3FS_LIVE_SERVER_THREAD pid=%s tid=%s comm=' ${p##*/} $tid; "
             "cat $t/comm 2>/dev/null; printf 'state='; grep '^State:' $t/status 2>/dev/null; "
             "printf 'wchan='; cat $t/wchan 2>/dev/null; printf '\\n'; done; done"),
            ("meta_worker_stacks", "p=/proc/$hf3fs_pid_meta_main; [ -r $p/status ]||exit 0; "
             "for t in $p/task/[0-9]*; do "
             "comm=$(cat $t/comm 2>/dev/null); case $comm in SvrProc*) ;; *) continue;; esac; "
             "printf 'HF3FS_LIVE_META_STACK tid=%s comm=%s wchan=' ${t##*/} $comm; "
             "cat $t/wchan 2>/dev/null; printf '\\n'; cat $t/stack 2>/dev/null; done"),
        ])
    else:
        commands.append(("process", select +
         "printf 'HF3FS_LIVE_PROCESS pid=%s comm=%s\\n' $pid $comm; "
         "printf 'wchan='; cat $p/wchan 2>/dev/null; printf '\\nsyscall='; "
         "cat $p/syscall 2>/dev/null; printf '\\n'; "
         "grep -E '^(State|Threads|Cpus_allowed_list|voluntary_ctxt_switches|nonvoluntary_ctxt_switches):' "
         "$p/status 2>/dev/null; cat $p/io 2>/dev/null; done"))
        commands.append(("threads", select +
         "case $comm in io500|io500-linebuf|mpiexec.hydra|hydra_pmi_proxy) ;; *) continue;; esac; "
         "for t in $p/task/[0-9]*; do tid=${t##*/}; printf 'HF3FS_LIVE_THREAD pid=%s tid=%s comm=' $pid $tid; "
         "cat $t/comm 2>/dev/null; printf 'state='; grep '^State:' $t/status 2>/dev/null; "
         "printf 'wchan='; cat $t/wchan 2>/dev/null; printf '\\nsyscall='; cat $t/syscall 2>/dev/null; "
         "printf '\\n'; done; done"))
    commands.append(("logs", f"printf 'HF3FS_LIVE_LOGS\\n'; for log in {logs}; do [ -f $log ]||continue; "
                     "printf 'HF3FS_LIVE_LOG %s\\n' $log; tail -c 16384 $log | tail -80; done"))
    commands.append(
        ("fuse", "printf 'HF3FS_LIVE_FUSE\\n'; for c in /sys/fs/fuse/connections/*; do "
         "[ -d $c ]||continue; printf 'connection=%s waiting=' ${c##*/}; cat $c/waiting 2>/dev/null; done"))
    if any(len(command) >= 800 for _, command in commands):
        raise ValueError("live diagnostic command exceeds the serial input budget")
    return commands


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
                  region_length: int = REGION_LENGTH, lane_count: int = 128, cohort_config: bool = False,
                  polling: CxlPolling | None = None) -> dict:
    cohort = cohort_config or clients == 10
    if not 1 <= clients <= 10 or (not cohort_config and clients not in (1, 10)) or not 0 <= node <= clients or region_length % (1024 * 1024):
        raise ValueError("invalid full-stack cohort or DAX geometry")
    directory = root / CONFIG.removeprefix("/")
    directory.mkdir(parents=True, exist_ok=False)
    # Preserve the cohort baseline; the 100 ms diagnostic experiment did not
    # eliminate FDB starvation or pass the sustained finite I/O workload.
    poll_sleep = "10ms" if cohort else "1ms"
    polling = polling or CxlPolling(spin="0us", yields=0, sleep=poll_sleep)
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
                                + render_polling(polling))
        # TCG needs longer request deadlines; geometry and wire framing stay fixed.
        parents = re.findall(r"^\[([^\[\]\n]+)\.io_worker\.cxlsocket\]$", text, re.MULTILINE)
        read_in_event_loop = cohort and source.name == "hf3fs_fuse_main.toml"
        text = re.sub(r"^\[([^\[\]\n]+)\.io_worker\.cxlsocket\]$",
                      lambda match: f"[{match[1]}.io_worker]\ndata_connect_timeout = '120s'\n"
                                    f"tcp_connect_timeout = '30s'\n"
                                    f"read_write_data_in_event_thread = "
                                    f"{'true' if read_in_event_loop else 'false'}\n{match[0]}",
                      text, flags=re.MULTILINE)
        for parent in parents:
            if parent.endswith("groups"):
                continue
            response_threads = 2 if read_in_event_loop else 1
            text += f"\n[{parent}]\ndefault_timeout = '120s'\n"
            text += (f"\n[{parent}.thread_pool]\nnum_proc_threads = {response_threads}\n"
                     f"num_io_threads = {response_threads}\n"
                     "num_bg_threads = 1\nnum_connect_threads = 1\n")
        # StorageMessenger supplies an explicit timeout from RetryConfig, so
        # net_client.default_timeout alone does not change storage RPC waits.
        for parent in sorted({p.removesuffix(".net_client") for p in parents if p.endswith(".net_client")}):
            text += (f"\n[{parent}.retry]\ninit_wait_time = '120s'\nmax_wait_time = '120s'\n"
                     "max_retry_time = '360s'\n")
        if source.name == "hf3fs_fuse_main.toml":
            text = "max_threads = 8\nnotify_inval_threads = 2\n" + text
            if cohort:
                # Ten mounts start together, so even a production-scale short
                # interval creates synchronized metadata bursts under TCG.
                # Final sync and unmount remain explicit and bounded.
                text = text.replace("interval = '500ms'", "interval = '300s'")
            # MetaClient likewise uses its own request and total retry budget.
            text += "\n[meta.retry_default]\nrpc_timeout = '120s'\nretry_total_time = '360s'\n"
        if source.name == "meta_main.toml":
            # These finite qualification runs start from empty storage and do
            # not need asynchronous physical space reclamation. Under TCG the
            # 200 ms GC scanner can starve foreground Meta/FDB requests even
            # during single-client mount readiness.
            text = text.replace("[server.meta.gc]\n", "[server.meta.gc]\n"
                                "enable = false\nscan_interval = '60s'\n")
            if cohort:
                # Under the full TCG cohort, a same-directory create batch can
                # take tens of seconds.  The production five-second deadline
                # expires while followers wait for the active parent-inode
                # batch, before their FDB transaction can start.  Keep the
                # server deadline below the client's 120-second RPC timeout.
                text = text.replace("[server.meta]\n", "[server.meta]\noperation_timeout = '90s'\n")
            # Every refresh prints the complete immutable configuration. A
            # two-second cadence can continuously occupy the emulated CPUs
            # while the service is still initializing, even with one client.
            text = text.replace("auto_extend_client_session_interval = '2s'",
                                "auto_extend_client_session_interval = '10s'")
            text = text.replace("auto_heartbeat_interval = '2s'",
                                "auto_heartbeat_interval = '10s'")
            text = text.replace("auto_refresh_interval = '2s'",
                                "auto_refresh_interval = '10s'")
        if cohort and source.name == "storage_main.toml":
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
        elif not cohort and source.name == "storage_main.toml":
            # Default Storage groups create more than two hundred threads.
            # On the four-CPU TCG guest this can prevent the management service
            # from completing the routing-info RPC needed by Storage startup.
            text = text.replace("[server.aio_read_worker]\n",
                                "[server.aio_read_worker]\nnum_threads = 2\n")
            text += ("\n[server.base.thread_pool]\nnum_proc_threads = 2\nnum_io_threads = 2\n"
                     "num_bg_threads = 1\nnum_connect_threads = 1\n"
                     "\n[server.base.independent_thread_pool]\nnum_proc_threads = 1\nnum_io_threads = 1\n"
                     "num_bg_threads = 1\nnum_connect_threads = 1\n"
                     "\n[server.storage.write_worker]\nnum_threads = 4\nbg_num_threads = 1\n"
                     "\n[server.sync_worker]\nnum_threads = 1\n"
                     "\n[server.coroutines_pool_read]\nthreads_num = 1\n"
                     "\n[server.coroutines_pool_update]\nthreads_num = 2\n"
                     "\n[server.coroutines_pool_sync]\nthreads_num = 1\n"
                     "\n[server.coroutines_pool_default]\nthreads_num = 1\n")
        elif source.name in ("mgmtd_main.toml", "meta_main.toml"):
            if cohort:
                proc_threads = io_threads = 4 if source.name == "meta_main.toml" else 2
            else:
                proc_threads = io_threads = 2 if source.name == "meta_main.toml" else 1
            text += (f"\n[server.base.thread_pool]\nnum_proc_threads = {proc_threads}\n"
                     f"num_io_threads = {io_threads}\n"
                     "num_bg_threads = 1\nnum_connect_threads = 1\n"
                     "\n[server.base.independent_thread_pool]\nnum_proc_threads = 1\nnum_io_threads = 1\n"
                     "num_bg_threads = 1\nnum_connect_threads = 1\n")
        if source.name == "mgmtd_main.toml":
            # A full standard run can keep the single-primary TCG cohort alive
            # for several hours. Keep lease validation enabled while ensuring
            # emulator scheduling cannot expire the only management service.
            text = text.replace("[server.service]\n", "[server.service]\nlease_length = '43200s'\n"
                                "heartbeat_timestamp_valid_window = '43200s'\nheartbeat_fail_interval = '43200s'\n")
        if cohort and source.name == "hf3fs_fuse_main_launcher.toml":
            text = text.replace("[mgmtd_client]\n", "[mgmtd_client]\n"
                                "auto_extend_client_session_interval = '30s'\n"
                                "auto_heartbeat_interval = '30s'\n"
                                "auto_refresh_interval = '30s'\n")
        (directory / source.name).write_text(text)
    for name, node in (("mgmtd", 1), ("meta", 50), ("storage", 10000)):
        (directory / f"{name}_main_app.toml").write_text(f"allow_empty_node_id = false\nnode_id = {node}\n")
    (directory / "token").write_text(TOKEN + "\n")
    topology = manifests.override_role_addresses(manifests.load_topology(manifests.DEFAULT_TOPOLOGY), [
        "fabric-authority=CXL://10.73.0.2:12499", "mgmtd=CXL://10.73.0.2:12501",
        "meta=CXL://10.73.0.2:12502", "storage-0=CXL://10.73.0.2:12503",
        "admin=CXL://10.73.0.2:12507", "client-0=CXL://10.73.0.3:12516",
    ])
    if cohort:
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
        f"{render_polling(polling)}"
        f"manifest_path = '{CONFIG}/manifest.json'\nauthority_owner_lock = '{CONFIG}/authority.lock'\n"
        f"authority_receipt = '{receipt}'\n")
    (directory / "chains.csv").write_text("ChainId,TargetId\n" + "".join(f"10000{i:02}001,10000{i:02}001\n" for i in range(1, 5)))
    (directory / "chain-table.csv").write_text("ChainId\n" + "".join(f"10000{i:02}001\n" for i in range(1, 5)))
    return {"manifest_sha256": hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest(),
            "manifest": manifest, "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()}}


def stage_roots(base: Path, run: Path, session: int, *, clients: int = 1,
                region_length: int = REGION_LENGTH, lane_count: int = 128, cohort_config: bool = False) -> tuple[list[Path], dict]:
    base = base.resolve(strict=True)
    if not base.is_dir():
        raise ValueError("full-stack base rootfs is not a directory")
    roots = [run / f"node{node}-staging" for node in range(clients + 1)]
    nodes = {}

    def stage(node: int) -> dict:
        root = roots[node]
        # QEMU boots the shared immutable base image and reads only these
        # generated overlay files from staging. Copying the rootfs here made
        # every run materialize one multi-gigabyte tree per guest.
        root.mkdir(parents=True)
        return render_config(root, session, clients=clients, node=node,
                             region_length=region_length, lane_count=lane_count, cohort_config=cohort_config)

    with ThreadPoolExecutor(max_workers=clients + 1) as pool:
        records = list(pool.map(stage, range(clients + 1)))
    record = records[0]
    for node, current in enumerate(records):
        if current["manifest_sha256"] != record["manifest_sha256"]:
            raise ValueError("guest manifest/configuration identities differ")
        record = current
        nodes[node] = current["files"]
    if clients > 1 or cohort_config:
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


def parse_transport_counters(output: str, node: int) -> list[dict]:
    return [dict(guest=node, **json.loads(match[1])) for match in
            re.finditer(r"(?m)^HF3FS_CXL_TRANSPORT_COUNTERS (\{[^\r\n]+\})", output)]


def capture_transport_counters(command, node: int, run: Path) -> dict:
    destination = run / f"node{node}-transport-counters.log"
    receipt = diagnostics.capture(command, node,
        f"grep -h '^HF3FS_CXL_TRANSPORT_COUNTERS ' {LOG}/*.stdout || true",
        destination, timeout=60, guest_directory=LOG)
    if receipt.get("outcome") != "passed":
        raise RuntimeError(f"transport counter export failed: {receipt.get('error')}")
    receipt["records"] = parse_transport_counters(destination.read_text(), node)
    return receipt


def application_stop_command(name: str, variable: str, node: int, failed: bool) -> str:
    if node > 0:
        if failed:
            # Unmount exits FuseMainLoop and removes its signal handlers before
            # client/CXL cleanup. A following SIGTERM would abort retirement.
            prefix = ("hf3fs_was_mounted=0; if grep -q ' /mnt/3fs ' /proc/mounts; then "
                      "hf3fs_was_mounted=1; fusermount3 -uz /mnt/3fs || true; "
                      f"elif kill -0 ${variable} 2>/dev/null; then kill ${variable}; fi; ")
            allowed = 'test "$rc" -eq 0 || test "$rc" -eq 143'
        else:
            prefix = ("hf3fs_was_mounted=0; if grep -q ' /mnt/3fs ' /proc/mounts; then "
                      "hf3fs_was_mounted=1; fusermount3 -u /mnt/3fs; "
                      f"elif kill -0 ${variable} 2>/dev/null; then kill ${variable}; fi; ")
            allowed = 'test "$rc" -eq 0 || { test $hf3fs_was_mounted -eq 0 && test "$rc" -eq 143; }'
    else:
        prefix = f"kill ${variable}; "
        allowed = 'test "$rc" -eq 0 || test "$rc" -eq 143'
    return (prefix + f"attempt=0; while kill -0 ${variable} 2>/dev/null; do "
            "attempt=$((attempt+1)); [ $attempt -lt 300 ] || break; sleep 0.1; done; "
            f"if kill -0 ${variable} 2>/dev/null; then kill -9 ${variable}; wait ${variable}; false; "
            f"else wait ${variable}; rc=$?; printf 'HF3FS_PROCESS_EXIT name={name} rc=%s\\n' $rc; {allowed}; fi")


def execute(consoles: list, run: Path, timeout: int, *, clients: int = 1,
            region_length: int = REGION_LENGTH, workload=None, prepare_workload=None,
            diagnostic_interval: float = 0, rpc_trace: bool = True, diagnostic_workload: bool = False) -> dict:
    record = dict(status="failed", region_type="Dax", region_offset=REGION_OFFSET, region_length=region_length,
                  server_guest=0, fuse_guest=1, ready=[], clean_retirement=False, retirement_errors=[], transport_records=[])
    # Live /proc and stack collection executes work inside the measured guest.
    # Opt in explicitly; disabling RPC trace alone must not be confused with it.
    record["diagnostic_policy"] = dict(rpc_trace=rpc_trace, live_server_interval_seconds=diagnostic_interval)
    commands = []
    owned = []
    unresponsive = set()
    command_lock = threading.Lock()
    # Launcher configuration and FUSE client initialization can each consume
    # the configured 360-second retry window. Keep a separate finite host
    # deadline for the larger cohort without changing RPC or lease limits.
    fuse_startup_budget = min(timeout, 720) if clients > 1 or diagnostic_workload else 120
    record["fuse_startup_budget_seconds"] = fuse_startup_budget
    service_startup_budget = min(timeout, 360)
    record["service_startup_budget_seconds"] = service_startup_budget

    def command(node: int, text: str, deadline: int | None = None, *, mark_unresponsive: bool = True) -> str:
        if node in unresponsive:
            raise RuntimeError(f"guest {node} console is unresponsive after a command timeout")
        started = time.monotonic_ns()
        with command_lock:
            sequence = len(commands)
            entry = {"node": node, "command": text, "log": str(run / f"full-command-{sequence:03}.log"),
                     "host_start_ns": started, "host_realtime_start_ns": time.time_ns(),
                     "outcome": "running"}
            commands.append(entry)
            (run / "full-commands.json").write_text(json.dumps(commands, indent=2) + "\n")
        try:
            output = consoles[node].shell_command(text, deadline or timeout)
        except Exception as error:
            if isinstance(error, TimeoutError) and mark_unresponsive:
                with command_lock:
                    unresponsive.add(node)
                    record["unresponsive_guests"] = sorted(unresponsive)
            Path(entry["log"]).write_text(str(error))
            with command_lock:
                ended = time.monotonic_ns()
                entry.update(host_end_ns=ended, host_realtime_end_ns=time.time_ns(),
                             duration_ms=(ended - started) / 1e6, outcome="failed",
                             error_type=type(error).__name__, error=str(error))
                (run / "full-commands.json").write_text(json.dumps(commands, indent=2) + "\n")
            raise
        Path(entry["log"]).write_text(output)
        with command_lock:
            ended = time.monotonic_ns()
            entry.update(host_end_ns=ended, host_realtime_end_ns=time.time_ns(),
                         duration_ms=(ended - started) / 1e6, outcome="passed",
                         output_bytes=len(output.encode()))
            for counter in parse_transport_counters(output, node):
                if counter not in record["transport_records"]:
                    record["transport_records"].append(counter)
            (run / "full-commands.json").write_text(json.dumps(commands, indent=2) + "\n")
        return output

    command.diagnostic = lambda node, text, deadline: command(node, text, deadline, mark_unresponsive=False)

    jobs = guest_jobs.GuestJobs(consoles, command, record.setdefault("workload_jobs", []), timeout)
    command.workload = jobs.run

    def start(name: str, node: int, arguments: str) -> None:
        variable = "hf3fs_pid_" + name.replace("-", "_")
        affinity = f"/bin/busybox taskset {SERVER_CPU_MASKS[name]} " if node == 0 and name in SERVER_CPU_MASKS else ""
        environment = ""
        if name == "meta_main":
            environment = "/bin/busybox env HF3FS_FDB_FUTURE_TRACE=1 HF3FS_RPC_TRACE=1 "
        elif name == "hf3fs_fuse_main":
            environment = "/bin/busybox env HF3FS_RPC_TRACE=1 "
        if rpc_trace and name in ("cxl-fabricd", "meta_main", "hf3fs_fuse_main"):
            environment = "/bin/busybox env HF3FS_RPC_TRACE_DIR=/var/log/3fs "
        elif not rpc_trace:
            environment = ""
        if diagnostic_workload and name == "hf3fs_fuse_main":
            command(node, "mkdir -p /run/hf3fs-diagnostic")
            environment = "/bin/busybox env " + ("HF3FS_RPC_TRACE_DIR=/var/log/3fs " if rpc_trace else "")
            environment += "HF3FS_CXL_DIAGNOSTIC_DIR=/run/hf3fs-diagnostic "
        command(node, f"{affinity}{environment}{BIN}/{name} {arguments} >{LOG}/{name}.stdout 2>{LOG}/{name}.stderr & {variable}=$!")
        owned.append((name, node, variable))

    def port_ready(port: int, variable: str, name: str) -> None:
        ready = f"grep -q 'Start server finished' {LOG}/{name}.log && nc -z -w 1 10.73.0.2 {port}"
        milestones = ("Server config inited|Start to init server|Init server finished|"
                      "Start to start server|MetaOperator::init|GcManager::init|"
                      "Listener setup|Start server finished")
        command(0, f"attempt=0; until {ready}; do "
                f"kill -0 ${variable} || break; attempt=$((attempt+1)); "
                f"if [ $((attempt%30)) -eq 0 ]; then hf3fs_log_ready=0; "
                f"grep -q 'Start server finished' {LOG}/{name}.log && hf3fs_log_ready=1; "
                f"hf3fs_port_ready=0; nc -z -w 1 10.73.0.2 {port} && hf3fs_port_ready=1; "
                f"printf 'HF3FS_SERVICE_WAIT name={name} attempt=%s log_ready=%s port_ready=%s log_bytes=' "
                "$attempt $hf3fs_log_ready $hf3fs_port_ready; "
                f"wc -c <{LOG}/{name}.log 2>/dev/null || echo 0; "
                f"tail -c 262144 {LOG}/{name}.log 2>/dev/null | "
                f"grep -E '{milestones}' | tail -1 || true; fi; "
                f"[ $attempt -lt {service_startup_budget} ] || break; sleep 1; done; "
                f"if kill -0 ${variable} && {ready}; then true; else ( "
                f"printf 'HF3FS_SERVICE_STARTUP_MILESTONES name={name}\\n'; "
                f"tail -c 1048576 {LOG}/{name}.log 2>/dev/null | "
                f"grep -E '{milestones}' | tail -40 || true; "
                f"printf 'HF3FS_SERVICE_STARTUP_THREADS name={name}\\n'; "
                f"for t in /proc/${variable}/task/[0-9]*; do [ -r $t/status ] || continue; "
                "printf 'tid=%s comm=' ${t##*/}; cat $t/comm 2>/dev/null; "
                "printf 'state='; grep '^State:' $t/status 2>/dev/null; "
                "printf 'wchan='; cat $t/wchan 2>/dev/null; printf '\\n'; done; "
                f"tail -100 {LOG}/{name}_main.stderr {LOG}/{name}.log ) >{LOG}/startup-{name}.diagnostic 2>&1; "
                f"head -c 2048 {LOG}/startup-{name}.diagnostic; false; fi",
                timeout)

    def admin(arguments: str) -> str:
        return command(0, f"{BIN}/admin_cli --cfg {CONFIG}/admin_cli.toml -- {arguments}")

    def run_with_server_diagnostics(operation, phase: str):
        """Sample the server while one aggregate client operation is blocked."""
        if diagnostic_interval <= 0:
            return operation()
        selected = {"system", "service_events", "rpc_meta", "process", "meta_worker_stacks"}
        started = time.monotonic_ns()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(operation)
            sample_number = 0
            while True:
                done, _ = wait((future,), timeout=diagnostic_interval)
                if done:
                    return future.result()
                sample_number += 1
                sample = {
                    "phase": phase,
                    "sample": sample_number,
                    "blocked_guest": None,
                    "elapsed_ms": (time.monotonic_ns() - started) / 1e6,
                    "sections": [],
                }
                for name, diagnostic in live_diagnostic_commands(0, phase):
                    if name not in selected:
                        continue
                    if future.done():
                        break
                    section = diagnostics.capture(
                        lambda node, text, deadline: command(node, text, deadline, mark_unresponsive=False),
                        0, diagnostic, run / f"live-{phase}-{sample_number}-{name}.log")
                    section["name"] = name
                    sample["sections"].append(section)
                    if section["outcome"] == "failed":
                        break
                with command_lock:
                    record.setdefault("live_diagnostics", []).append(sample)

    def command_with_server_diagnostics(node: int, text: str, deadline: int, phase: str) -> str:
        return run_with_server_diagnostics(lambda: command(node, text, deadline), phase)

    def capture_failure_logs() -> None:
        """Preserve bounded causal logs before shutdown messages displace them."""
        snapshots = []

        def capture(node: int) -> None:
            try:
                # Keep each shell line below the guest serial input limit and
                # keep diagnostic failures independent from teardown state.
                selected = {"service_events", "rpc_meta", "process"} if node == 0 else {"process", "logs"}
                for name, diagnostic in live_diagnostic_commands(node, "failure"):
                    if name in selected:
                        section = diagnostics.capture(
                            lambda n, text, deadline: command(n, text, deadline, mark_unresponsive=False),
                            node, diagnostic, run / f"failure-node{node}-{name}.log")
                        with command_lock:
                            record.setdefault("failure_diagnostics", []).append(dict(name=name, **section))
                        if section["outcome"] != "passed":
                            raise RuntimeError(section["error"])
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
        if 1 <= clients <= 10:
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
        def warm_server_route(node: int) -> None:
            command(node, "attempt=0; until /bin/busybox ping -c 1 -W 1 10.73.0.2 >/dev/null 2>&1; do "
                    "attempt=$((attempt+1)); [ $attempt -lt 60 ] || break; sleep 1; done; "
                    "if /bin/busybox ping -c 1 -W 1 10.73.0.2 >/dev/null 2>&1; then "
                    "printf 'HF3FS_BOOTSTRAP_NETWORK_READY peer=10.73.0.2 attempts=%s\\n' $attempt; "
                    "else ip addr show dev eth0; ip route; ip neigh; false; fi", min(timeout, 180))

        if clients:
            try:
                with ThreadPoolExecutor(max_workers=clients) as pool:
                    list(pool.map(warm_server_route, range(1, clients + 1)))
            except Exception:
                snapshots = []
                for node in range(clients + 1):
                    diagnostic = (f"printf 'HF3FS_NETWORK_FAILURE node={node}\\n'; "
                                  "ip addr show dev eth0; ip route; ip neigh")
                    try:
                        output = command(node, diagnostic, 30, mark_unresponsive=False)
                        snapshots.append({"guest": node, "output_bytes": len(output.encode())})
                    except Exception as error:
                        snapshots.append({"guest": node, "error": str(error)})
                record["network_failure_diagnostics"] = snapshots
                raise
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
        # Meta's initial filesystem check needs FDB but does not need Storage.
        # Start it before Storage so the RISC-V TCG guest does not contend with
        # Storage's worker cohort while bringing up its first FDB transaction.
        for name, port in (("mgmtd", 12501), ("meta", 12502), ("storage", 12503)):
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
                fuse_startup_budget if clients > 1 or diagnostic_workload else None)

        with ThreadPoolExecutor(max_workers=clients) as pool:
            list(pool.map(wait_fuse, range(1, clients + 1)))
        record["ready"].append("hf3fs_fuse_main")
        if clients == 1 and workload is None:
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
                record["clients"] = run_with_server_diagnostics(
                    lambda: concurrent_io(consoles, command, clients), "qualification")
            else:
                record["workload"] = workload(consoles, command, run)
                if record["workload"].get("status") != "passed":
                    raise RuntimeError(record["workload"].get("first_failure", "workload failed"))
            record["cpu_affinity"]["after_io"] = parse_cpu_affinity(command(0, cpu_affinity_command()))
    except Exception as error:
        record["first_failure"] = str(error)
    finally:
        for job in record["workload_jobs"]:
            try:
                jobs.stop(job)
            except Exception as error:
                record["retirement_errors"].append(dict(guest=job["guest"], process="workload", error=str(error)))
        if "first_failure" in record:
            capture_failure_logs()
        # FDB is owned by the outer G0 runner and stays alive through this cleanup.
        for name in ("hf3fs_fuse_main", "meta_main", "storage_main", "mgmtd_main", "cxl-fabricd"):
            matching = [item for item in owned if item[0] == name]

            def stop_owned(item) -> None:
                owned_name, node, variable = item
                try:
                    command(node, application_stop_command(name, variable, node, "first_failure" in record), 180)
                except Exception as error:
                    with command_lock:
                        record["retirement_errors"].append(dict(guest=node, process=owned_name, error=str(error)))
                        record.setdefault("first_failure", f"application teardown: {error}")

            with ThreadPoolExecutor(max_workers=max(1, len(matching))) as pool:
                list(pool.map(stop_owned, matching))
        for job in record["workload_jobs"]:
            try:
                jobs.verify_stopped(job)
            except Exception as error:
                record["retirement_errors"].append(dict(guest=job["guest"], process="workload", error=str(error)))
        try:
            command(0, f"grep HF3FS_CXL_FABRIC_RETIRED {LOG}/cxl-fabricd.stdout")
            with ThreadPoolExecutor(max_workers=clients) as pool:
                list(pool.map(lambda node: command(node, "! grep -q ' /mnt/3fs ' /proc/mounts"),
                              range(1, clients + 1)))
            record["clean_retirement"] = not record["retirement_errors"]
        except Exception as error:
            record["retirement_errors"].append(dict(stage="retirement_markers", error=str(error)))
            record.setdefault("first_failure", str(error))

        def collect(node: int) -> None:
            try:
                receipt = capture_transport_counters(command.diagnostic, node, run)
                with command_lock:
                    record.setdefault("counter_logs", []).append({k: v for k, v in receipt.items() if k != "records"})
                    for counter in receipt["records"]:
                        if counter not in record["transport_records"]:
                            record["transport_records"].append(counter)
            except Exception as error:
                with command_lock:
                    record.setdefault("collection_errors", []).append(dict(guest=node, error=str(error)))
                    record.setdefault("first_failure", f"counter collection: {error}")
            if node == 0:
                try:
                    listing = command(node, f"for f in {LOG}/startup-*.diagnostic; do [ -f $f ] || continue; "
                                      "printf 'HF3FS_STARTUP_FILE %s\\n' $f; done", 30)
                    for path in re.findall(r"(?m)^HF3FS_STARTUP_FILE (/var/log/3fs/startup-[a-z]+\.diagnostic)\r*$", listing):
                        exported = diagnostics.export_file(command, node, path, run / Path(path).name)
                        with command_lock:
                            record.setdefault("startup_diagnostics", []).append(exported)
                except Exception as error:
                    with command_lock:
                        record.setdefault("collection_errors", []).append(dict(guest=node, kind="startup", error=str(error)))
            if rpc_trace:
                try:
                    listing = command(node, f"for f in {LOG}/rpc-*.trace; do [ -f $f ] || continue; "
                                      "printf 'HF3FS_TRACE_FILE %s\\n' $f; done", 30)
                    paths = re.findall(r"(?m)^HF3FS_TRACE_FILE (/var/log/3fs/rpc-[0-9]+\.trace)\r*$", listing)
                    if node in range(1, clients + 1) or any(name == "meta_main" for name, _, _ in owned):
                        if not paths:
                            raise ValueError("expected process request trace is missing")
                    for path in paths:
                        try:
                            destination = run / f"node{node}-{Path(path).name}"
                            exported = diagnostics.export_file(command, node, path, destination,
                                                               max_bytes=4 * 1024 * 1024 + 256)
                            completeness = diagnostics.validate_rpc_trace(destination)
                            with command_lock:
                                record.setdefault("request_traces", []).append({"guest": node, **exported, **completeness})
                        except Exception as error:
                            with command_lock:
                                record.setdefault("collection_errors", []).append(
                                    dict(guest=node, kind="request_trace", path=path, error=str(error)))
                except Exception as error:
                    with command_lock:
                        record.setdefault("collection_errors", []).append(dict(guest=node, kind="request_trace", error=str(error)))
            if "first_failure" not in record:
                try:
                    command(node, f"( for log in {LOG}/*; do printf '\\nHF3FS_LOG_FILE %s\\n' \"$log\"; "
                            "tail -30 \"$log\"; done ) | head -c 3072", 30)
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
        output = getattr(command, "workload", command)(node,
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
            output = getattr(command, "workload", command)(reader, f"dd if=/mnt/3fs/test/client{record['guest']}.bin "
                             "of=/var/lib/3fs/neighbor bs=65536 iflag=direct && sha256sum /var/lib/3fs/neighbor")
            hashes = re.findall(r"(?m)^([0-9a-f]{64})\s+/var/lib/3fs/neighbor\s*$", output)
            if hashes != [record["sha256"]]:
                raise ValueError(f"client {reader} cannot verify its neighbor's completed file")
            record.update(neighbor_guest=reader, neighbor_sha256=hashes[0])
        list(pool.map(neighbor, results))
    return results
