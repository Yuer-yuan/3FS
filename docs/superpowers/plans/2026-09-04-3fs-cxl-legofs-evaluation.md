# 3FS CXL and LegoFS Comparative Evaluation Implementation Plan

> **Execution constraint:** Implement task-by-task in one agent. Do not spawn
> subagents unless the user separately requests them. Steps use checkbox
> (`- [ ]`) syntax for tracking.

**Goal:** Produce a reproducible, validity-gated comparison of 3FS-CXL phase 1, 3FS-CXL phase 2 and LegoFS-CXL on the same RISC-V QEMU/CXLMemSim platform without modifying LegoFS.

**Architecture:** A 3FS-owned experiment manifest freezes platform, topology,
workload and semantic controls; adapters invoke the existing 3FS and LegoFS
runners as black boxes; a common validator admits only evidence-complete runs;
and an aggregator reports within-system transport diagnostics plus direct IO500
medians/relative MAD while keeping non-equivalent transport, durability and
replication classes separate.

**Tech Stack:** Python 3 standard library and `unittest`, C++20 transport microbenchmark, JSON/JSONL, IO500 configurations, RISC-V QEMU guests, CXLMemSim coherence-v2, existing LegoFS runners invoked read-only.

**Spec:** `docs/superpowers/specs/2026-09-04-3fs-cxl-transport-design.md`

## Global Constraints

- Begin performance comparison only after the relevant phase-1 or phase-2 functional gate passes.
- Modify source and documentation only inside `components/3FS`; never patch `components/legofs`, top-level runners, QEMU, Linux or CXLMemSim for this comparison.
- Execute on `vlm-server` from `/home/guokc/mypro/gnn-mount-sda1/CXLMemSim-riscv`.
- Treat LegoFS as a read-only measured system. Generated LegoFS run bundles may remain in its existing runner-owned result directory, but the comparison adapter copies and hashes accepted evidence into `components/3FS/out/cxl-compare/`.
- Label every result `functional QEMU/TCG + CXLMemSim evidence; not physical CXL performance`.
- Do not report a measured 3FS-RDMA baseline because no RDMA device exists. External RDMA numbers are contextual and never enter plots or ratios.
- Fix QEMU/Linux/CXLMemSim commits, normalized per-role CXL device argv,
  endpoint-backing inode policy, authoritative SSD-stream configuration and
  all exposed CXL capacity, cache, line, latency, bandwidth, disk-image,
  polling and queue parameters across comparable variants. For RF1 full-system
  comparison, also fix client count and aggregate vCPU and RAM across every
  client, server and infrastructure guest; record each system's native internal
  guest/role partition inside that equal envelope.
- Freeze the host kernel/CPU topology, QEMU accelerator and TCG thread mode,
  host CPU/NUMA affinity, backing filesystem/device class and a predeclared
  host-load/noise policy. Measured slots run serially and their process
  lifetimes must not overlap another variant.
- Compare 3FS replication factor 1 directly with single-server LegoFS. Report 3FS RF=2 and RF=3 separately as scaling/durability results.
- Compare only matching completion semantics: visibility, local durability or replicated durability. Never relabel a visibility result as durable.
- Normalize and compare the realized IO500 configuration fields, not profile
  names alone. A `standard`/`scc` label with different phase sizes, stonewall,
  file counts or API choices is not a paired case.
- Do not calculate a cross-system transport ratio unless both unmodified
  systems expose the same payload, concurrency, timing and completion contract
  on the same QEMU/CXLMemSim path. Otherwise publish separate diagnostic panels
  and use RF1 IO500 for the direct comparison.
- For every preconditioned configuration, perform its declared warm-up inside
  the measured command's live guest cohort. The principal cross-system IO500
  profile is instead `cold`: every slot uses fresh backing and zero warm-ups,
  because the audited read-only LegoFS runner has no retain-cohort/warm-up
  interface. Require at least five valid interleaved complete blocks and never
  substitute best-of-N. Publish no cross-system warm-cache ratio unless a
  later unmodified LegoFS runner passes that capability gate.
- Preserve failed runs and their first failure. They do not enter statistics.
- Treat `tiny`, `easy-smoke`, `metadata-smoke` and `rnd4k` as qualification
  profiles only: the audited LegoFS runner classifies them as semantic smokes
  whose IO500 verifier expects an invalid-size result. Only a profile with a
  clean valid IO500 verifier, initially `scc` and `standard`, may enter
  filesystem performance statistics.
- The previously inspected LegoFS result `placement-route-restore-v118-10c1s-tiny-r2` is failed and is an explicit negative fixture, not a baseline.
- Do not create commits; leave all work uncommitted.
- Unless a step explicitly names the superproject root, run its commands from the `components/3FS` checkout root.

---

## File map

**Create `benchmarks/cxl_transport_bench`:**

- `CMakeLists.txt`
- `CxlTransportBench.cc`
- `BenchProtocol.h`
- `BenchStats.h`, `BenchStats.cc`

**Create `benchmarks/cxl_compare`:**

- `README.md`
- `experiment.schema.json`
- `experiment.json`
- `manifest.py` — normalize and validate experiment identity.
- `adapters.py` — build exact 3FS/LegoFS argv and locate result bundles.
- `validate.py` — common and variant-specific valid-run gates.
- `schedule.py` — deterministic interleaving.
- `run.py` — orchestration and immutable run ledger.
- `aggregate.py` — median, MAD and paired ratios.
- `report.py` — Markdown/CSV/JSON report generation.
- `configs/io500-easy-smoke.ini`
- `configs/io500-metadata-smoke.ini`
- `configs/io500-rnd4k.ini`
- `configs/io500-tiny.ini`
- `configs/io500-scc.ini`
- `configs/io500-standard.ini`

**Create tests:**

- `tests/benchmarks/test_compare_manifest.py`
- `tests/benchmarks/test_compare_adapters.py`
- `tests/benchmarks/test_compare_validate.py`
- `tests/benchmarks/test_compare_schedule.py`
- `tests/benchmarks/test_compare_aggregate.py`
- `tests/benchmarks/TestBenchStats.cc`
- `tests/benchmarks/TestBenchProtocol.cc`
- `tests/benchmarks/CMakeLists.txt`
- `tests/benchmarks/fixtures/legofs_failed_result.json`
- `tests/benchmarks/fixtures/legofs_valid_result.json`
- `tests/benchmarks/fixtures/threefs_p1_valid_result.json`
- `tests/benchmarks/fixtures/threefs_p2_valid_result.json`

**Modify:**

- `benchmarks/CMakeLists.txt`
- `tests/CMakeLists.txt`
- `.gitignore` — ignore only `out/cxl-compare/`.

## Task 1: Define a frozen experiment manifest and provenance contract

**Files:**

- Create: `benchmarks/cxl_compare/experiment.schema.json`
- Create: `benchmarks/cxl_compare/experiment.json`
- Create: `benchmarks/cxl_compare/manifest.py`
- Create: `tests/benchmarks/test_compare_manifest.py`

**Interfaces:**

- Produces: `Experiment.load(path)`, `validate(superproject_root)`, `canonical_bytes()` and `experiment_id()`.
- Records: source commits/dirty hashes, platform artifact hashes, topology,
  normalized QEMU Type-3/FMW/HDM-DB/BI argv, endpoint path/device/inode
  identities, normalized CXLMemSim SSD-stream/coherence argv, CXL model,
  queues, polling, disks, workloads, seeds, repetitions, operation contract,
  completion class, normalized realized IO500 configuration and aggregate
  resource-envelope ID.
- Records: host uname/CPU topology, QEMU acceleration/threading, process
  affinity, backing mount/device identity, start/end load and pressure samples,
  and runner-owned measured-process lifetimes.
- Gate: a comparison row is valid only when all fields named in the spec's experimental-controls section match its comparison group.
- Gate: a cross-system RF1 throughput ratio additionally requires the same
  aggregate resource-envelope ID; internal role topology is recorded and
  charged but is not required to have the same guest count.

Keep three identities separate:

| Identity | Across comparable repetitions |
| --- | --- |
| source/artifact identity | byte-identical |
| comparison-contract ID | byte-identical after schema-defined normalization |
| run-instance ID | unique: session generation, owner token, ports, paths and inode numbers |

Normalization may replace only explicitly typed ephemeral fields with stable
tokens. It retains concrete values in each evidence bundle and verifies their
required uniqueness, range and ownership. It compares backing-inode
relationships (`all-distinct`/`all-shared`) and backing device/mount class, not
literal inode numbers from different cold runs. Unknown fields are not dropped.

- [ ] **Step 1: Write missing-field and canonical-hash tests**

```python
COMPARE_DIR = Path(__file__).parents[2] / "benchmarks" / "cxl_compare"
sys.path.insert(0, str(COMPARE_DIR))
import manifest

class CompareManifestTest(unittest.TestCase):
    def test_rejects_unpinned_platform(self):
        raw = complete_experiment()
        del raw["platform"]["qemu_commit"]
        self.assertIn("platform.qemu_commit is required", manifest.Experiment(raw).validate(Path("/repo")))

    def test_key_order_does_not_change_experiment_id(self):
        first = manifest.Experiment(complete_experiment())
        second = manifest.Experiment(dict(reversed(list(complete_experiment().items()))))
        self.assertEqual(first.experiment_id(), second.experiment_id())
```

- [ ] **Step 2: Run and observe the missing module**

Run from `components/3FS`:

```bash
python3 -m unittest -v tests/benchmarks/test_compare_manifest.py
```

Expected: FAIL with `ModuleNotFoundError: No module named 'manifest'`.

- [ ] **Step 3: Implement the schema and canonical identity**

The checked-in manifest declares these variant IDs exactly:

```json
{
  "variants": ["3fs-cxl-p1-rf1", "3fs-cxl-p2-rf1", "legofs-cxl-1s"],
  "extra_variants": ["3fs-cxl-p1-rf2", "3fs-cxl-p2-rf2", "3fs-cxl-p2-rf3"],
  "repetitions": 5,
  "measurement_profiles": {
    "transport-preconditioned": {"cache_state": "preconditioned", "warmups": 1},
    "io500-rf1-cold": {"cache_state": "cold", "warmups": 0}
  },
  "max_block_attempts": 10,
  "claims": ["functional-qemu-tcg-cxlmemsim"],
  "line_bytes": 64
}
```

`canonical_bytes()` uses UTF-8 JSON with sorted keys, compact separators and a trailing newline. `experiment_id()` is the first 16 lowercase hexadecimal characters of its SHA-256.

For a `preconditioned` profile, `warmups` means workload preconditioning inside
every measured command after readiness, using the same live guest cohort and
storage state. It does not mean a disposable VM run whose caches disappear
before measurement. A `cold` profile requires fresh backing and zero workload
warm-ups. Here cold means new writable storage/CXL backing and new guest plus
simulator processes; the runner does not claim to evict host kernel page cache.
Rotating complete blocks mitigates that remaining host-state drift. Cold and
preconditioned groups are never aggregated. `repetitions`
is the target number of complete valid pairing blocks,
not merely the number of attempted slots. `max_block_attempts` bounds failures
independently for each exact case;
reaching it produces an explicit `insufficient valid repetitions` result rather
than selecting the best surviving runs or looping forever.

- [ ] **Step 4: Record dirty source without changing it**

For every relevant repository path, record `HEAD`, the tracked `git diff
--binary` SHA-256, tracked tombstones, `git status --porcelain=v1`, and a sorted
path/mode/type/content hash manifest for every non-ignored untracked file. A
dirty tree is allowed because this work is intentionally uncommitted, but two
compared repetitions must use the same combined canonical source identity. Status
output alone cannot identify the contents of the new untracked CXL sources.

Hash each QEMU binary, guest kernel, guest image, CXLMemSim library, 3FS binary, LegoFS binary, IO500 binary and config file. Reject missing or mutable-during-run artifacts.

Parse the realized per-role QEMU and CXLMemSim argv into a canonical platform
record; do not compare raw path strings alone. Require equal CPU cache-block,
FMW restriction/size, HDM passthrough, flit, HDM-DB, Type-3 persistence,
coherence host-ID uniqueness, cache/ways/timeout/read policy, logical fabric
capacity and simulator SSD-stream parameters. Record endpoint backing
`[st_dev, st_ino]` relationships as `all-distinct`, `all-shared` or
`mixed`. The comparison adapter configures the 3FS measured run to the
unmodified LegoFS runner's realized policy. Reject `mixed`, or any policy
mismatch, before calculating a ratio. This matched measurement remains
separate from G0's mandatory distinct-inode coherence proof.

- [ ] **Step 5: Validate the checked-in experiment**

Run:

```bash
python3 benchmarks/cxl_compare/manifest.py \
  --experiment benchmarks/cxl_compare/experiment.json \
  --superproject-root /home/guokc/mypro/gnn-mount-sda1/CXLMemSim-riscv \
  --print-id
```

Expected: one 16-character experiment ID followed by `manifest valid`.

- [ ] **Step 6: Review checkpoint**

Run `git diff --check`, inspect the schema for every control in the design spec and leave changes uncommitted.

## Task 2: Add a transport-isolated 3FS CXL diagnostic

**Files:**

- Create: `benchmarks/cxl_transport_bench/CMakeLists.txt`
- Create: `benchmarks/cxl_transport_bench/BenchProtocol.h`
- Create: `benchmarks/cxl_transport_bench/BenchStats.h`
- Create: `benchmarks/cxl_transport_bench/BenchStats.cc`
- Create: `benchmarks/cxl_transport_bench/CxlTransportBench.cc`
- Create: `tests/benchmarks/TestBenchStats.cc`
- Create: `tests/benchmarks/TestBenchProtocol.cc`
- Create: `tests/benchmarks/CMakeLists.txt`
- Modify: `benchmarks/CMakeLists.txt`
- Modify: `tests/CMakeLists.txt`
- Create: `tests/benchmarks/test_compare_validate.py`

**Interfaces:**

- Produces: `cxl_transport_bench --role server|client --manifest PATH --case CASE --output PATH`.
- Cases: framed RPC sizes `64,1024,8192,65536,1048576,4194304`; queue depths `1,8,32,64`; bulk sizes `4096,65536,1048576,4194304`.
- Produces one JSON record with request count, checksum, latency histogram,
  useful bytes, CXL bytes, stream frames, copies, queue-full time, poller time
  and simulator counters.

- [ ] **Step 1: Add statistics and schema tests**

```cpp
TEST(BenchStats, QuantilesUseNearestRank) {
  BenchStats stats;
  for (uint64_t value : {10, 20, 30, 40, 50}) stats.add(value);
  EXPECT_EQ(stats.percentile(50), 30);
  EXPECT_EQ(stats.percentile(99), 50);
}

TEST(BenchProtocol, RefusesPayloadLargerThanDeclaredCase) {
  BenchCase c{.payloadBytes = 64, .queueDepth = 1, .operations = 1000};
  EXPECT_FALSE(c.validatePayload(std::vector<std::byte>(65)));
}
```

- [ ] **Step 2: Run and observe the missing target**

Run:

```bash
cmake --build build/cxl-g0-host --target test_benchmarks
```

Expected: FAIL until the new benchmark sources and tests are registered.

- [ ] **Step 3: Implement a deterministic echo and bulk protocol**

Use a fixed seed from the experiment manifest. Every response returns request sequence, payload length and CRC32C. Warm-up operations are completed and discarded before resetting counters. Start the measurement only after both endpoints acknowledge the same session epoch and case hash.

Do not use wall-clock synchronization between guests. The client measures request latency with its own monotonic clock; throughput uses client elapsed time from the first measured publish through the last validated completion.

Expose `BenchStats` and protocol validation through a
`cxl-transport-bench-lib` target, link `cxl_transport_bench` against it, and
register `tests/benchmarks` as the `test_benchmarks` target in
`tests/CMakeLists.txt`.

- [ ] **Step 4: Bind each metric to a source**

Record:

```text
latency_ns: client monotonic timestamps
throughput_ops_s and useful_bytes_s: client
cxl_bytes, stream_frames, queue occupancy: CxlMetrics deltas
poll_spin_ns, poll_yield_count, poll_sleep_count: progress-engine deltas
copy_bytes: explicit copy instrumentation
coherence_events: CXLMemSim run evidence
```

Fail the case if request count, response count, sequence continuity or aggregate payload hash differs.

- [ ] **Step 5: Run host file-region tests**

Run:

```bash
cmake --build build/cxl-g0-host --target cxl_transport_bench test_benchmarks
build/cxl-g0-host/tests/test_benchmarks --gtest_filter='BenchStats.*:BenchProtocol.*'
build/cxl-g0-host/bin/cxl_transport_bench \
  --role selftest --manifest benchmarks/cxl_compare/experiment.json \
  --case rpc-65536-qd8 --output build/cxl-g0-host/selftest.json
```

Expected: PASS with `operations_valid=true` and positive CXL byte counters.

- [ ] **Step 6: Review checkpoint**

Run `git diff --check`; verify benchmark-only code does not alter transport behavior and leave changes uncommitted.

## Task 3: Build read-only runner adapters for all variants

**Files:**

- Create: `benchmarks/cxl_compare/adapters.py`
- Create: `tests/benchmarks/test_compare_adapters.py`
- Modify: `benchmarks/cxl_compare/experiment.json`

**Interfaces:**

- Produces: `ThreeFsAdapter.command(case, run_dir, resource_envelope)` and
  `LegoFsAdapter.command(case, run_dir, resource_envelope)` as argv/env
  records.
- Produces: `transport_capability(case)` with a machine-readable
  `directly_comparable` boolean and exact mismatch reasons.
- Produces: `ResultLocator.locate(completed_command) -> Path` without editing the measured runner's files.
- Enforces: source paths, output paths and variant-specific required flags.

- [ ] **Step 1: Write exact argv tests**

```python
class CompareAdaptersTest(unittest.TestCase):
    def test_phase2_disables_rdma_and_tcp_bootstrap(self):
        command = adapter_for("3fs-cxl-p2-rf1").command(
            io500_case("tiny", client_ranks=4), Path("/runs/p2"), envelope()
        )
        joined = " ".join(command.argv)
        self.assertIn("run_phase2.py", joined)
        self.assertIn("HF3FS_ENABLE_RDMA=OFF", joined)
        self.assertIn("HF3FS_CXL_TCP_BOOTSTRAP=OFF", joined)
        self.assertIn("--stage io500", joined)
        self.assertIn("--client-ranks 4", joined)
        self.assertIn("--io500-config", joined)
        self.assertIn("--resource-envelope", joined)
        self.assertIn("--platform-contract", joined)
        self.assertIn("--endpoint-backing-policy", joined)
        self.assertIn("--measurement-marker", joined)
        self.assertIn("--warmup-iterations", joined)
        self.assertIn("--cache-state", joined)
        self.assertIn("--result-class", joined)

    def test_legofs_uses_existing_runner_read_only(self):
        command = adapter_for("legofs-cxl-1s").command(
            io500_case("tiny"), Path("/runs/lego"), envelope()
        )
        self.assertEqual(command.argv[1], "/repo/scripts/legofs_io500.py")
        self.assertIn("--serving-transport", command.argv)
        self.assertIn("cxl", command.argv)
        self.assertEqual(command.writable_source_paths, ())

    def test_legofs_rejects_preconditioned_profile_without_a_cohort_hook(self):
        capability = adapter_for("legofs-cxl-1s").measurement_capability(
            io500_case("tiny", cache_state="preconditioned", warmups=1)
        )
        self.assertFalse(capability.directly_comparable)
        self.assertIn("no retain-cohort warm-up interface", capability.reasons)

    def test_host_only_legofs_fabric_probe_is_not_a_qemu_ratio(self):
        capability = adapter_for("legofs-cxl-1s").transport_capability(
            transport_case("rpc-65536-qd8")
        )
        self.assertFalse(capability.directly_comparable)
        self.assertIn("different operation contract", capability.reasons)
        self.assertIn("not the multi-guest QEMU path", capability.reasons)
```

- [ ] **Step 2: Run and observe the missing adapter module**

Run:

```bash
python3 -m unittest -v tests/benchmarks/test_compare_adapters.py
```

Expected: FAIL with an import error for `adapters`.

- [ ] **Step 3: Implement 3FS commands**

Map every `3fs-cxl-p1-rf{1,2}` variant to
`deploy/cxl-riscv/run_phase1.py` and every
`3fs-cxl-p2-rf{1,2,3}` variant to `deploy/cxl-riscv/run_phase2.py`. Pass an
absolute runner-owned output directory beneath the phase runner's existing
`out/cxl-riscv/...` confinement, exact base plus derived manifest hashes,
`--scenario/--stage io500`, absolute IO500 config, client ranks, immutable
resource envelope, replication factor, seed, measurement marker and the
frozen platform/backing policy. Pass `--warmup-iterations` and `--cache-state`
plus `--result-class` from the workload group without adapter-specific defaults. After validation,
hash-copy the immutable
result into the comparison `run_dir`; do not point a phase runner directly at
`out/cxl-compare`. Reject phase 1 when a case demands zero serving TCP; reject
phase 2 unless its FDB/TCP gate is enabled. The adapter verifies that
derivation preserved base endpoint IDs, that the number of storage guests
equals the declared factor, and that each client rank has one independent
guest/FUSE endpoint. For multi-rank phase 2, require the comparison-only
`mpi0` network to match LegoFS's QEMU socket/multicast and MPI/Hydra contract
while remaining unreachable to 3FS/FDB processes.

- [ ] **Step 4: Implement the LegoFS IO500 command**

For the one-rank fixture, build this argv from manifest values:

```text
/usr/bin/python3
/home/guokc/mypro/gnn-mount-sda1/CXLMemSim-riscv/scripts/legofs_io500.py
--stage tiny
--server-count 1
--client-count 1
--serving-transport cxl
--cursor-mode owned
--cq-wait-mode timer_sleep
--result-label cxlcmp-legofs-tiny-r01
--timeout 7200
```

For measured runs, replace the shown `--client-count 1` with the frozen
resource envelope's rank count. Also pass the manifest's coherence-cache,
SSD-cache, durability, persistence-owner, packed-segment, close-batch and
observation settings. Capture the runner's emitted result path; hash and copy
`result.json`, manifests, logs and validation artifacts into the 3FS-owned run
bundle. Do not delete or rewrite the original.

The audited runner accepts one `--stage` per process and shuts down its QEMU
guests and CXLMemSim server before returning. It has no retain-cohort or
discarded-warm-up CLI. Therefore map the principal direct IO500 comparison only
to `io500-rf1-cold`: require a never-before-used result label and fresh backing,
and record `warmups=0`. Do not approximate a warm-cache run with a separate
runner invocation. A preconditioned cross-system case is a capability omission
until an unmodified runner exposes a verifiable same-cohort hook.

Do not pass invented flags to the read-only runner. Treat its unique
`LEGOFS_IO500_MPI_STARTED stage=...` event as the workload-start marker, bind it
to the manifest slot through the immutable result label and event-log hash, and
copy the timestamp into the common evidence schema. Because every accepted cold
slot starts a new CXLMemSim process and fresh backing, its simulator counter
baseline is exactly zero; use the runner's existing before/after process-cost
window and final simulator record as the two other measurement boundaries.
Missing, duplicate or out-of-order boundaries make the slot invalid.

For `scc` and `standard`, extract the exact config preserved by the LegoFS run,
normalize section/key/value tuples and compare them with the 3FS-owned config
used by the corresponding adapter. Profile-name equality is insufficient.
Store both source hashes and the normalized contract hash; a mismatch makes the
cell `not directly comparable` before any rate is read.

- [ ] **Step 5: Add a versioned field-mapping table**

The adapter records how common concepts map:

| Common field | 3FS source | LegoFS source |
| --- | --- | --- |
| filesystem valid | phase evidence `filesystem_valid` | `result.json.filesystem_valid` |
| guest CXL evidence | per-endpoint CXL counters | `guest_visible_cxl_evidence` and serving evidence |
| completion class | 3FS operation profile | durability/persistence profile |
| client throughput | IO500 result | IO500 result |
| total resources | all 3FS plus FDB guests | all LegoFS guests |

Reject unknown runner schema versions instead of guessing fields.

Audit the existing read-only LegoFS probes explicitly. At the audited revision,
`components/legofs/scripts/run_fabric_microbench.py` uses a fixed host/NUMA
workload and a different staged/blob operation contract, while
`scripts/legofs_io500.py --stage hello` proves MPI placement rather than a
serving request. Neither may be silently treated as the matching raw 3FS
transport case. Record this as a capability result, not as a failed benchmark.
If a later unmodified LegoFS revision exposes a genuinely matching multi-guest
case, require a new schema/hash and re-run the capability gate before ratios.

- [ ] **Step 6: Run adapter tests**

Run:

```bash
python3 -m unittest -v tests/benchmarks/test_compare_adapters.py
```

Expected: PASS; every command is an argv tuple, uses absolute tool/source paths and has no shell expansion.

- [ ] **Step 7: Review checkpoint**

Run `git diff --check`; inspect `git -C ../legofs status --short` and the superproject status to confirm no LegoFS source change was introduced. Leave changes uncommitted.

## Task 4: Implement a strict common valid-run gate

**Files:**

- Create: `benchmarks/cxl_compare/validate.py`
- Create: `tests/benchmarks/test_compare_validate.py`
- Create: `tests/benchmarks/fixtures/legofs_failed_result.json`
- Create: `tests/benchmarks/fixtures/legofs_valid_result.json`
- Create: `tests/benchmarks/fixtures/threefs_p1_valid_result.json`
- Create: `tests/benchmarks/fixtures/threefs_p2_valid_result.json`

**Interfaces:**

- Produces: `ValidationResult(valid: bool, reasons: tuple[str, ...], normalized: dict)`.
- Produces: `validate_threefs_p1`, `validate_threefs_p2`, `validate_legofs` and `validate_common`.
- Produces: a separate `validate_qualification` result that can accept a
  stage-specific expected semantic-smoke verdict but exposes no performance
  fields and can never satisfy the measured-run gate.
- A run is immutable after validation; its bundle SHA-256 is recorded in the ledger.

- [ ] **Step 1: Add positive and negative fixtures**

The failed LegoFS fixture copies the decisive fields from the inspected remote run:

```json
{
  "status": "failed",
  "first_failure": "timed out waiting for MPI stage tiny exit",
  "filesystem_valid": false,
  "guest_visible_cxl_evidence": false
}
```

Test that it yields all three reasons and never normalizes performance values.

```python
def test_failed_legofs_result_is_not_a_baseline(self):
    result = validate.validate_legofs(load_fixture("legofs_failed_result.json"))
    self.assertFalse(result.valid)
    self.assertIn("status is not passed", result.reasons)
    self.assertIn("filesystem is invalid", result.reasons)
    self.assertIn("guest-visible CXL evidence is absent", result.reasons)
    self.assertNotIn("performance", result.normalized)
```

- [ ] **Step 2: Run tests and observe the absent validator**

Run:

```bash
python3 -m unittest -v tests/benchmarks/test_compare_validate.py
```

Expected: FAIL with an import error for `validate`.

- [ ] **Step 3: Implement common gates**

Require:

```text
status passed
filesystem/application validation true
runner cleanup complete
no pending request or owned process
no measured lifetime overlaps another variant and host-noise policy passes
manifest and artifact hashes match
normalized realized workload/configuration hash matches the case
guest-visible CXL evidence true for every serving participant
no checksum, generation, range, protocol or delivery failure
CXLMemSim evidence complete
completion class exactly matches its comparison group
declared cache state and warm-up count match the experiment
fresh backing is proven for every cold slot
for a preconditioned slot, warm-up completed in the measured command's cohort
pre-measurement cleanup, counter snapshots and measurement marker are present
```

- [ ] **Step 4: Implement 3FS-specific gates**

Both phases require `rdma_open_attempts=0` and `forbidden_fallbacks=0`. Phase 1 requires positive CXL RPC and bulk bytes and permits only the declared Core/FDB/bootstrap TCP classes. Phase 2 additionally requires `serving_tcp_bytes=0`, `fdb_cxl_bytes>0`, `fdb_route_device=cxl0` and static-rendezvous evidence.
For a manifest-declared multi-rank IO500 case, both phases may also carry only
the independently attributed MPI/Hydra owners, ports and bytes on the frozen
benchmark-control network. Those bytes never satisfy a serving-path gate;
unknown owners or routes invalidate the run.

- [ ] **Step 5: Implement LegoFS-specific gates**

Require the existing runner's serving-transport mode to be `cxl`, positive validated guest-visible CXL operations/bytes, zero post-readiness forbidden serving-path counters, valid IO500 verifier output and clean runner-owned process teardown.

For a measured filesystem slot, `valid IO500 verifier output` means the clean
valid verdict required by the runner's `scc`/`standard` paths; an
`EXPECTED_INVALID_SEMANTIC_SMOKE` result is qualification evidence only. Reject
any attempt to normalize its rate or count it as a measured run.

- [ ] **Step 6: Make incomplete metrics invalid rather than zero**

Missing counter, missing log or parse error is a gate failure. Never coerce it to zero. Preserve the source value, JSON pointer and reason in `validation.json`.

- [ ] **Step 7: Run all validator fixtures**

Run:

```bash
python3 -m unittest -v tests/benchmarks/test_compare_validate.py
```

Expected: valid fixtures pass; each single-field mutation and the known failed LegoFS fixture fail for the exact asserted reason.

- [ ] **Step 8: Review checkpoint**

Run `git diff --check`; ensure no performance field is read before validity succeeds and leave changes uncommitted.

## Task 5: Generate a deterministic interleaved schedule and run ledger

**Files:**

- Create: `benchmarks/cxl_compare/schedule.py`
- Create: `benchmarks/cxl_compare/run.py`
- Create: `tests/benchmarks/test_compare_schedule.py`

**Interfaces:**

- Produces: `make_schedule(case, variants, repetitions, seed) -> tuple[RunSlot, ...]`.
- Produces: append-only `ledger.jsonl` with planned, started, finished, validated and failed events.
- Resuming never overwrites an existing run bundle or changes an earlier schedule.
- A block is scoped to exactly one immutable case ID: suite/profile, payload,
  queue depth, topology, completion class and measurement profile. It contains
  one slot for each capability-admitted principal variant.

- [ ] **Step 1: Add balance and repeatability tests**

```python
class CompareScheduleTest(unittest.TestCase):
    def test_three_variant_rotation_is_balanced(self):
        schedule = schedule_module.make_schedule("io500-tiny", ("p1", "p2", "lego"), 5, 20260904)
        self.assertEqual(len(schedule), 15)
        self.assertEqual({slot.case_id for slot in schedule}, {"io500-tiny"})
        self.assertEqual(Counter(slot.variant for slot in schedule), {"p1": 5, "p2": 5, "lego": 5})
        self.assertNotEqual([s.variant for s in schedule[:3]], [s.variant for s in schedule[3:6]])

    def test_same_seed_is_byte_reproducible(self):
        self.assertEqual(render(make()), render(make()))

    def test_failed_slot_replaces_the_complete_pairing_block(self):
        ledger = run_one_block_with_failure("p2")
        replacement = schedule_module.replacements(ledger)
        self.assertCountEqual([s.variant for s in replacement], ["p1", "p2", "lego"])
        self.assertEqual({s.case_id for s in replacement}, {ledger.failed_case_id})
        self.assertNotEqual(replacement[0].block_id, ledger.failed_block_id)
```

- [ ] **Step 2: Run and observe the missing scheduler**

Run:

```bash
python3 -m unittest -v tests/benchmarks/test_compare_schedule.py
```

Expected: FAIL with an import error for `schedule`.

- [ ] **Step 3: Implement rotating blocks**

For each exact case and its capability-admitted principal variants, rotate the
seed-selected order by one position per block. The three-way RF1 IO500 cells
therefore use three slots per block; a transport cell may use only its admitted
variants and cannot inherit a ratio to an omitted system. Run one unmeasured
qualification command per variant before the schedule, but do not rely on it
for guest cache state. A
preconditioned measured command performs its declared warm-up after readiness
in the same guest cohort, validates warm-up cleanup, snapshots counters, emits
the common measurement marker and then runs the measured workload. A cold
command instead proves fresh backing, uses zero warm-ups and records the
runner-native workload-start marker in the common schema.

A failed measured slot stays failed and makes that entire block ineligible for
paired ratios. Append a fresh replacement block containing all principal
variants in the next rotation, each with a new run ID; never rerun only the
failed variant and pair it with older results. Aggregation requires five
complete valid blocks. Extra valid runs from incomplete blocks remain visible
as diagnostics but do not enter the principal paired median.

Expanding `--profiles` or a transport size/depth matrix creates independent
case/block sequences. Every cold IO500 slot launches a new guest/simulator
cohort, session and writable backing set; profiles never run consecutively on
the same backing. A top-level `run.py` invocation may orchestrate these slots,
but it does not turn them into one measurement process.

- [ ] **Step 4: Implement append-only execution**

Create every run directory with exclusive creation. Record command argv,
environment allowlist, start/end monotonic timestamps, readiness/warm-up/
measurement marker timestamps, counter snapshots, artifact hashes, exit
status, first failure and validation result. `--resume` validates existing
hashes and schedules only absent full replacement blocks; it never mutates or
fills a hole inside an earlier block.

- [ ] **Step 5: Add a dry-run command**

Run:

```bash
python3 benchmarks/cxl_compare/run.py \
  --experiment benchmarks/cxl_compare/experiment.json \
  --output out/cxl-compare/2026-09-04-audit \
  --dry-run
```

Expected: prints every planned measured run ID, its in-command warm-up
declaration and block ID in interleaved order, creates only the manifest and
ledger, and invokes no QEMU process.

- [ ] **Step 6: Review checkpoint**

Run `git diff --check`; verify run deletion and best-result selection are absent and leave changes uncommitted.

## Task 6: Implement median, relative MAD and paired comparison

**Files:**

- Create: `benchmarks/cxl_compare/aggregate.py`
- Create: `tests/benchmarks/test_compare_aggregate.py`

**Interfaces:**

- Produces: `median(values)`, `mad(values)`, `relative_mad(values)` and `aggregate(validated_runs)`.
- Produces paired ratios only for runs in the same block, workload, topology
  class, aggregate resource envelope, operation contract, completion class and
  normalized comparison-contract ID. Exact internal topology equality is required for
  transport-isolated diagnostics and 3FS P1/P2 pairs; cross-system full-stack
  RF1 permits different native role partitioning within the same total
  envelope.
- Never aggregates failed or semantically mismatched runs.

- [ ] **Step 1: Write exact statistics tests**

```python
class CompareAggregateTest(unittest.TestCase):
    def test_median_and_relative_mad(self):
        values = [8.0, 10.0, 10.0, 12.0, 100.0]
        self.assertEqual(aggregate.median(values), 10.0)
        self.assertEqual(aggregate.mad(values), 2.0)
        self.assertEqual(aggregate.relative_mad(values), 0.2)

    def test_rejects_invalid_and_mismatched_completion(self):
        with self.assertRaisesRegex(ValueError, "invalid run"):
            aggregate.aggregate([failed_run()])
        with self.assertRaisesRegex(ValueError, "completion class mismatch"):
            aggregate.paired_ratio(visibility_run(), durable_run())

    def test_rejects_mismatched_transport_contract(self):
        with self.assertRaisesRegex(ValueError, "operation contract mismatch"):
            aggregate.paired_ratio(framed_rpc_run(), staged_file_run())

    def test_incomplete_block_is_diagnostic_only(self):
        runs = complete_block()
        runs.pop()  # one principal variant is absent
        result = aggregate.aggregate(runs)
        self.assertEqual(result.principal_samples, ())
        self.assertEqual(result.exclusions[0].reason, "incomplete pairing block")
```

- [ ] **Step 2: Run and observe the missing aggregator**

Run:

```bash
python3 -m unittest -v tests/benchmarks/test_compare_aggregate.py
```

Expected: FAIL with an import error for `aggregate`.

- [ ] **Step 3: Implement statistics without external packages**

Use `statistics.median`. Define MAD as the median absolute deviation from the median; define relative MAD as `MAD / abs(median)`. If the median is zero, emit JSON `null` and the reason `zero median` instead of dividing.

Group first by the complete immutable case ID and block ID. Admit a principal
sample only when the block contains exactly one valid slot for every required
variant and no extra/duplicate slot. Preserve incomplete blocks and their
reason as diagnostics; never fill them from a different block.

- [ ] **Step 4: Normalize transport metrics**

For every size/queue-depth cell report p50/p95/p99 latency, ops/s, useful
bytes/s, useful/CXL byte ratio, stream frames/op, copy bytes/op, maximum
occupancy, queue-full time share and poller CPU share. Report raw counts beside
ratios. When `directly_comparable` is false, keep each system in a separate
diagnostic panel, emit no speedup/slowdown ratio and preserve the mismatch
reasons in JSON and Markdown.

- [ ] **Step 5: Normalize filesystem metrics and resource cost**

For every IO500 phase report client-visible rate plus total vCPU-seconds, maximum aggregate guest RSS, number of server/infrastructure guests and CXL bytes. Include the FDB guest in 3FS totals. Do not combine RF1 and RF2/RF3.

For phase 2, also report FDB CXL packets, packet fragments, bytes and the
fragment-per-packet ratio as carrier overhead. Keep those counters out of the
transport-isolated RPC panel and do not compare them to a LegoFS request-entry
count as though they had the same operation contract.

Reject a cross-system speed ratio when aggregate vCPU, configured RAM, client
rank count, writable capacity or exposed CXL capacity differs. In that case,
publish separate feasibility and throughput-per-resource panels with the exact
mismatch reason; do not silently normalize unlike runs into a direct ratio.

- [ ] **Step 6: Run aggregate tests**

Run:

```bash
python3 -m unittest -v tests/benchmarks/test_compare_aggregate.py
```

Expected: PASS with exact medians/MADs and deterministic sorted JSON.

- [ ] **Step 7: Review checkpoint**

Run `git diff --check`; manually recompute one fixture group and leave changes uncommitted.

## Task 7: Run bounded pilots, then the five-run comparison

**Files:**

- Modify: `benchmarks/cxl_compare/experiment.json`
- Modify: `benchmarks/cxl_compare/run.py`
- Create: `benchmarks/cxl_compare/README.md`

**Interfaces:**

- Produces pilot and measured bundles beneath `components/3FS/out/cxl-compare/`.
- Produces an immutable `resource-envelope.json` selected by feasibility only,
  before IO500 timing begins.
- A pilot verifies feasibility and evidence only; it never enters measured statistics.
- The standard profile starts only after tiny/easy/metadata/random-4K profiles each have one valid pilot for all compared variants.

- [ ] **Step 1: Run the transport pilot**

On `vlm-server`, from the approved superproject root:

```bash
python3 components/3FS/benchmarks/cxl_compare/run.py \
  --experiment components/3FS/benchmarks/cxl_compare/experiment.json \
  --output components/3FS/out/cxl-compare/transport-pilot \
  --suite transport --repetitions 1
```

Expected: valid 3FS phase-1 and phase-2 1-client/1-server diagnostics for all
declared sizes and queue depths. The LegoFS adapter either supplies a
contract-matched QEMU result or an explicit `not directly comparable`
capability record; it must not substitute the existing host-only fabric probe.
Add 2-client/1-server only after the first topology passes.

- [ ] **Step 2: Freeze the smallest feasible equal resource envelope**

The unmodified LegoFS runner currently fixes each guest at 5 vCPUs and 2 GiB.
For candidate client counts `1,2,4`, compute its aggregate client plus server
resources, allocate exactly those totals across the native 3FS client,
mgmtd/meta/storage/FDB roles, and run readiness plus a one-operation filesystem
probe only:

```bash
python3 components/3FS/benchmarks/cxl_compare/run.py \
  --experiment components/3FS/benchmarks/cxl_compare/experiment.json \
  --output components/3FS/out/cxl-compare/io500-pilot \
  --suite resource-feasibility --client-count-candidates 1,2,4 \
  --lock-resource-envelope
```

Choose the smallest client count for which every principal RF1 variant passes
within identical aggregate vCPU, configured RAM, storage and CXL capacity. This
selection observes only feasibility, never throughput. Write the chosen
per-role allocation, commands and artifact hash atomically to
`io500-pilot/resource-envelope.json`. If none passes, preserve all failures,
mark cross-system RF1 `not directly comparable`, and run only separately
labeled native-envelope diagnostics.

Here client count means both MPI ranks and independent client QEMU guests for
all principal variants. Freeze rank-to-guest placement, CPU affinity, MTU,
QEMU socket/multicast parameters and MPI/Hydra argv. Phase 1 classifies that
network as benchmark control; the phase-2 comparison profile exposes only its
restricted `mpi0` counterpart. If those harness contracts differ from the
unmodified LegoFS run, emit no ratio. Count shared logical CXL capacity once
and record guest aperture count separately.

- [ ] **Step 3: Run IO500 pilots in increasing cost order**

```bash
python3 components/3FS/benchmarks/cxl_compare/run.py \
  --experiment components/3FS/benchmarks/cxl_compare/experiment.json \
  --output components/3FS/out/cxl-compare/io500-pilot \
  --suite io500 --profiles easy-smoke,metadata-smoke,rnd4k,tiny \
  --resource-envelope components/3FS/out/cxl-compare/io500-pilot/resource-envelope.json \
  --repetitions 1 --qualification-only
```

Expected: each variant completes the stage-specific functional assertion and
CXL evidence gate for every qualification profile. These semantic-smoke
bundles are labeled `qualification_only=true`, expose no admitted performance
sample and never count toward the five measured blocks. Any failure is retained
and repaired before a clean-valid profile is attempted.

- [ ] **Step 4: Reproduce rejection of the known failed LegoFS run**

Run:

```bash
python3 components/3FS/benchmarks/cxl_compare/validate.py \
  --variant legofs-cxl-1s \
  --result target/results/legofs-io500/placement-route-restore-v118-10c1s-tiny-r2/result.json
```

Expected: nonzero exit and reasons for timeout, invalid filesystem and absent guest-visible CXL evidence.

- [ ] **Step 5: Run five valid interleaved repetitions**

```bash
python3 components/3FS/benchmarks/cxl_compare/run.py \
  --experiment components/3FS/benchmarks/cxl_compare/experiment.json \
  --output components/3FS/out/cxl-compare/measured \
  --suite transport,io500 \
  --profiles scc,standard \
  --resource-envelope components/3FS/out/cxl-compare/io500-pilot/resource-envelope.json \
  --target-valid-blocks 5 --max-block-attempts 10 --resume
```

The runner rotates variant order. It gives every cold IO500 slot fresh backing
and zero warm-ups; for preconditioned transport slots it performs each declared
warm-up inside that slot's live guest cohort. It records warm-up and measurement
counter snapshots around a common marker, records all failures and continues
only when cleanup proves no owned QEMU process remains. A failed slot invalidates its whole block;
the runner appends a complete replacement block until five complete valid
blocks exist or the frozen attempt limit is reached.
It schedules transport slots only for variants whose capability record supports
that case; a capability-based omission is not counted as a valid run and is
never converted to zero.

- [ ] **Step 6: Run RF2/RF3 as separate 3FS scaling groups**

Use separate experiment group IDs and do not generate LegoFS ratios for them:

```bash
python3 components/3FS/benchmarks/cxl_compare/run.py \
  --experiment components/3FS/benchmarks/cxl_compare/experiment.json \
  --output components/3FS/out/cxl-compare/measured-rf2 \
  --suite io500 --variants 3fs-cxl-p1-rf2,3fs-cxl-p2-rf2 \
  --profiles scc,standard \
  --target-valid-blocks 5 --max-block-attempts 10 --resume
python3 components/3FS/benchmarks/cxl_compare/run.py \
  --experiment components/3FS/benchmarks/cxl_compare/experiment.json \
  --output components/3FS/out/cxl-compare/measured-rf3 \
  --suite io500 --variants 3fs-cxl-p2-rf3 \
  --profiles scc,standard \
  --target-valid-blocks 5 --max-block-attempts 10 --resume
```

The runner derives and hashes the factor-specific topology before the first
slot and reuses that immutable hash for every repetition. Require the same
workload/configuration controls and completion class within each scaling group.
RF3 has no P1 row because it is an additional P2 scaling result in the frozen
variant list; it receives no paired speed ratio unless a P1 RF3 variant is
explicitly added in a future manifest.

- [ ] **Step 7: Validate the full ledger**

Run:

```bash
python3 components/3FS/benchmarks/cxl_compare/validate.py \
  --ledger components/3FS/out/cxl-compare/measured/ledger.jsonl \
  --require-complete-valid-blocks 5
```

Expected: exit zero only when every scheduled/reported compatible cell has five
complete valid pairing blocks; no incomplete block contributes to a principal
median or ratio, and no accepted
run references a failed result, changed artifact hash, forbidden transport,
incomplete cleanup or mismatched operation/semantic class. Capability-based
`not directly comparable` records remain visible but are not counted as runs.

- [ ] **Step 8: Review checkpoint**

Preserve all pilots and failures, run `git diff --check`, and leave changes uncommitted.

## Task 8: Generate an evidence-linked comparison report

**Files:**

- Create: `benchmarks/cxl_compare/report.py`
- Modify: `benchmarks/cxl_compare/README.md`
- Create at runtime: `out/cxl-compare/measured/report.md`
- Create at runtime: `out/cxl-compare/measured/summary.csv`
- Create at runtime: `out/cxl-compare/measured/summary.json`

**Interfaces:**

- Produces a report with methodology, validity table, transport results, IO500 results, resource cost, semantic limitations and links/hashes for every source bundle.
- Every plotted/summary cell carries valid-run count, median and relative MAD.
- Failed-run appendix is generated from the ledger but excluded from performance tables.

- [ ] **Step 1: Add report-shape tests to aggregate fixtures**

Require these exact sections:

```text
Platform and artifact identity
Validity and exclusions
Transport diagnostics and comparability
IO500 RF1 comparison
3FS replication scaling
Completion and durability semantics
Resource accounting
Limitations
Failed-run appendix
```

- [ ] **Step 2: Generate report files atomically**

Write to a new sibling temporary file, `fsync`, then rename. Refuse to overwrite a report whose input ledger hash differs unless `--new-report-id` is supplied.

- [ ] **Step 3: State claims precisely**

The title and conclusion must say the results are functional QEMU/TCG plus CXLMemSim measurements. If no configuration reaches five valid runs, state `insufficient valid repetitions` and omit its ratio rather than extrapolating.

- [ ] **Step 4: Generate the final report**

Run:

```bash
python3 components/3FS/benchmarks/cxl_compare/report.py \
  --ledger components/3FS/out/cxl-compare/measured/ledger.jsonl \
  --output components/3FS/out/cxl-compare/measured
```

Expected: `report.md`, `summary.csv` and `summary.json` agree on valid counts and medians; every result row points to an immutable bundle hash.

- [ ] **Step 5: Perform the final audit**

Run from the superproject root:

```bash
python3 -m unittest discover -s components/3FS/tests/benchmarks -p 'test_compare_*.py' -v
git -C components/3FS diff --check
git -C components/3FS status --short
git -C components/legofs status --short
```

Expected: comparison tests PASS, 3FS changes remain uncommitted, and LegoFS has no new source modification caused by this work.

## Evaluation completion gate

The evaluation is complete only when:

- every reported principal configuration has at least five complete valid
  interleaved pairing blocks; a valid slot in an incomplete block is diagnostic
  only;
- no qualification-only semantic-smoke result contributes a performance value;
- the common gate excludes the known failed LegoFS bundle and every injected negative case;
- transport parameters and platform/artifact identities match within each comparison group;
- every cross-system transport ratio has a passing operation-contract
  capability record; otherwise the report contains only separate diagnostics;
- RF1, RF2 and RF3 and all completion classes remain visibly separate;
- medians and relative MAD values reproduce from the immutable ledger;
- all claims are labeled as QEMU/TCG plus CXLMemSim evidence, not physical CXL performance;
- no source outside `components/3FS` was changed and no commit was created.
