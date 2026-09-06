import copy
import json
from pathlib import Path
import sys
import tempfile
import shutil
import unittest


DEPLOY_DIR = Path(__file__).parents[2] / "deploy/cxl-riscv"
sys.path.insert(0, str(DEPLOY_DIR))

import run_g0


def passing_result():
    binary = {"machine": "RISC-V", "needed": ["libc.so.6"]}
    endpoint = {
        "status": "passed",
        "generation": 1,
        "checksum": 42,
        "payload_bytes": run_g0.DAX_PROBE_PAYLOAD_BYTES,
        "atomic_u32_lock_free": True,
        "atomic_u64_lock_free": True,
    }
    return {
        "schema": run_g0.RESULT_SCHEMA,
        "binaries": {name: copy.deepcopy(binary) for name in run_g0.PLATFORM_SMOKE_BINARIES},
        "dax": {
            "guests": [
                {"host_id": 0, "backing_inode": [1, 10]},
                {"host_id": 1, "backing_inode": [1, 11]},
            ],
            "qemu_lifetimes_overlap": True,
            "directions": [
                {
                    "writer_host": 0,
                    "reader_host": 1,
                    "writer": copy.deepcopy(endpoint),
                    "reader": copy.deepcopy(endpoint),
                },
                {
                    "writer_host": 1,
                    "reader_host": 0,
                    "writer": {**endpoint, "generation": 2, "checksum": 84},
                    "reader": {**endpoint, "generation": 2, "checksum": 84},
                },
            ],
            "coherence": {
                "complete": True,
                "error_events": 0,
                "dirty_handoffs": [
                    {"owner_host": 0, "requester_host": 1},
                    {"owner_host": 1, "requester_host": 0},
                ],
            },
        },
        "fuse": {
            "lookup_seen": True,
            "open_seen": True,
            "read_seen": True,
            "unmounted": True,
        },
        "fdb": {
            "guest_server": True,
            "api_version": 710,
            "set_ok": True,
            "get_ok": True,
            "value_match": True,
        },
        "teardown": {"all_owned_processes_stopped": True},
    }


class G0EvidenceTest(unittest.TestCase):
    def test_passing_platform_smoke_result(self):
        self.assertEqual(run_g0.validate_result(passing_result()), [])

    def test_result_rejects_x86_or_ibverbs(self):
        result = passing_result()
        result["binaries"]["fdbserver"]["machine"] = "X86-64"
        result["binaries"]["fdb_client_smoke"]["needed"].append("libibverbs.so.1")
        errors = run_g0.validate_result(result)
        self.assertIn("fdbserver is not RISC-V", errors)
        self.assertIn("fdb_client_smoke links libibverbs.so.1", errors)

    def test_requires_two_distinct_overlapping_guests(self):
        result = passing_result()
        result["dax"]["guests"][1]["backing_inode"] = [1, 10]
        result["dax"]["qemu_lifetimes_overlap"] = False
        errors = run_g0.validate_result(result)
        self.assertIn("DAX guests do not have distinct backing inodes", errors)
        self.assertIn("DAX guest lifetimes did not overlap", errors)

    def test_requires_bidirectional_lock_free_peer_publication_and_bi(self):
        result = passing_result()
        result["dax"]["directions"].pop()
        result["dax"]["directions"][0]["reader"]["atomic_u64_lock_free"] = False
        result["dax"]["coherence"]["complete"] = False
        errors = run_g0.validate_result(result)
        self.assertIn("DAX peer publication is not bidirectional", errors)
        self.assertIn("DAX 32/64-bit atomics are not lock-free", errors)
        self.assertIn("DAX range has no authoritative CXLMemSim coherence evidence", errors)

    def test_requires_real_fuse_and_guest_fdb(self):
        result = passing_result()
        result["fuse"]["read_seen"] = False
        result["fdb"]["guest_server"] = False
        errors = run_g0.validate_result(result)
        self.assertIn("FUSE lookup/open/read/unmount proof is incomplete", errors)
        self.assertIn("guest FoundationDB transaction proof is incomplete", errors)

    def test_qemu_contract_pins_bi_and_separate_root_disk(self):
        paths = run_g0.PlatformPaths(*(Path(f"/platform/{name}") for name in (
            "qemu", "cxlmemsim", "topology", "opensbi", "uboot", "Image"
        )))
        command = run_g0.build_qemu_command(
            paths,
            run_g0.RuntimeConfig(),
            node=1,
            coherence_port=12345,
            root_image=Path("/run/root.ext4"),
            endpoint_memory=Path("/run/endpoint.raw"),
            lsa=Path("/run/lsa.raw"),
            root_snapshot=True,
            config_image=Path("/run/config.ext4"),
        )
        joined = " ".join(command)
        for value in (
            "cxl-fmw.0.restrictions=0x29",
            "hdm_for_passthrough=on",
            "x-256b-flit=on",
            "coherence-v2=on",
            "hdm-db=on",
            "coherence-v2-host-id=1",
            "coherence-v2-write-through=off",
            "virtio-blk-pci",
            "snapshot=on",
            "readonly=on,id=config-g0-node1",
            "drive=config-g0-node1",
        ):
            self.assertIn(value, joined)

    @unittest.skipUnless(shutil.which("zstd"), "zstd is required")
    def test_fifo_trace_is_archived_losslessly_and_analyzed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fifo = root / "trace.fifo"
            archive = root / "trace.jsonl.zst"
            capture = run_g0.CompressedTrace(fifo, archive)
            line = (json.dumps({"schema_version": 1, "monotonic_ns": 1,
                                "event": "registration"}) + "\n").encode()
            with fifo.open("wb", buffering=0) as writer:
                writer.write(line)
                capture.release_guard()
            self.assertEqual(capture.finish(), 0)
            evidence = run_g0.analyze_trace(archive, 4096, expected_directions=set())
            self.assertTrue(evidence["complete"])
            self.assertEqual(evidence["event_counts"], {"registration": 1})
            self.assertEqual(evidence["trace_encoding"], "zstd")
            self.assertFalse(fifo.exists())

    def test_final_coherence_stats_reject_errors_and_missing_registration(self):
        fields = {name: 0 for name in (
            "registrations gets getm upgrade puts putm request_fence snp_inv snp_downgrade "
            "snp_data_inv snp_data_downgrade host_fence model_acks native_acks "
            "dirty_data_completions persistence_fence_completions timeouts protocol_errors "
            "delivery_failures server_copy_failures active_bindings").split()}
        fields.update(registrations=11, gets=1, getm=1, model_acks=1, dirty_data_completions=1)
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "server.log"
            log.write_text("COHERENCE_V2_STATS_JSON " + json.dumps(fields) + "\n")
            self.assertTrue(run_g0.parse_coherence_stats(log, 11)["complete"])
            fields["timeouts"] = 1
            log.write_text("COHERENCE_V2_STATS_JSON " + json.dumps(fields) + "\n")
            evidence = run_g0.parse_coherence_stats(log, 11)
            self.assertFalse(evidence["complete"])
            self.assertEqual(evidence["error_events"], 1)

    def test_server_trace_is_optional_for_unperturbed_measurement(self):
        paths = run_g0.PlatformPaths(*(Path(f"/platform/{name}") for name in (
            "qemu", "cxlmemsim", "topology", "opensbi", "uboot", "Image")))
        command = run_g0.build_server_command(
            paths, coherence_port=12345, trace=None, backing=Path("/run/backing.raw"))
        self.assertFalse(any(item.startswith("--coherence-v2-trace=") for item in command))

    def test_full_profile_accepts_final_stats_with_bidirectional_dax_records(self):
        result = passing_result()
        result["gate"] = "G0-B"
        result["build_manifest_sha256"] = "build"
        result["source_closure_sha256"] = "source"
        result["dax"]["coherence"] = {
            "complete": True,
            "mode": "final-aggregate-stats-plus-bidirectional-dax-records",
            "error_events": 0,
            "stats": {"dirty_data_completions": 2},
        }
        import full_stack
        result["binaries"].update({
            name: {"machine": "RISC-V", "needed": ["libc.so.6"]}
            for name in full_stack.APPLICATIONS
        })
        original = full_stack.validate_evidence
        full_stack.validate_evidence = lambda evidence: []
        try:
            self.assertEqual(run_g0.validate_result(result, "full-3fs"), [])
            result["dax"]["coherence"]["stats"]["dirty_data_completions"] = 0
            self.assertIn(
                "CXLMemSim dirty BI hand-off completion is absent",
                run_g0.validate_result(result, "full-3fs"),
            )
        finally:
            full_stack.validate_evidence = original

    def test_full_profile_disables_per_event_trace(self):
        source = (DEPLOY_DIR / "run_g0.py").read_text(encoding="utf-8")
        self.assertIn('trace_enabled = profile == "platform-smoke"', source)
        self.assertIn('trace=trace_fifo if trace_enabled else None', source)

    def test_fdb_readiness_is_emulation_tolerant_and_diagnostic(self):
        command = run_g0.fdb_readiness_command()
        self.assertIn(f"-lt {run_g0.FDB_READY_ATTEMPTS}", command)
        self.assertGreaterEqual(
            run_g0.FDB_READY_ATTEMPTS * run_g0.FDB_READY_SLEEP_SECONDS,
            240,
        )
        self.assertIn(run_g0.FDB_CONSOLE_LOG, command)
        self.assertIn("HF3FS_G0_FDB_ALIVE", command)
        self.assertIn("if ! kill -0 $fdb_pid", command)
        self.assertIn("ps;", command)

    def test_fdb_server_is_bounded_for_the_two_gib_guest(self):
        command = run_g0.fdb_server_command()
        self.assertGreater(run_g0.FDB_MEMORY_LIMIT_MIB, run_g0.FDB_CACHE_MEMORY_MIB)
        self.assertLess(run_g0.FDB_MEMORY_LIMIT_MIB, 2 * 1024)
        self.assertIn(
            f"--cache-memory {run_g0.FDB_CACHE_MEMORY_MIB}MiB",
            command,
        )
        self.assertIn(
            f"--storage-memory {run_g0.FDB_STORAGE_MEMORY_MIB}MiB",
            command,
        )
        self.assertIn(run_g0.FDB_CONSOLE_LOG, command)

    def test_full_profile_uses_the_emulation_tolerant_fdb_cohort(self):
        source = (DEPLOY_DIR / "run_g0.py").read_text(encoding="utf-8")
        self.assertIn('fdb_server_command(clients=10 if profile == "full-3fs" else 1)', source)

    def test_completed_dax_and_fuse_are_recorded_before_fdb_starts(self):
        source = (DEPLOY_DIR / "run_g0.py").read_text(encoding="utf-8")
        fdb_start = source.index('cluster = "hf3fsg0:')
        self.assertLess(source.index('result["dax"] = {'), fdb_start)
        self.assertLess(source.index('result["fuse"] = fuse'), fdb_start)

    def test_trace_rejects_server_copy_failure(self):
        record = {
            "schema_version": 1,
            "event": "server_copy_failure",
            "monotonic_ns": 1,
            "opcode": "GETM",
            "src_host": 0,
            "dst_host": 0,
            "snoop_id": 0,
            "line_address": run_g0.CXL_DPA_BASE,
            "payload_len": 0,
            "status": "OK",
            "dirty_data": False,
        }
        with tempfile.TemporaryDirectory() as temporary:
            trace = Path(temporary) / "coherence.jsonl"
            trace.write_text(json.dumps(record) + "\n", encoding="utf-8")
            evidence = run_g0.analyze_trace(trace, 4096)
        self.assertEqual(evidence["error_events"], 1)
        self.assertFalse(evidence["complete"])

    def test_trace_accepts_dirty_downgrade_and_invalidate_in_both_directions(self):
        records = []
        monotonic = 0
        for requester, owner, opcode, line in (
            (1, 0, "SNP_DATA_DOWNGRADE", run_g0.CXL_DPA_BASE),
            (0, 1, "SNP_DATA_INV", run_g0.CXL_DPA_BASE + 64),
        ):
            snoop_id = monotonic + 100
            for event in (
                {
                    "event": "request",
                    "opcode": "GETS" if opcode.endswith("DOWNGRADE") else "GETM",
                    "src_host": requester,
                    "dst_host": requester,
                    "snoop_id": 0,
                    "dirty_data": False,
                    "payload_len": 0,
                },
                {
                    "event": "snoop_send",
                    "opcode": opcode,
                    "src_host": requester,
                    "dst_host": owner,
                    "snoop_id": snoop_id,
                    "dirty_data": False,
                    "payload_len": 0,
                },
                {
                    "event": "snoop_ack",
                    "opcode": "SNOOP_ACK",
                    "src_host": owner,
                    "dst_host": requester,
                    "snoop_id": snoop_id,
                    "dirty_data": True,
                    "payload_len": 64,
                },
                {
                    "event": "dirty_completion",
                    "opcode": "SNOOP_ACK",
                    "src_host": owner,
                    "dst_host": requester,
                    "snoop_id": snoop_id,
                    "dirty_data": True,
                    "payload_len": 64,
                },
            ):
                monotonic += 1
                records.append(
                    {
                        "schema_version": 1,
                        "monotonic_ns": monotonic,
                        "line_address": line,
                        "status": "OK",
                        **event,
                    }
                )
        with tempfile.TemporaryDirectory() as temporary:
            trace = Path(temporary) / "coherence.jsonl"
            trace.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            evidence = run_g0.analyze_trace(trace, 4096)
        self.assertTrue(evidence["complete"])
        self.assertEqual(
            {item["snoop_opcode"] for item in evidence["dirty_handoffs"]},
            {"SNP_DATA_DOWNGRADE", "SNP_DATA_INV"},
        )
        self.assertEqual(evidence["range"]["dpa_start"], 0)
        self.assertEqual(evidence["range"]["guest_hpa_start"], run_g0.CXL_HPA_BASE)
        self.assertEqual(evidence["dirty_handoff_counts"], {"0->1": 1, "1->0": 1})


if __name__ == "__main__":
    unittest.main()
