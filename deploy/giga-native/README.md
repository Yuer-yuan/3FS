# Giga native 3FS-CXL storage scaling

This runner supports `{1,2,3}c{1,2,4}s`: C is the number of independent FUSE
clients; S is the number of native `storage_main` processes. Each run creates a
new, empty cluster. It does not migrate an existing cluster or rebalance targets.
The server binaries and the public RISC-V configuration generator are unchanged.

## Deployment contract

All cases use RF=1, four targets, four single-member chains, chain table 1,
524288-byte chunks, and stripe size 1. A file is not automatically striped over
all servers. The target/chain IDs remain `1000001001`, `1000002001`, `1000003001`,
and `1000004001`. For zero-based global target i, the owner is node
`10000 + i % S` and the local disk index is `i // S`.

All targets explicitly use the upstream new chunk engine: `create-target
--use-new-chunk-engine` sets the physical target's `only_chunk_engine=true`.
The runner reads each actual `target.toml`, rejects a false/missing value, and
archives it under `storage-targets/`. This is a target-level choice, not a global
`storage_main.toml` switch. It applies equally to 1S, 2S and 4S; old results with
unknown or different engine selection are not comparable baselines.

| S | Node 10000 | Node 10001 | Node 10002 | Node 10003 | Server physical cores |
|---|---|---|---|---|---|
| 1 | T1,T2,T3,T4 | — | — | — | 6 |
| 2 | T1,T3 | T2,T4 | — | — | 8 |
| 4 | T1 | T2 | T3 | T4 | 12 |

Each storage gets two physical cores. FDB, fabric, mgmtd and meta each get one
additional core. Per-process worker and buffer settings stay the same; aggregate
resources increase with S. This is not a fixed-total-resource comparison.

| Role | Node ID | CXL endpoint | Data/control port | CPU | LLC |
|---|---|---|---|---|---|
| FDB | — | — | TCP 4500 | 18 | 3 |
| Fabric | — | 1 | 12499 | 19 | 3 |
| mgmtd | 1 | 2 | 12501 | 20 | 3 |
| meta | 50 | 3 | 12502 | 21 | 3 |
| storage | 10000 | 4 | 12503 | 22,23 | 3 |
| storage-1 | 10001 | 5 | 12504 | 16,17 | 2 |
| storage-2 | 10002 | 6 | 12505 | 10,11 | 1 |
| storage-3 | 10003 | 9 | 12506 | 4,5 | 0 |
| admin | — | 7 | 12507 | Existing policy | — |
| client-0/1/2 | — | 16/17/18 | 12516/12517/12518 | 1/7/13 | 0/1/2 |

All CXL addresses use `127.0.0.1`. Endpoint 8 remains reserved for monitor.
Core retains its separate TCP listener with configured port 0; the result records
actual process-owned listening sockets and a successful Core `get-config` for
each storage. Extra storages share LLC with some clients; this is a single-host
deployment, not evidence of cross-host CXL scaling.

Startup also waits for meta to finish installing the routing version containing
the published chain table. A healthy target list in mgmtd alone is insufficient:
meta's independent periodic refresh can lag it. The barrier observes the native
refresh completion logs, excludes discarded views, and keeps the original
refresh period. Its versions and waiting time are recorded in `result.json`.

The transport region remains 1 GiB on memory node 1, with 256 lanes, queue depth
8, 65536-byte cells and 256 allocation slots per endpoint. `adaptive` polling
remains the default. RPC, serialization, buffer copies, checksums, replication,
fsync/commit, retries, GC and FUSE cache settings are unchanged. The CXL region
holds transport data; the backend remains fallocate-capable `/tmp` tmpfs. These
tests do not establish power-loss durability.

1S retains its old manifests and complete rendered config bytes for identical
inputs. Multi-S uses the original manifest builder with explicit native storage
roles, independently of RF. Each storage receives its own `--app_cfg`,
`--launcher_cfg` and `--cfg`; explicit local configs prevent a common mgmtd
configuration from replacing node-specific settings.

## Run on giga

From `/root/cxlmemsim-riscv-io500`, first validate the host:

```bash
python3 components/3FS/deploy/giga-native/storage_smoke.py \
  --repo "$PWD" --topology 2c4s --preflight
```

Run the correctness ladder in order: `2c1s`, then `2c2s`, then `2c4s`.
Only proceed after the preceding run passes; give every attempt a new run ID.

```bash
python3 components/3FS/deploy/giga-native/storage_smoke.py \
  --repo "$PWD" \
  --build-manifest "$PWD/target/results/giga-native-3fs/build-manifest.json" \
  --topology 2c2s --run-id storage-scale-smoke-2c2s-r1
```

The smoke writes 64 deterministic files of 4 MiB, split between two independent
clients. Each file is fsynced and closed; after all writes complete, the opposite
client opens it and validates its complete SHA-256. The per-file deadline is
120 seconds and each read/write stage has a 600-second deadline. It preserves
normal file placement and fails if any of the four chains was not covered.
For each storage, an actual stored chunk is read through `query-chunk --read`
with checksums enabled. No throughput threshold or speedup claim is used.

The 64 `stat --display-chunks` queries use the CLI's native semicolon batch in
one admin process. Every command marker and every individual result is checked,
including early-stop cases where the CLI can return zero. This avoids creating
64 short-lived admin attachments purely for evidence collection. A separate
1S attempt with one process per query exposed a CXL send failure during repeated
short-lived admin queries; its root cause is not yet confirmed. That failure is
retained and is not claimed fixed by this adaptation.

After all smoke cases pass, run the existing bounded IO500 profile in order:
`3c1s`, `3c2s`, `3c4s`.

```bash
scripts/run_giga_native_3fs_io500.sh \
  --topology 3c2s --run-id storage-scale-bounded-3c2s-r1
```

The original 22 phases, one-second stonewall and verifier remain unchanged.
Results retain `official=false` and `expected_invalid=true`: this is compatibility
validation, not an official IO500 score. Per-phase server balance is not measured
by process-lifetime counters; do not infer uniform participation from aggregate
results. Actual IOR logs report single-shared-file for hard/random phases and
three file-per-process files for easy phases. Under stripe size 1, their foreground
file data can use at most one or three storage nodes, respectively. These are
layout-derived upper bounds, not measured per-phase transport counts.

## Evidence and failures

Each result lives at `target/results/giga-native-3fs/<run-id>/`:

- `result.json`: first failure, topology, placement, node readiness, live Core
  configs, process-lifetime transport counters and teardown status.
- `manifest.json`, `config/`, `rendered-config.json`: exact inputs, hashes and
  storage-only adaptation checks. The original binary build identity is retained
  separately from the current Python deployment hashes.
- `processes/`, `snapshot-before-io.json`, `snapshot-after-io.json`: owned process
  identity, CPU placement, thread affinity, CPU time and RSS. Command records
  include child CPU usage and the cumulative child peak RSS, not per-command RSS.
- `logs/admin-list-targets.log`, `logs/admin-list-chains.log`,
  `actual-chain-table.csv`: the complete four-target RF1 placement proof.
- Smoke: `smoke-write-*.json`, `smoke-read-*.json`,
  `smoke-file-placement.json` and `logs/admin-smoke-backend-*.log`.
- IO500: unchanged phase records, rank receipts and verifier output.

Every storage must have one matching PID/session/manifest/endpoint transport
record, positive CXL RPC and positive bulk pull+push activity. Bulk read/write
counters refer to transport pull/push, not directly to file reads/writes. RDMA
opens, TCP data-plane bytes and bootstrap serving fallback must all remain zero.

Startup failure uses the same reverse-order, receipt-checked teardown as normal
completion. It checks all owned processes, mounts and fixed ports and retains
the original failure if cleanup also fails. Successful runs remove only their
owned CXL/tmpfs backings. Failed backings remain for diagnosis; no global process
kill, unrelated directory cleanup or compressed backup is part of this runner.

## Tests

```bash
python3 -m unittest discover -s components/3FS/tests/giga_native -p 'test_*.py'
python3 -m unittest discover -s components/3FS/tests/cxl_riscv -p 'test_make_phase1_manifest.py'
python3 -m unittest discover -s components/3FS/tests/cxl_riscv -p 'test_full_stack.py'
git -C components/3FS diff --check
```

The frozen 1S fixture was captured from commit `6197e74dce6a38ee691b23a7281c7b8ae6c6fc72`
before adaptation. It stores full manifests and hashes of every rendered file,
including the token hash, without displaying token contents.

## Validated on 2026-09-11

The final source passed all 87 local regression tests and the following ordered
giga checks, using the same previously attested binaries:

| Check | 1S | 2S | 4S |
|---|---|---|---|
| 2-client content/placement/backend smoke | Passed | Passed | Passed |
| Actual files per storage (64 total) | 64 | 32 / 32 | 16 / 16 / 16 / 16 |
| 3-client bounded IO500 + verifier | 22/22 | 22/22 | 22/22 |
| New chunk engine / CXL-only serving / teardown | Passed | Passed | Passed |

Result bundles are under `target/results/giga-native-3fs/`, with the prefix
`storage-scale-`. Final smoke IDs end in `2c1s-r5`, `2c2s-r2`, `2c4s-r2`; final
IO500 IDs are `storage-scale-bounded-20260911-3c{1,2,4}s-r1`. The consolidated
`storage-scaling-adaptation/report.md` records provenance, failures, fixes,
phase-layout limits and exact cleanup evidence. The separate short-lived-admin
send failure remains documented as unresolved. No code was committed or pushed.
