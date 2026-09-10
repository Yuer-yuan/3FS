#!/usr/bin/env python3
"""Finite 10-client/1-server CXL qualification on the existing RISC-V platform."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import uuid

import full_stack
import guest_image
import io500_standard
import minimal_requests
import starvation_probe
import scaling_probe
import prepare_io500
import run_g0 as g0
import stage_full_rootfs

SCHEMA = "hf3fs.cxl-phase1-10c1s.v1"
CLIENTS = 10
CAPACITY = 2 * 1024**3
REGION_LENGTH = CAPACITY - full_stack.REGION_OFFSET
LANES = 800  # endpointCount 25 * 32 lanes, including normal/update clients.
DAX_PROBE_PAYLOAD_BYTES = 64 * 1024 - 64
COUNTERS = (
    "rdma_open_attempts", "tcp_receive_bytes", "tcp_data_plane_bytes", "core_tcp_bytes",
    "bootstrap_control_bytes", "bootstrap_serving_bytes", "cxl_rpc_requests", "cxl_rpc_responses",
    "cxl_rpc_bytes", "cxl_bulk_read_bytes", "cxl_bulk_write_bytes",
)


def rootfs_image_identity(root: Path, record: dict, image_bytes: int) -> tuple[str, dict]:
    return guest_image.rootfs_image_identity(root, record, image_bytes)


def network_args(node: int, group: str, port: int) -> list[str]:
    if not 0 <= node <= CLIENTS or not group.startswith("239.73.") or not 0 < port < 65536:
        raise ValueError("invalid run-owned multicast identity")
    socket.inet_aton(group)
    return ["-netdev", f"socket,id=control,mcast={group}:{port},localaddr=127.0.0.1",
            "-device", f"virtio-net-pci,netdev=control,bus=pcie.0,mac=52:54:00:73:00:{node + 2:02x}"]


def verify_reference(reference_path: Path, rootfs_record: dict, artifacts: dict) -> dict:
    reference = json.loads(reference_path.read_text())
    if reference.get("schema") != "hf3fs.cxl-riscv-g0-b-handoff.v1":
        raise ValueError("unsupported G0-B reference")
    path = Path(reference["result_path"]).resolve(strict=True)
    if g0.sha256_file(path) != reference.get("result_sha256"):
        raise ValueError("G0-B result changed")
    result = json.loads(path.read_text())
    if result.get("status") != "passed" or result.get("first_failure") or g0.validate_result(result, "full-3fs"):
        raise ValueError("G0-B did not pass")
    for field in ("build_manifest_sha256", "source_closure_sha256"):
        if result.get(field) != reference.get(field):
            raise ValueError("G0-B handoff identity mismatch: " + field)
    if result.get("platform_contract_sha256") != reference.get("platform_contract_sha256"):
        raise ValueError("G0-B platform contract identity mismatch")
    for name, artifact in artifacts.items():
        if artifact["sha256"] != result["platform_artifacts"][name]["sha256"]:
            raise ValueError("G0-B platform artifact changed: " + name)
    # G0-B proves the platform and 3FS application stack. Benchmark payloads
    # have a separate pinned manifest and are verified below for standard runs;
    # changing an IO500-only helper must not invalidate the platform proof.
    for name, artifact in rootfs_record["binaries"].items():
        if name.startswith("io500/"):
            continue
        if artifact.get("sha256") != result["binaries"].get(name, {}).get("sha256"):
            raise ValueError("G0-B staged binary changed: " + name)
    return dict(
        reference=reference,
        reference_sha256=g0.sha256_file(reference_path),
        verified=True,
        compatibility="exact-platform-and-staged-3fs-binary-hashes",
        build_manifest_sha256=rootfs_record["build_manifest_sha256"],
        source_closure_sha256=rootfs_record["source_closure_sha256"],
        g0_build_manifest_sha256=result["build_manifest_sha256"],
        g0_source_closure_sha256=result["source_closure_sha256"],
    )


def validate_cohort_result(result: dict) -> list[str]:
    errors = []
    if result.get("first_failure"):
        errors.append("run has a first failure")
    reference = result.get("g0_b", {})
    if reference.get("verified") is not True or any(
        reference.get(field) != result.get(field) for field in ("build_manifest_sha256", "source_closure_sha256")
    ):
        errors.append("G0-B artifact identity mismatch")
    if result.get("capacity") != CAPACITY or result.get("lane_count") != LANES:
        errors.append("10c1s capacity/lane contract mismatch")
    guests = result.get("guests", [])
    if len(guests) != 11 or {g.get("host_id") for g in guests} != set(range(11)):
        errors.append("10c1s requires eleven independent QEMU guests")
    elif len({tuple(g.get("backing_inode", [])) for g in guests}) != 11:
        errors.append("guest CXL backing files are not all distinct")
    if any(g.get("readiness", {}).get("size") != CAPACITY or
           g.get("readiness", {}).get("device") != "/dev/dax0.0" or
           g.get("readiness", {}).get("fuse_device") is not True for g in guests):
        errors.append("guest DAX/FUSE geometry is incomplete")
    directions = result.get("dax_directions", [])
    pairs = {(d.get("writer_host"), d.get("reader_host")) for d in directions}
    expected = {(0, n) for n in range(1, 11)} | {(n, 0) for n in range(1, 11)}
    if pairs != expected or len(directions) != 20:
        errors.append("DAX publication does not cover both directions for every client")
    for d in directions:
        w, r = d.get("writer", {}), d.get("reader", {})
        if (w.get("status") != "passed" or r.get("status") != "passed" or
            w.get("checksum") != r.get("checksum") or w.get("generation") != r.get("generation") or
            w.get("bytes") != full_stack.REGION_OFFSET or r.get("bytes") != full_stack.REGION_OFFSET or
            w.get("payload_bytes") != DAX_PROBE_PAYLOAD_BYTES or
            r.get("payload_bytes") != DAX_PROBE_PAYLOAD_BYTES or
            any(p.get(a) is not True for p in (w, r) for a in ("atomic_u32_lock_free", "atomic_u64_lock_free"))):
            errors.append("DAX generation/checksum/atomic proof failed")
            break
    if result.get("coherence", {}).get("complete") is not True:
        errors.append("bidirectional dirty BI handoff evidence is incomplete")
    fdb = result.get("fdb", {})
    if (any(fdb.get(f) is not True for f in ("guest_server", "set_ok", "get_ok", "value_match")) or
        fdb.get("guest") != 0 or fdb.get("bind_address") != "127.0.0.1:4500" or fdb.get("api_version") != 710):
        errors.append("machine-local TCP FoundationDB transaction did not pass")
    if fdb.get("server_knobs") != g0.FDB_COHORT_KNOBS:
        errors.append("FoundationDB simulation budget is absent or changed")
    apps = result.get("applications", {})
    affinity = apps.get("cpu_affinity", {})
    if any(not full_stack.validate_cpu_affinity(affinity.get(stage, [])) for stage in ("before_io", "after_io")):
        errors.append("resident guest CPU affinity is absent or overlaps")
    elif ({(r["role"], r["pid"]) for r in affinity["before_io"]} !=
          {(r["role"], r["pid"]) for r in affinity["after_io"]}):
        errors.append("resident processes changed across the workload")
    if apps.get("status") != "passed" or apps.get("clean_retirement") is not True:
        errors.append("application readiness or clean retirement failed")
    if set(apps.get("ready", [])) != set(full_stack.APPLICATIONS):
        errors.append("full application cohort is not ready")
    if apps.get("region_length") != REGION_LENGTH or apps.get("region_offset") != full_stack.REGION_OFFSET:
        errors.append("application range is not isolated from the DAX probe")
    manifest = result.get("configuration", {}).get("manifest", {})
    participants = {p["endpoint"] for p in manifest.get("participants", [])}
    expected_endpoints = {1, 2, 3, 4, 7, *range(16, 26)}
    if participants != expected_endpoints:
        errors.append("manifest does not contain the exact 15-process cohort")
    records = apps.get("transport_records", [])
    totals = dict.fromkeys(COUNTERS, 0)
    seen, endpoints = set(), set()
    for r in records:
        key = (r.get("guest"), r.get("pid"), r.get("endpoint"), r.get("generation"))
        if key in seen:
            errors.append("duplicate process transport evidence")
        seen.add(key)
        endpoint = r.get("endpoint", 0)
        endpoints.add(endpoint)
        expected_guest = 0 if endpoint in {1, 2, 3, 4, 7} else endpoint - 15
        if (r.get("schema") != "hf3fs.transport-counters.v1" or r.get("clean_shutdown") is not True or
            r.get("scope") != "process-lifetime-receive" or r.get("guest") != expected_guest or
            r.get("session") != manifest.get("sessionGeneration") or
            r.get("manifest_sha256") != manifest.get("manifestSha256") or
            not isinstance(r.get("generation"), int) or r.get("generation", 0) <= 0):
            errors.append("transport identity or retirement mismatch")
        counters = r.get("counters", {})
        if any(type(counters.get(k)) is not int or counters[k] < 0 for k in COUNTERS):
            errors.append("transport counter is absent or invalid")
            continue
        for k in COUNTERS:
            totals[k] += counters[k]
        if counters["tcp_receive_bytes"] != sum(counters[k] for k in
                ("core_tcp_bytes", "bootstrap_control_bytes", "bootstrap_serving_bytes")):
            errors.append("unclassified TCP receive bytes remain")
        if endpoint >= 16 and (counters["cxl_rpc_requests"] + counters["cxl_rpc_responses"] <= 0):
            errors.append("a FUSE client lacks CXL RPC evidence")
    if endpoints != participants or not records:
        errors.append("transport evidence does not cover every endpoint")
    for endpoint in expected_endpoints:
        generations = sorted(r.get("generation", 0) for r in records if r.get("endpoint") == endpoint
                             and type(r.get("generation")) is int)
        if endpoint == 7:
            if not generations or generations != list(range(1, generations[-1] + 1)):
                errors.append("admin generation evidence has a gap or duplicate")
        elif generations != [1]:
            errors.append("resident endpoint does not have exactly one generation-1 receipt")
    for k in ("rdma_open_attempts", "tcp_data_plane_bytes", "bootstrap_serving_bytes"):
        if totals[k] != 0:
            errors.append(k + " must be zero")
    for k in ("core_tcp_bytes", "bootstrap_control_bytes", "cxl_rpc_requests", "cxl_rpc_responses",
              "cxl_rpc_bytes", "cxl_bulk_read_bytes", "cxl_bulk_write_bytes"):
        if totals[k] <= 0:
            errors.append(k + " must be positive")
    teardown = result.get("teardown", {})
    if (teardown.get("all_owned_processes_stopped") is not True or
        teardown.get("qemu_returncodes") != [0] * 11 or teardown.get("cxlmemsim_returncode") != 0):
        errors.append("owned process cleanup is incomplete or forced")
    return errors


def validate_phase1_result(result: dict) -> list[str]:
    errors = validate_cohort_result(result)
    if result.get("schema") == io500_standard.SCHEMA and result.get("result_class") == "measured":
        errors.extend(io500_standard.validate_result(result))
        return errors
    if result.get("schema") != SCHEMA or result.get("result_class") != "qualification":
        errors.append("unsupported qualification schema/class")
    clients = result.get("applications", {}).get("clients", [])
    if len(clients) != 10 or {c.get("guest") for c in clients} != set(range(1, 11)):
        errors.append("ten independent client results are required")
    else:
        starts, ends = [], []
        for c in clients:
            digest = c.get("sha256", "")
            if c.get("io") != dict(block_bytes=65536, write="direct", read="direct"):
                errors.append("client I/O profile is absent or changed")
            if (c.get("bytes") != 2097152 or len(digest) != 64 or c.get("readback_sha256") != digest or
                c.get("neighbor_sha256") != digest or c.get("neighbor_guest") != c["guest"] % 10 + 1 or
                c.get("endpoint") != c["guest"] + 15 or c.get("marker") != "HF3FS_CXL_FUSE_SMOKE_OK"):
                errors.append("client or cross-client finite FUSE verification failed")
            starts.append(c.get("begin_observed_ns", 0))
            ends.append(c.get("end_observed_ns", 0))
        if min(starts) <= 0 or max(starts) >= min(ends):
            errors.append("ten client I/O intervals did not overlap in serial observations")
    return errors


def execute(args) -> Path:
    workload = getattr(args, "workload", "qualification")
    if workload not in ("qualification", "io500-standard", "diagnostic-minimal", "diagnostic-minimal-retirement", "diagnostic-starvation", "diagnostic-scale"):
        raise ValueError("unsupported phase-1 workload")
    diagnostic_clients = getattr(args, "diagnostic_clients", None)
    if diagnostic_clients is not None and workload != "diagnostic-scale":
        raise ValueError("client-count override is only allowed for diagnostic-scale")
    fdb_cpus = getattr(args, "diagnostic_fdb_cpus", "0")
    if fdb_cpus not in ("0", "0-1") or (workload != "diagnostic-scale" and fdb_cpus != "0"):
        raise ValueError("FDB affinity override is only allowed for diagnostic-scale")
    clients = diagnostic_clients if diagnostic_clients is not None else CLIENTS
    if not 1 <= clients <= CLIENTS:
        raise ValueError("diagnostic client count must be between 1 and 10")
    if workload == "diagnostic-scale" and args.smp != 5:
        raise ValueError("diagnostic-scale preserves the existing five-hart machine configuration")
    standard = workload == "io500-standard"
    paths = g0.PlatformPaths(*(getattr(args, name).resolve(strict=True) for name in
                             ("qemu", "cxlmemsim_server", "topology", "opensbi", "uboot", "kernel")))
    artifacts = g0._require_paths(paths)
    root, rootfs = g0._load_rootfs_manifest(args.rootfs_manifest)
    for name, artifact in rootfs["binaries"].items():
        path = Path(artifact["path"]).resolve(strict=True)
        if not path.is_relative_to(root) or g0.sha256_file(path) != artifact.get("sha256"):
            raise ValueError("staged binary changed: " + name)
    stage_full_rootfs.verify_build(Path(rootfs["build_manifest"]), Path(rootfs["source_closure"]))
    reference = verify_reference(args.g0_b, rootfs, artifacts)
    if standard:
        bundle = prepare_io500.verify(root / "opt/io500/manifest.json")
        binding = rootfs.get("io500_bundle", {})
        if (not binding or binding.get("record") != bundle or
            binding.get("manifest_sha256") != g0.sha256_file(root / "opt/io500/manifest.json")):
            raise ValueError("rootfs benchmark bundle identity mismatch")
    if guest_image.validate_kernel_config(args.kernel_config):
        raise ValueError("kernel configuration did not pass")
    output = args.output_root.resolve()
    if not output.is_relative_to(g0.PROJECT_ROOT.resolve() / "out/cxl-riscv/phase1"):
        raise ValueError("output must be under this 3FS checkout's out/cxl-riscv/phase1")
    output.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    run = output / (dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{os.getpid()}")
    run.mkdir(mode=0o700)
    result_path = run / "result.json"
    config = g0.RuntimeConfig(args.guest_memory, args.timeout, args.smp)
    result = dict(schema=io500_standard.SCHEMA if standard else SCHEMA,
                  result_class="measured" if standard else ("diagnostic" if workload.startswith("diagnostic-") else "qualification"), workload=workload,
                  status="failed", first_failure=None, client_count=clients,
                  owner_token=token, g0_b=reference, platform_artifacts=artifacts, capacity=CAPACITY,
                  lane_count=LANES, binaries=rootfs["binaries"], qemu_commands=[], guests=[], dax_directions=[],
                  orchestration_seconds={},
                  build_manifest_sha256=rootfs["build_manifest_sha256"],
                  source_closure_sha256=rootfs["source_closure_sha256"])
    if standard:
        result["io500_bundle"] = rootfs["io500_bundle"]
    def persist():
        g0._atomic_write_json(result_path, result)
    persist()
    consoles, readiness, backings = [], [], []
    server, server_log = None, None
    reservation = g0.PortReservation()
    network = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    network.bind(("127.0.0.1", 0))
    network_port = network.getsockname()[1]
    group = f"239.73.{int(token[:2], 16)}.{int(token[2:4], 16)}"
    trace = None
    fdb_started = False
    try:
        step_start = time.monotonic()
        roots, result["configuration"] = full_stack.stage_roots(
            root, run, time.time_ns(), clients=clients, region_length=REGION_LENGTH, lane_count=LANES, cohort_config=True)
        if standard:
            result["io500_configuration"] = io500_standard.stage_roots(roots)
        result["orchestration_seconds"]["stage_roots"] = round(time.monotonic() - step_start, 6)
        config_images = [run / f"node{node}-config.ext4" for node in range(clients + 1)]
        backings = [run / f"node{node}-cxl.raw" for node in range(clients + 1)]
        lsas = [run / f"node{node}-lsa.raw" for node in range(clients + 1)]

        rootfs_manifest_sha256 = g0.sha256_file(args.rootfs_manifest)
        cache_key, cache_content = rootfs_image_identity(root, rootfs, args.image_bytes)
        cache_directory = g0.PROJECT_ROOT.resolve() / "out/cxl-riscv/image-cache"
        base_image = cache_directory / f"{cache_key}.ext4"
        cache_start = time.monotonic()
        base_record, cache_hit = guest_image.ensure_cached_image(
            rootfs=root, output=base_image, image_bytes=args.image_bytes,
            truncate=Path("/usr/bin/truncate"), mkfs_ext4=Path("/usr/sbin/mkfs.ext4"),
            e2fsck=Path("/usr/sbin/e2fsck"))
        result["orchestration_seconds"]["base_image_cache"] = round(
            time.monotonic() - cache_start, 6)
        result["image_cache"] = {
            "schema": "hf3fs.cxl-riscv-image-cache.v1", "key": cache_key,
            "cache_hit": cache_hit, "image": str(base_image),
            "sha256": base_record["sha256"], "bytes": base_record["bytes"],
            "rootfs_manifest_sha256": rootfs_manifest_sha256,
            "content": cache_content,
            "base_receipt_before": guest_image.immutable_image_receipt(base_image),
            "root_policy": "immutable-shared-base-with-qemu-temporary-snapshot",
        }

        def node_patches(node: int) -> dict[str, Path]:
            patches = {
                f"{full_stack.CONFIG}/{name}": roots[node] / full_stack.CONFIG.removeprefix("/") / name
                for name in result["configuration"]["guest_files"][node]
            }
            if standard:
                patches.update({
                    "/" + relative: roots[node] / relative
                    for relative in result["io500_configuration"]["guest_files"][node]
                })
            expected = {
                **{f"{full_stack.CONFIG}/{name}": digest for name, digest in
                   result["configuration"]["guest_files"][node].items()},
                **({"/" + relative: digest for relative, digest in
                    result["io500_configuration"]["guest_files"][node].items()} if standard else {}),
            }
            if any(g0.sha256_file(source) != expected[path] for path, source in patches.items()):
                raise ValueError(f"node {node} staged image patch changed")
            return patches

        def create_node_storage(node: int) -> None:
            config_image, backing, lsa = config_images[node], backings[node], lsas[node]
            guest_image.create_config_image(
                output=config_image, patches=node_patches(node), image_bytes=16 * 1024**2,
                truncate=Path("/usr/bin/truncate"), mkfs_ext4=Path("/usr/sbin/mkfs.ext4"),
                e2fsck=Path("/usr/sbin/e2fsck"))
            g0._create_sparse(backing, CAPACITY, 0x31 + node)
            g0._create_sparse(lsa, g0.LSA_CAPACITY, 0x71 + node)

        step_start = time.monotonic()
        with ThreadPoolExecutor(max_workers=clients + 1) as pool:
            list(pool.map(create_node_storage, range(clients + 1)))
        result["image_cache"]["config_images"] = [
            str(image.with_suffix(image.suffix + ".manifest.json")) for image in config_images]
        central = run / "cxl-authority.raw"
        g0._create_sparse(central, CAPACITY, 0xa5)
        result["orchestration_seconds"]["create_images"] = round(time.monotonic() - step_start, 6)
        server_command = g0.build_server_command(paths, coherence_port=reservation.port, trace=None,
                                                backing=central, capacity=CAPACITY)
        result["platform_contract"] = dict(server_command=server_command, hpa_base=g0.CXL_HPA_BASE,
            capacity=CAPACITY, backing_policy="all-distinct", guest_count=clients + 1,
            guest_memory=config.guest_memory, smp=config.smp, image_bytes=args.image_bytes,
            root_disk_policy="shared-immutable-base-qemu-snapshot",
            node_config_disk_bytes=16 * 1024**2,
            trace_policy="matching-g0-full-trace-plus-phase-final-aggregate-stats",
            multicast_group=group, multicast_port=network_port, multicast_interface="127.0.0.1")
        server_log = (run / "cxlmemsim.log").open("w")
        reservation.release()
        env = g0._qemu_environment(paths)
        env["HF3FS_CXL_RUN_OWNER"] = token
        server = subprocess.Popen(server_command, stdout=server_log, stderr=subprocess.STDOUT,
                                  start_new_session=True, env=env)
        result["cxlmemsim_pid"] = server.pid
        persist()
        g0._wait_for_log(run / "cxlmemsim.log", "Server listening on TCP port", server, args.timeout)
        network.close()
        for node in range(clients + 1):
            command = g0.build_qemu_command(paths, config, node=node, guest_count=clients + 1, capacity=CAPACITY,
                        coherence_port=reservation.port, root_image=base_image, root_snapshot=True,
                        config_image=config_images[node], endpoint_memory=backings[node], lsa=lsas[node],
                        server_read_exclusive=False)
            command += network_args(node, group, network_port)
            result["qemu_commands"].append(command)
            console = g0.GuestConsole(command, run / f"node{node}-console.log", env)
            consoles.append(console)
            result.setdefault("owned_qemu_pids", []).append(console.process.pid)
            persist()
        step_start = time.monotonic()
        with ThreadPoolExecutor(max_workers=clients + 1) as pool:
            readiness = list(pool.map(
                lambda item: g0._boot_guest(item[1], paths, item[0], args.timeout, CAPACITY,
                                            require_config_disk=True),
                enumerate(consoles)))
        result["orchestration_seconds"]["boot_guests"] = round(time.monotonic() - step_start, 6)
        length = readiness[0]["alignment"]
        if any(r["alignment"] != length or r["size"] != CAPACITY for r in readiness) or length > full_stack.REGION_OFFSET:
            raise ValueError("guest DAX geometries differ or overlap the application range")
        step_start = time.monotonic()
        for client in range(1, clients + 1):
            for writer, reader in ((0, client), (client, 0)):
                generation = len(result["dax_directions"]) + 1
                records = {}
                for role, node in (("writer", writer), ("reader", reader)):
                    text = consoles[node].shell_command(
                        f"/opt/3fs/bin/cxl_dax_smoke --device /dev/dax0.0 --offset 0 --length {length} "
                        f"--payload-bytes {DAX_PROBE_PAYLOAD_BYTES} "
                        f"--role {role} --generation {generation} --timeout-ms 30000", args.timeout)
                    records[role] = g0._require_probe_record(text, f"DAX {role} guest {node}")
                result["dax_directions"].append(dict(writer_host=writer, reader_host=reader, **records))
                persist()
        result["orchestration_seconds"]["dax_handoffs"] = round(time.monotonic() - step_start, 6)
        if standard:
            result["clock_initialization"] = io500_standard.clocks(consoles, synchronize=True)
            persist()
        consoles[0].shell_command("mkdir -p /var/lib/fdb /var/log/fdb && "
            "printf '%s\\n' 'hf3fsg0:hf3fsg0@127.0.0.1:4500' >/opt/3fs/etc/fdb.cluster", args.timeout)
        consoles[0].shell_command(g0.fdb_server_command(clients=CLIENTS), args.timeout)
        fdb_started = True
        consoles[0].shell_command(g0.fdb_readiness_command(), args.timeout)
        consoles[0].shell_command("attempt=0; while [ $attempt -lt 120 ]; do "
            "/opt/foundationdb/bin/fdbcli -C /opt/3fs/etc/fdb.cluster --exec 'configure new single memory' && break; "
            "attempt=$((attempt + 1)); sleep 1; done; [ $attempt -lt 120 ]", args.timeout)
        result["fdb"] = g0._require_probe_record(consoles[0].shell_command(
            f"/opt/3fs/bin/fdb_client_smoke --cluster-file /opt/3fs/etc/fdb.cluster --key {token} --value phase1-10c1s",
            args.timeout), "local TCP FoundationDB transaction")
        result["fdb"].update(guest=0, guest_server=True, bind_address="127.0.0.1:4500",
                             server_knobs=dict(g0.FDB_COHORT_KNOBS))
        persist()
        result["applications"] = full_stack.execute(consoles, run, args.timeout, clients=clients, region_length=REGION_LENGTH,
            workload=io500_standard.execute if standard else (minimal_requests.execute if workload == "diagnostic-minimal" else
                (minimal_requests.execute_and_retire if workload == "diagnostic-minimal-retirement" else
                 (starvation_probe.execute if workload == "diagnostic-starvation" else
                  ((lambda consoles, command, run: scaling_probe.execute(consoles, command, run,
                      first_mask="3" if fdb_cpus == "0-1" else "1")) if workload == "diagnostic-scale" else None)))),
            rpc_trace=not getattr(args, "no_rpc_trace", False), diagnostic_workload=workload.startswith("diagnostic-"),
            prepare_workload=io500_standard.preflight if standard else None)
        if result["applications"].get("first_failure"):
            result["first_failure"] = result["applications"]["first_failure"]
        persist()
    except Exception as error:
        result["first_failure"] = str(error)
        persist()
    finally:
        reservation.release()
        network.close()
        if fdb_started and consoles and 0 not in result.get("applications", {}).get("unresponsive_guests", []):
            try:
                consoles[0].shell_command("fdb_pid=$(cat /run/fdbserver.pid); kill $fdb_pid; "
                    "wait $fdb_pid; rc=$?; test $rc -eq 0 || test $rc -eq 143", 30)
            except Exception as error:
                result["first_failure"] = result["first_failure"] or f"FDB teardown: {error}"
        def stop(console):
            try:
                console.stop(timeout=30)
            except Exception as error:
                result["first_failure"] = result["first_failure"] or f"guest teardown: {error}"
        with ThreadPoolExecutor(max_workers=clients + 1) as pool:
            list(pool.map(stop, reversed(consoles)))
        if server and server.poll() is None:
            try:
                os.killpg(server.pid, signal.SIGINT)
                server.wait(timeout=30)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if server.poll() is None:
                    os.killpg(server.pid, signal.SIGKILL)
                    server.wait(timeout=5)
        if server_log:
            server_log.close()
        result.get("image_cache", {})["base_receipt_after"] = (
            guest_image.immutable_image_receipt(base_image) if 'base_image' in locals() else None)
        if (result.get("image_cache", {}).get("base_receipt_before") !=
                result.get("image_cache", {}).get("base_receipt_after")):
            result["first_failure"] = result["first_failure"] or "immutable base image changed during QEMU snapshots"
        result["teardown"] = dict(all_owned_processes_stopped=all(c.process.poll() is not None for c in consoles)
            and (server is None or server.poll() is not None),
            qemu_returncodes=[c.process.returncode for c in consoles],
            cxlmemsim_returncode=server.returncode if server else None)
        result["guests"] = [c.record(n, backings[n], readiness[n] if n < len(readiness) else {})
                            for n, c in enumerate(consoles)]
        persist()
    try:
        result["coherence"] = g0.parse_coherence_stats(run / "cxlmemsim.log", clients + 1)
        result["coherence"]["dax_directions"] = [
            [item["writer_host"], item["reader_host"]] for item in result["dax_directions"]]
    except Exception as error:
        result["first_failure"] = result["first_failure"] or f"coherence validation: {error}"
    if workload.startswith("diagnostic-"):
        result["acceptance_evidence"] = False
        result["validation_errors"] = validate_cohort_result(result)
        if result.get("applications", {}).get("workload", {}).get("status") != "passed":
            result["validation_errors"].append("minimal request diagnostic did not finish")
    else:
        result["validation_errors"] = validate_phase1_result(result)
    if not result["validation_errors"]:
        result["status"] = "passed"
        result["marker"] = "HF3FS_CXL_IO500_STANDARD_OK" if standard else ("HF3FS_CXL_DIAGNOSTIC_OK" if workload == "diagnostic-minimal" else "HF3FS_CXL_10C1S_OK")
        print(result["marker"], flush=True)
    persist()
    return result_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-result", type=Path)
    parser.add_argument("--workload", choices=("qualification", "io500-standard", "diagnostic-minimal", "diagnostic-minimal-retirement", "diagnostic-starvation", "diagnostic-scale"), default="qualification")
    for name in ("qemu", "cxlmemsim-server", "topology", "opensbi", "uboot", "kernel", "kernel-config",
                 "rootfs-manifest", "g0-b", "output-root"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--diagnostic-clients", type=int, choices=range(1, 11))
    parser.add_argument("--diagnostic-fdb-cpus", choices=("0", "0-1"), default="0")
    parser.add_argument("--no-rpc-trace", action="store_true")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--guest-memory", default="4G")
    parser.add_argument("--smp", type=int, default=5)
    parser.add_argument("--image-bytes", type=int, default=8 * 1024**3)
    args = parser.parse_args()
    if args.validate_result:
        errors = validate_phase1_result(json.loads(args.validate_result.read_text()))
        print(json.dumps(dict(status="failed" if errors else "passed", errors=errors), indent=2))
        return int(bool(errors))
    for name in ("qemu", "cxlmemsim_server", "topology", "opensbi", "uboot", "kernel", "kernel_config",
                 "rootfs_manifest", "g0_b", "output_root"):
        if getattr(args, name) is None:
            parser.error("missing --" + name.replace("_", "-"))
    if args.timeout <= 0 or args.smp <= 0 or args.image_bytes <= 0:
        parser.error("timeout, smp and image bytes must be positive")
    result = execute(args)
    print(result, flush=True)
    return int(json.loads(result.read_text())["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
