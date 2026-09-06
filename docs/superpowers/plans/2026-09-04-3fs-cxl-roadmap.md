# 3FS CXL Migration Roadmap

> **Execution constraint:** Implement the linked plans task-by-task in one
> agent. Do not spawn subagents unless the user separately requests them. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute the approved two-phase 3FS transport migration through explicit feasibility, functional and comparative gates while keeping all source changes inside `components/3FS` and uncommitted.

**Architecture:** G0 proves the RISC-V guest toolchain and runtime contract; phase 1 replaces every RDMA RPC and bulk path while retaining TCP control traffic; phase 2 moves Core and the FoundationDB carrier to CXL; the final plan compares phase 1, phase 2 and unmodified LegoFS only after their evidence gates pass.

**Tech Stack:** C++20, Python 3, CMake/Ninja, Folly, Linux devdax/TUN, FoundationDB, RISC-V QEMU, CXLMemSim, GoogleTest, P, IO500.

**Spec:** `docs/superpowers/specs/2026-09-04-3fs-cxl-transport-design.md`

## Global Constraints

- Source and documentation changes stay inside `components/3FS`.
- Remote execution stays under `/home/guokc/mypro/gnn-mount-sda1/CXLMemSim-riscv` on `vlm-server`.
- The parent QEMU/Linux/CXLMemSim platform and LegoFS are consumed read-only.
- Phase 1 keeps Core, FoundationDB and CXL bootstrap TCP; all paths formerly using RDMA move together to CXL.
- Phase 2 moves Core directly to CXL, uses static CXL rendezvous and carries FoundationDB TCP/IP through `cxl0` over CXL.
- No stage requires RDMA hardware and no failure permits an RDMA or TCP fallback.
- A later gate cannot pass using a mock, host-native substitute or incomplete evidence from an earlier failed gate.
- Generated benchmark claims are functional QEMU/TCG plus CXLMemSim evidence, not physical CXL performance.
- Do not create commits until the user explicitly changes that instruction.

---

## Execution order

| Order | Gate | Plan | Required outcome |
| --- | --- | --- | --- |
| 1 | G0-A | `2026-09-04-3fs-cxl-g0-riscv-feasibility.md` | Standalone RISC-V C++/DAX/FDB and guest kernel/rootfs proof without linking current RDMA-coupled common code |
| 2 | G1 plus phase-1 source closure | `2026-09-04-3fs-cxl-phase1-rdma-replacement.md` | Neutral APIs, CXL ABI/host tests and a CXL-only source tree |
| 3 | G0-B | `2026-09-04-3fs-cxl-g0-riscv-feasibility.md` | Full RISC-V 3FS binary/start closure with no verbs dependency |
| 4 | G2-G3 / P1 | `2026-09-04-3fs-cxl-phase1-rdma-replacement.md` | Every former RDMA RPC/bulk/forward/resync path uses CXL; retained TCP classes only |
| 5 | G4-G5 / P2 | `2026-09-04-3fs-cxl-phase2-tcp-replacement.md` | Core uses CXL; FDB crosses `cxl0`; zero ordinary serving TCP after readiness |
| 6 | G6 | `2026-09-04-3fs-cxl-legofs-evaluation.md` | Five valid interleaved runs and evidence-linked comparison with unmodified LegoFS |

The design spec is authoritative when a plan summary is ambiguous. A plan's more detailed test command and file mapping is authoritative for execution mechanics as long as it does not weaken the spec.

## Checkpoint 1: Complete G0-A before transport implementation

- [ ] Run the G0 preflight on `vlm-server` and resolve the currently known missing RISC-V C++ compiler/sysroot inputs.
- [ ] Import a provenance-complete native RISC-V FoundationDB bundle, or run
      the pinned 7.3.63 source-port experiment; stop if neither produces both
      `fdbserver` and `libfdb_c` for RISC-V with C API 710.
- [ ] Build the standalone RISC-V DAX, FUSE and FDB probes without linking the current 3FS `common` library.
- [ ] Prove target 32-bit and 64-bit atomics are lock-free and run the pinned RISC-V
      `fdbserver`/`libfdb_c` transaction without a host substitute.
- [ ] Boot the existing RISC-V QEMU/CXLMemSim platform and prove `/dev/dax0.0` mapping/publication.
- [ ] Preserve the exact compatible RISC-V FDB client/server artifact hashes
      for reuse in both transport phases.
- [ ] Preserve a passing G0-A evidence bundle and its artifact hashes.

Stop here if G0-A fails. Do not begin CXL RPC code using an x86-only stand-in as integration evidence.

## Checkpoint 2: Complete phase 1 as one RDMA cutover

- [ ] Introduce transport-neutral identity, buffer and bulk interfaces.
- [ ] Make Control/Data plane an explicit connection/lane property and remove
      the bootstrap TCP path's ability to dispatch Data services.
- [ ] Replace unconditional `IBManager` process lifecycle with one
      process-scoped `CxlFabric` mapping and a neutral runtime owner.
- [ ] Implement and formally model the shared-region ABI, owner-separated lanes, progress and timeout rules.
- [ ] Integrate CXL with the existing RPC stack using TCP only for the declared bootstrap/control classes.
- [ ] Replace storage client/server remote buffers, reads, writes, forwarding and resync together.
- [ ] Build with `HF3FS_ENABLE_RDMA=OFF` and verify no verbs dependency or open attempt.
- [ ] Return to the G0 plan and pass G0-B for all complete RISC-V 3FS binaries before starting multi-guest tests.
- [ ] Pass the full phase-1 multi-guest filesystem gate with positive CXL RPC/bulk evidence.

Do not begin phase 2 until storage replication and resync no longer carry RDMA-specific state outside the optional IB backend.

## Checkpoint 3: Complete phase 2 as one TCP-serving cutover

- [ ] Freeze a deterministic guest/process-endpoint/lane manifest and implement
      static generation-scoped rendezvous; every DAX-mapping process, including
      `cxl-netd`, has a unique writer identity.
- [ ] Move Core services directly onto `CxlSocket` and disable the phase-1 TCP bootstrap.
- [ ] Build `cxl-netd`, provision `cxl0` and route all FDB coordinator/worker addresses through it.
- [ ] Prove a real RISC-V FoundationDB transaction and FDB restart over CXL packet lanes.
- [ ] Pass the post-readiness gate with `serving_tcp_bytes=0`, `fdb_cxl_bytes>0`, `rdma_open_attempts=0` and `forbidden_fallbacks=0`.
- [ ] Pass the complete phase-2 FUSE filesystem and fault matrix.

Normal phase-2 control and evidence collection use QEMU serial consoles. Guest
ordinary NICs are unaddressed and down before `CXL_READY`; only the explicit
negative-control run temporarily enables one and must be rejected.

## Checkpoint 4: Compare with LegoFS

- [ ] Freeze identical platform, topology, queue, polling, storage, workload and semantic inputs.
- [ ] Obtain one valid pilot per variant before starting measured runs.
- [ ] Verify that the known failed LegoFS tiny run is rejected by the common gate.
- [ ] Use tiny/easy/metadata/random-4K only as functional qualification, then
      run clean-valid SCC/standard IO500 profiles in an interleaved order.
- [ ] Run transport sizes/queue depths in their own contract-gated interleaved groups.
- [ ] Collect at least five complete valid interleaved pairing blocks per
      reported principal cell; replace a failed block as a whole and aggregate
      median plus relative MAD only from complete blocks.
- [ ] Use fresh-backing, zero-warm-up cold IO500 runs for the principal LegoFS
      comparison; publish no warm-cache ratio without an unmodified
      retain-cohort capability on both sides.
- [ ] Keep RF1 direct comparison, RF2/RF3 scaling and different durability classes separate.
- [ ] Generate a report whose every result links to an immutable evidence hash.

## Cross-plan handoff contract

Each completed plan supplies the next plan with one immutable JSON handoff record containing:

```json
{
  "schema": "hf3fs.cxl-handoff.v1",
  "gate": "G0-B",
  "status": "passed",
  "source_identity_sha256": "64 lowercase hexadecimal characters",
  "platform_identity_sha256": "64 lowercase hexadecimal characters",
  "artifact_manifest_sha256": "64 lowercase hexadecimal characters",
  "evidence_bundle": "components/3FS/out/cxl-riscv/g0-b/20260904T130000Z-1234/result.json"
}
```

The real `gate` and evidence path change at each handoff. Source-only closures
use the purpose-specific `hf3fs.cxl-source-closure.v1` schema defined in the
phase plans. A consumer validates all hashes before starting. `status` other
than `passed`, a missing artifact, a changed hash or a schema mismatch blocks
the consumer.

## Final acceptance

- [ ] G0, P1 and P2 evidence each validate independently.
- [ ] The phase-1 manifest proves only the approved TCP classes remained.
- [ ] The phase-2 manifest proves all serving carriers used CXL after readiness.
- [ ] Comparison statistics contain only valid, same-semantics runs.
- [ ] LegoFS and all parent components have no source edit caused by this work.
- [ ] `git -C components/3FS diff --check` passes.
- [ ] `git -C components/3FS status --short` shows the intended uncommitted 3FS work.
- [ ] No commit has been created.
