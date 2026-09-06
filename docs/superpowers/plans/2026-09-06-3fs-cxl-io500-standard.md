# 3FS CXL 10c1s IO500 Standard Implementation Plan

> **Execution constraint:** Execute task-by-task in this session with one agent.
> No delegation or commits. Existing user approval covers this continuation.

**Goal:** Finish phase 1 with an actual ten-client/one-server standard IO500
measurement, valid official verification, and closed timing/root-cause evidence.

**Architecture:** Reuse `run_phase1.py` platform ownership, G0 binding, DAX,
transport and retirement gates, and `full_stack.py` service startup/cleanup.
Add a separate standard workload and measured result class. Stage a private,
hash-verified IO500/MPICH/musl bundle alongside the existing glibc 3FS runtime.
MPI uses Hydra manual launch on the existing isolated guest network; POSIX
operations use each guest's real 3FS FUSE mount.

**Tech Stack:** RISC-V Linux, QEMU/CXLMemSim, 3FS CXL, FoundationDB 7.3.63 TCP,
IO500 a69cf60cf76538a34c1332bc448838cf9a560a9b, MPICH 4.3.2, Python, C++.

**Spec:** `docs/superpowers/specs/2026-09-04-3fs-cxl-transport-design.md`

## Global constraints

- Only modify `components/3FS`; reference parent benchmark sources read-only.
- Keep Core, FoundationDB and bootstrap TCP. No phase 2 or cross-system comparison.
- No commits, platform changes, host configuration changes or evidence deletion.
- Preserve queue depth 8/cell bytes 65536, CXL ON/RDMA OFF and all fail-closed gates.
- Ten independent client guests, one server guest, one MPI rank per client, RF1.
- Copy `configs/io500-standard.ini` from the parent as the reference; only rewrite
  `/badfs` mount-root paths to `/mnt/3fs`. Keep 300-second stonewall and every
  built-in phase/default. Do not reduce transfers, skip phases or accept INVALID.
- Require all 13 scored phases once, positive finite reported durations, and
  the existing standard gate of at most 600 seconds per phase/window.
- No CLOCK_REALTIME steps during measured work. Preserve guest and host monotonic
  clocks separately, timestamp anchors and observed skew/error bounds.
- The accepted 2 MiB direct-I/O cohort is qualification, not this measurement.
- Retain every failed run, exact first failure and exact-owned cleanup receipts.

## Task 1: Repair operation latency evidence

Files: `src/common/monitor/Recorder.cc`, `tests/common/monitor/TestMonitor.cc`.

- [x] Reproduce empty `Guard::latency()` after report for both recorder types,
  tagged/untagged and success/failure. Tests also require first-completion
  stability and dismissed guards to retain no completion time.
- [x] Store the already computed monotonic duration with `latency_ = latency;`
  before setting `reported_ = true`.
- [x] Run native `tests/test_common --gtest_filter='TestMonitor.*'`; preserve
  red/green logs. Include this fix in the next source closure and RISC-V build.

## Task 2: Stage an attributable benchmark bundle

Files: create `deploy/cxl-riscv/prepare_io500.py`; extend
`deploy/cxl-riscv/stage_full_rootfs.py`; test `tests/cxl_riscv/test_io500_standard.py`.

- [ ] Implement `prepare_io500.execute(reference_root: Path, output: Path) -> Path`
  to verify pinned clean IO500/IOR/pfind/MPICH sources and the existing manifest's
  benchmark hashes, copy only io500, io500-verify, Hydra and MPI/musl libraries,
  and emit source/artifact/runtime provenance. Reject changed or forbidden ELF
  dependencies. Never copy LegoFS preload/exporter or alter reference inputs.
- [ ] Stage verified files under `/opt/io500`; resolve the musl interpreter
  without replacing the 3FS glibc loader. Include all files and the bundle
  manifest hash in rootfs identity and the G0 binary closure.
- [ ] Test hash tampering, source pin drift, dependency escape and mixed-loader
  resolution. Exercise actual ELF closure with the remote artifacts.

## Task 3: Run the standard workload through the existing cohort

Files: create `deploy/cxl-riscv/io500_standard.py` and guest rank/probe tools;
modify `full_stack.py`, `run_phase1.py`; extend `tests/cxl_riscv/test_io500_standard.py`.

- [ ] Give `full_stack.execute` an optional workload callback receiving the
  consoles, bounded command function and run directory; keep existing
  qualification and service ownership/cleanup unchanged.
- [ ] Add explicit `--workload io500-standard` selection to `run_phase1.py`.
  Factor shared cohort validation from qualification-specific file checks;
  standard validation consumes the same shared proof plus IO500 evidence.
- [ ] Implement manual Hydra launch with exactly proxy IDs 0..9, one rank per
  client, rank/hostname markers, and checked MPI/proxy exits. Use process-local
  musl library paths and no LD_PRELOAD. Preserve launch commands and output.
- [ ] Run a rank placement/clock probe before standard release. Sync wall clocks
  before FDB starts; never step them later. Record before/after realtime,
  monotonic and host request/response intervals on all eleven guests.
- [ ] Parse streaming phase results and retain host monotonic observation times;
  fail on duplicate/missing phases, INVALID, nonfinite/zero durations, 600-second
  phase overruns or a failed guest. Tests inject each failure and fragmented lines.
- [ ] Export raw result/config/stonewall/stat artifacts through plain POSIX reads
  of the real FUSE mount, with byte counts and SHA-256. Run paired official
  `io500-verify`, require rc=0 and `[OK]`, preserve full verifier output and
  official score/bandwidth/metadata values without recomputing a substitute.

## Task 4: Execute and close actual failures

- [ ] Run focused Python/native checks, freeze new source closure and full
  RISC-V build, stage a fresh rootfs with the benchmark bundle, and pass G0-B
  with matching app/runtime/bundle hashes.
- [ ] Run the unchanged 10c1s standard cohort on the approved remote platform.
  Observe bounded progress and retain failure evidence immediately. If it
  fails, isolate the first failing operation/phase using 3FS timing and a
  minimal reproducer, fix the root cause within 3FS, run its regression and
  repeat the standard test. Do not inflate gates or shrink the benchmark.
- [ ] Require 13 valid phases, official verifier success, positive CXL RPC/bulk
  counters, zero forbidden serving traffic, independent guest/rank placement,
  valid time windows, BI handoffs and clean exact-owned retirement.
- [ ] Record raw source/config/result/verifier/timing hashes, official scores,
  all phase times, host elapsed window, clock uncertainty and known limits in
  the result bundle and owning spec. Correct completion only after these gates
  pass; preserve simulation-only scope. Mark the user goal complete and stop.

## Current evidence

### First actual standard write failure and next bounded correction

Build 031/source closure 033/rootfs full-024 passed G0-B at
`out/cxl-riscv/g0-b/full-024/20260905T232610.315987Z-2244857/result.json`
(SHA-256 `6abccb5372308f51b0a46245ad4ce8dc2bcc2906ef74743ba499787bc612d39d`).
The subsequent standard cohort is
`out/cxl-riscv/phase1/io500-standard/20260905T233216.389204Z-2244857`.
It passed all twenty DAX directions, local TCP FDB, and the real ten-rank MPI
probe (9.226496912 s host elapsed; ten proxy exits zero). All ten FUSE mounts
and actual standard rank wrappers started. No scored phase completed.

The first standard write fails: IOR reports EIO on a 2097152-byte write at
offset zero and aborts in POSIX_Xfer. Guest 7's offline FUSE log records a
1048576-byte FUSE request split into two 524288-byte storage operations. Its
first batchWrite times out after 120046 ms, a retry sees ChannelIsLocked, and
the user call gives up after 243837 ms. The matching server log shows the
same client/request/channel/chunk identity still locked. FDB's preserved trace
has no failed_to_progress or storage-server-list fetch timeout events.

`bulk-trace-timing.json` indexes actual post-launch coherence requests by guest
and owner arena: server reads of client6/endpoint22's first 512 KiB do not
start until 319.112813293 s after launch and finish at 401.182195456 s. This
places its first data pull after its client timeout. The storage pool grants
only two 4 MiB small slots; each independent handleUpdate holds an entire slot
for a 512 KiB operation until forwarding/commit finishes. The separate 8 MiB
big slot cannot serve these small requests. Code also unconditionally copies
every bulk write back into a server-owned CXL export even at a chain tail.

The failed workload was interrupted after recording the fatal IOR output in
`failed-workload-abort.json`. The signal arrived during application cleanup;
do not claim clean application retirement or complete coherence validation.
All eleven QEMU processes and the simulator subsequently exited zero and
their exact owned PIDs disappeared. Keep the original failed result and all
offline logs; later shutdown vda errors are not evidence of the initial cause.
The host filesystem still had 80 GiB available after failure.

Next changes, within phase 1 and with no benchmark or timeout relaxation:

- [x] Add fragmented-output regression and immediate fatal IOR read/write or
  assertion detection. Preserve the first line, host monotonic timestamp and
  raw workload output before cleanup. Local 17-test suite passes; red/green
  logs are `full-deps/io500-fatal-watchdog-{before,after}.log`.
- [x] For the ten-client deployment only, use 24 small 512 KiB slots and one
  4 MiB large slot, retaining exactly 16 MiB total. The twenty initial chunk
  operations can then acquire independent slots without the previous two-slot
  admission bottleneck. Preserve G0's existing single-client pool profile.
- [x] Add a real CXL storage regression proving a tail bulk write/read creates
  no forwarding allocation. Move DRAM-to-CXL export creation to the actual
  forwarding path after successor/sync decisions, preserving the export lease
  for the RPC and the stable local bytes across retries. Exercise multi-replica
  and syncing regressions as well as the tail case.
- [ ] Rebuild, freeze, pass matching G0-B and rerun the unchanged standard.
  Queueing and unnecessary-copy findings are established; a successful standard
  measurement and performance sufficiency are still unproven.

The tail regression fails on the old server with allocation generations
`{0, ...}` versus `{1, ...}` while its 512 KiB write/read checksum succeeds:
`full-deps/tail-export-before-02.log`. The earlier test implementation treated
never-published zero slots as corrupt; its failed log is retained separately.
An initial after-build run still used an old exec helper because aggregate
`test_storage` lacked a `cxl_storage_test_helper` dependency. Add that explicit
dependency, preserve `tail-export-after-stale-helper.json`, and do not treat
that run as validation of the changed server. The correctly rebuilt helper and
test binary hashes are in `tail-export-after-binaries-02.json`. All six focused
tail/multi-replica/failure/sync tests then pass in 127.960 s in
`tail-export-after-02.log`. The local complete Python suite passes 135 tests in
`io500-python-local-05.log`. Both rendered single-client and ten-client pools
still total exactly 16777216 bytes; only the latter changes allocation units.

The first real timing regression failed both recorder types as expected:
`out/cxl-riscv/full-deps/operation-latency-before-02.log`. The earlier invocation
used an incorrect `bin/test_common` path and is retained separately; the test
binary lives at `build/cxl-no-rdma-host/tests/test_common`.

Functional source closure 029/build 027 cohort remains accepted at
`out/cxl-riscv/phase1/10c1s/20260905T203556.712060Z-2074312/result.json`.
Its reported 2 MiB checks and transport evidence do not satisfy standard IO500.

Monitor regression result: 5/5 enabled tests passed in 582 ms; one existing
duplicate-recorder abort test remains disabled. Evidence: `operation-latency-after.log`
and `operation-latency-after-build.log` under the same full-deps directory.

The first standard pipeline passed native all-target build, 129 Python tests
and RISC-V build 028/source closure 030, then stopped before launching any VM:
rootfs staging rejected an already-present musl loader. The base loader and
adopted runtime are byte-identical (SHA-256
`8c3e69a0bbca649dc16f04ad62a12205b14b0d7188125b849f5f8b9b5037234b`).
Reuse now requires that exact identity and an in-root path; a different loader
still fails. A regression checks both cases. The adopted loader also retains
its reference hash through debug stripping. Retain the failed `full-021` tree
and `io500-standard-pipeline.log`; no platform/application run occurred.

Build 029/source closure 031/rootfs full-022 passed G0-B at
`out/cxl-riscv/g0-b/full-022/20260905T223253.369786Z-2198937/result.json`;
its SHA-256 is `e33e6d3d0578ec62adccad44070e7d6bad4784f20f554bf53202a3a77379630b`.
Both 2 MiB write/read hashes are
`801de90e6a37fe04b27f885728d583e87cdebf05b049c37c1687722abf91bace`;
DD reports 45.716057 s write/fsync and 38.831540 s read. All G0 gates pass and
`cleanup-verification.json` finds no exact-run platform process remaining.
These are qualification times, not a standard score or a causal speedup claim.

The first standard cohort directory is
`out/cxl-riscv/phase1/io500-standard/20260905T224017.941562Z-2198937`.
It booted eleven guests but was explicitly aborted during DAX qualification,
before FDB, the 3FS services or MPI started. Source audit found that MPICH
ch3:sock's `MPIDI_CH3I_Sock_get_host_description()` falls back to `gethostname()`;
Hydra's `-iface eth0` does not configure that channel's peer names. The fresh
rootfs had no `/etc/hosts`, so those peer names could not resolve. Preserve
`pre-mpi-abort.json`, the failed result and all consoles; this is an aborted
preflight, not an executed standard benchmark. Staging now writes the exact
server/client hostname-address map into every fresh guest. A regression checks
all eleven maps. The real ten-rank MPI probe now runs after network setup but
before 3FS service startup, so network errors fail at their source.

Build 030/source closure 032/rootfs full-023 also passed G0-B:
`out/cxl-riscv/g0-b/full-023/20260905T225325.538094Z-2208637/result.json`,
SHA-256 `75e19747974781b3c14e8d2787137dfa40c2715c19501ff0cdd278a9f516a6e9`.
Guest operation latency samples (69 us, 203 us, 133 us, 192 ms 394 us,
130 ms 914 us) and their exact source-log hashes are retained in
`operation-latency-guest-evidence.json`. G0 cleanup is independently verified.

The second standard cohort is
`out/cxl-riscv/phase1/io500-standard/20260905T230026.071412Z-2208637`.
All eleven guests, twenty DAX directions, eleven initial clock receipts and
the local TCP FDB transaction passed. MPI preflight failed before any 3FS
service started: proxy 0's `HYDRA_LAUNCH` line immediately followed the idle
`hf3fs-g0-1# ` prompt, so an anchored parser recognized only proxies 1..9.
The same prompt handling is required for asynchronous probe/rank markers.
A regression reproduces that exact prefix and now passes after removing only
the runner's recognized prompt.

Failure cleanup also timed out because it waited for an unreaped zombie
session leader while the parent console was waiting for the cleanup command.
The native real-session regression reproduces the timeout with the original
stopper and passes when zombie/dead states are ignored. `/proc` reads now use
shell builtins, avoiding thousands of `awk` subprocesses in a QEMU guest.
Leader starttime/session identity checks and exact-session termination remain.
After the stopper returns, the console reaps its recorded job. Both red/green
logs are `out/cxl-riscv/full-deps/io500-console-cleanup-{before,after}.log`.
The failed cohort's eleven QEMUs and simulator exited zero and all recorded
platform PIDs are absent. Its benchmark phases have not run; retain every
failure receipt and console. The outer result now propagates the application
or preflight first failure directly as well as retaining the detailed record.

### Third standard cohort: systemic write-back latency and runner optimization

Build 032/source closure 034/rootfs full-025 passed the full source-bound G0
gate. The next standard cohort is
`out/cxl-riscv/phase1/io500-standard/20260906T010512.082711Z-2285320`.
It passed eleven guest boots, all twenty bidirectional DAX publications, the
machine-local TCP FDB transaction, a real ten-proxy MPI preflight, all 3FS
services, four targets and ten FUSE mounts. The unchanged standard workload
then failed in `ior-easy-write`; no scored phase completed.

All ten clients submitted their first 1 MiB FUSE write, split into two 512 KiB
storage operations, and all first storage RPC batches exceeded 120 seconds at
nearly the same time. Their retries saw `ChannelIsLocked` because the original
server requests were still active. Rank 4 emitted the first IOR fatal EIO after
the bounded client retry path gave up. The server pool had allocated the new
24 x 512 KiB plus 1 x 4 MiB geometry, so this is not the previous two-slot
admission failure. Mgmtd lease extension later stalled for more than five
minutes and FDB reported a transaction-too-old error as secondary saturation.
The complete 1.4 GiB coherence trace has no timeout, protocol, delivery or
server-copy error event. Its post-banner traffic contains roughly one million
cache-line protocol records and covers the entire RPC/bulk region, establishing
write-back simulator throughput as the current limit rather than a corrupt
endpoint, allocation or lane.

A write-through diagnostic was attempted only through the existing QEMU
command-line property. QEMU rejected startup with `CXL Type-3 Legofs coherence
proof requires write-back mode`; no platform source was changed and all owned
processes exited. Preserve
`out/cxl-riscv/g0-a/write-through-diagnostic-001/20260906T021219.328522Z-2307135`
as negative evidence and keep write-back enabled.

The next candidate removes test-orchestration delay without reducing the
standard workload or its gates:

- stage eleven root trees, create their images and wait for eleven independent
  guest boots concurrently;
- launch all ten FUSE clients before concurrently checking mount readiness;
- retain full 2 MiB devdax mappings for all twenty directions but touch and
  checksum exactly 65472 payload bytes plus the 64-byte publication header;
- validate every resident thread mask inside the guest and emit one aggregate
  record per process instead of hundreds of serial lines;
- compute the coherence trace SHA-256 during its required parse instead of a
  second full-file read.

The production-sized defaults created more than 300 resident service threads
on the three CPUs assigned to 3FS. The ten-client simulation profile now uses
bounded server/client thread pools (including four storage proc/io, four disk
read and four update workers) and restores the prior 10 ms CXL poll interval.
The 100 ms interval serialized RPC progress into multi-minute metadata delays;
the earlier 10 ms attempt ran before thread-pool isolation and was dominated by
guest scheduling. Single-client G0 and production defaults remain unchanged.
The updated CLI/config/evidence suite passed 136 tests locally and remotely.
Build 033/source closure 035/rootfs full-026 and the matching G0-B gate passed.
The resulting standard cohort is
`out/cxl-riscv/phase1/io500-standard/20260906T023808.100009Z-2308873`.
The measured orchestration costs were 0.723285 seconds for staging, 115.853625
seconds for eleven images, 6.194392 seconds for eleven parallel boots, and
69.058756 seconds for all twenty DAX handoffs. It stopped before FDB or any 3FS
service because two valid MPI receipts arrived contiguously on the shared
serial stream, without a newline between rank 6 and rank 2. The fixed-field
receipts themselves are complete and all ten proxies exited zero. Receipt
parsing now finds the exact marker fields independently of line boundaries;
a regression covers the preserved concatenated form. All eleven QEMUs and the
simulator exited zero and the failed cohort remains evidence, not a benchmark
result.

The image stage now uses a content-addressed base ext4 cache keyed by the
verified base-rootfs, binary, runtime, guest-tool and IO500 content hashes plus
image size. Each run verifies or creates
one base image, makes eleven independent sparse writable clones, injects only
the rendered per-node 3FS and IO500 files with `debugfs`, repairs and checks
each ext4 filesystem, and dumps every injected file back out for exact SHA-256
verification. The remote ext4 filesystem rejects reflink, so clones use sparse
copies rather than shared extents. Clone evidence binds the base hash and all
patch hashes without repeating an 8 GiB whole-image hash for every node. This
preserves independent writable roots while eliminating eleven repeated mkfs,
root-tree imports and full logical-image hashes. A new source-bound handoff can
therefore reuse byte-identical rootfs content, while any payload change creates
a new cache entry. The complete suite now passes 138 tests locally. Remote
timing and the unchanged standard rerun are pending.

### Fourth standard cohort: orchestration cache regression and trace bottleneck

Build 034/source closure 036/rootfs full-027 passed the matching G0-B gate at
`out/cxl-riscv/g0-b/full-027/20260906T030125.734555Z-2333971/result.json`.
The standard cohort is
`out/cxl-riscv/phase1/io500-standard/20260906T031242.901364Z-2333971`.
It passed eleven boots, all twenty DAX handoffs, the machine-local TCP FDB
transaction, the corrected ten-rank MPI probe, every service and all ten FUSE
mounts. The first `ior-easy-write` no longer produced a storage RPC timeout,
ChannelIsLocked retry or EIO, confirming the bounded thread-pool change removed
the previous hard failure. It nevertheless completed no scored phase within
the unchanged 600-second gate.

The retained 938556669-byte trace contains 1006294 coherence requests, 963498
snoop send/ack pairs, 496796 dirty completions, every required bidirectional
DAX handoff and zero protocol/error events. CXLMemSim formats JSON and flushes
an output stream for every 64-byte event. That synchronous trace path is the
remaining measured-I/O bottleneck. Cleanup ultimately reports all eleven QEMUs
and CXLMemSim exited zero; the first failure remains exactly
`IO500 phase window exceeded 600 seconds after 0 completed phases`.

The sparse-clone image cache is rejected as a performance optimization on this
remote ext4 filesystem. A cache hit still spent 27.803636 seconds hashing the
8 GiB base, and eleven sparse copies plus patch/check took 122.838038 seconds,
versus 115.853625 seconds for the previous full image stage. It preserves
correctness but does not reduce handoff time.

The replacement under test uses one content-addressed, mode-0444 base ext4,
QEMU `snapshot=on` temporary write layers and one 16 MiB read-only node config
disk. Cache hits validate an immutable inode/stat receipt without rereading 8
GiB. The guest copies the required config disk into its ephemeral root before
readiness. Both G0 and the eleven-guest cohort use this policy; CXL endpoint
backings remain eleven distinct files. Complete coherence output is drained
through a run-owned FIFO into a zstd archive, preserving every trace byte and
its uncompressed SHA-256 while removing per-event filesystem writes from the
measured path. Resident affinity scans use shell builtins instead of one `awk`
process per thread. Failure cleanup keeps dependency order but runs independent
guests in parallel, and counter/log collection is parallel per guest. The
local Python suite passes 140 tests. A new source-bound build, matching G0-B,
and unchanged standard rerun remain required before acceptance.

G0-B full-031 is the first passing true-CoW gate:
`out/cxl-riscv/g0-b/full-031/20260906T041856.615304Z-2375850/result.json`.
It uses one content-addressed immutable base image, two 16 MiB config disks and
QEMU temporary snapshots; both guests emitted `HF3FS_G0_CONFIG_READY`. The base
inode/stat receipt is identical before and after. The complete zstd trace is
11331561 bytes, its compressor and simulator exit zero, and trace validation
finds zero errors. The 2 MiB FUSE write/read hashes both equal
`2def55d8ff5adabfbf6068e996eb93e64c4ae2ad1c177adb07d3efe70d83788a`;
all services retire cleanly. The initial cache population is deliberately not
a cache-hit timing result.

Compression removes trace storage volume but does not remove simulator JSON
formatting: the G0 FUSE write still took 118.965970 seconds. Therefore the
measured eleven-guest cohort omits the optional per-event trace argument. It
still runs all twenty direction-specific DAX generation/checksum checks and is
bound to a matching, fully traced G0-B. At simulator exit it requires the exact
`COHERENCE_V2_STATS_JSON` schema, eleven registrations, positive GET/GETM,
model acknowledgements and dirty-data completions, zero timeouts/protocol/
delivery/server-copy failures, and zero active bindings. The result retains
the simulator log and SHA-256. This prevents the diagnostic JSON formatter
from determining the benchmark result while keeping aggregate protocol and
per-direction visibility evidence.

The first untraced phase cohort is
`out/cxl-riscv/phase1/io500-standard/20260906T044013.515789Z-2393109`.
It demonstrates the intended incremental costs: base-cache validation 0.000457
seconds, eleven config/CXL storage sets 1.371268 seconds, eleven parallel boots
7.736010 seconds and twenty DAX handoffs 61.620563 seconds. Final simulator
statistics contain eleven registrations, 73827 dirty completions and zero
errors/active bindings. Service startup, MPI probe, every FUSE mount and clean
owned-process teardown pass. The standard workload did not start because the
new shell-builtin affinity command was 1380 characters and the emulated UART
lost the last two characters; the shell parsed final `done` as `do` and never
emitted the command receipt. The run was ended by terminating only its recorded
node0 QEMU; the runner then cleaned all eleven QEMUs and simulator with zero
return codes.

The replacement affinity command is 691 characters and launches one BusyBox
awk per resident process (five total), passing all `/proc/PID/task/*/status`
files in one invocation. It retains leader presence, positive thread count and
every-thread mask validation while avoiding both the long UART line and the
former one-process-per-thread cost.

### Fifth through seventh cohorts: lease lifetime, fabric scheduling, and metadata diagnosis

The matching full G0 gate subsequently passed at
`out/cxl-riscv/g0-b/full-032/20260906T042947.125630Z-2385274/result.json`.
The standard cohort
`out/cxl-riscv/phase1/io500-standard/20260906T071102.712773Z-2471827`
passed all platform, service, FUSE, MPI and affinity prerequisites and reached
the official IO500 banner. It completed no scored phase within the unchanged
600-second phase window. Its final coherence record is complete with eleven
registrations, 697357 dirty-data completions and zero timeout, protocol,
delivery or server-copy failures. Cleanup later observed `Mgmtd::NotPrimary`:
the single primary's 900-second lease was shorter than this TCG cohort's
multi-hour wall-clock lifetime. The simulation profile now keeps lease
validation enabled but sets the lease, timestamp window and heartbeat failure
interval to 43200 seconds.

The next cohort,
`out/cxl-riscv/phase1/io500-standard/20260906T075534.348520Z-2490692`,
pinned the fabric process to CPU 0 beside the local TCP FDB. All ten FUSE
mounts then made no progress while the server guest consumed approximately one
CPU. The run was terminated early after this bounded diagnostic was conclusive;
all eleven QEMUs and the simulator still exited zero. The fabric process now
may schedule on CPUs 0-3, while mgmtd, meta and storage retain fixed masks 1, 2
and 3. This restored all ten mount completions in the following cohort.

`out/cxl-riscv/phase1/io500-standard/20260906T081055.373686Z-2503731`
passed all ten mounts, the MPI probe and resident affinity checks. Rank 0 then
spent approximately 461 seconds attempting its first FUSE metadata mutation
and IO500 exited 221 with `Remote I/O error` while creating
`/mnt/3fs/io500-standard-results/`. No bulk write began. All transport and
coherence summaries are clean: eleven registrations, 207591 dirty-data
completions and zero timeout, protocol, delivery or server-copy failures. The
current fault is therefore isolated to the FUSE-to-Meta RPC path, before the
standard IOR data phase. Shutdown logging displaced the causal MetaClient
message from the former 100-line tails. The runner now captures 1000 lines from
every guest log before teardown on failure and performs a bounded cross-client
FUSE metadata preflight (create on client 0, observe on client 1, remove on
client 0) before the standard workload. These checks do not change the IO500
configuration, phase list, stonewall duration or acceptance windows.

### Incremental rootfs payload cache

The image pipeline now caches the fully staged rootfs payload as well as the
immutable ext4 base. Its content identity covers the base manifest, every
application binary, runtime library, guest tool, IO500 bundle, init script and
strip tool. A hit verifies the cached binary/runtime/init hashes, creates a
directory symlink for the new source-bound staging record, and writes fresh
provenance. It never mutates the cached payload. The first remote hit reused
`full-043` from `full-045` with payload identity
`739b8fcb4a32f163950ca69ebf554efa44f36781e1a6e122be823269bd24848d`;
rootfs staging fell from 40.486079 seconds to 11.921313 seconds. Together with
the immutable base image and QEMU temporary snapshots, this removes repeated
mkfs, root-tree population and 8 GiB cloning from each candidate run. The
remaining hit cost hashes 24 binaries and 67 runtime files before reuse. The
local and remote CXL/RISC-V Python suites pass 150 tests after adding the
failure-snapshot and metadata-preflight regressions.

The next cohort,
`out/cxl-riscv/phase1/io500-standard/20260906T090430.854253Z-2523981`,
proved that this is a server scheduling failure rather than a missing CXL
request. After all ten mounts passed, a fresh server-local `admin_cli mkdir`
sent 77 CXL responses but timed out. The Meta log later processed multiple
copies of the same request UUID, first reporting FDB error 1007 (transaction
too old), then repeated FDB error 1020 conflicts and request timeouts. The
single-CPU Meta mask was therefore allowing its 57 threads and background
management work to build a long queue; retrying the identical mutation then
created a conflict storm. The TCG cohort now reserves CPU 0 for the local TCP
FDB and lets fabric, mgmtd, meta and storage schedule across CPUs 1-3. Meta's
management refresh/heartbeat/extension cadence is ten seconds, reducing
repeated immutable-config traffic while retaining the 43200-second lease.
The one-off admin diagnostic has been removed, and failure snapshots now
select causal warnings with short raw tails so a UART dump cannot add another
minute to teardown.

The corrected scheduling policy was exercised by
`out/cxl-riscv/phase1/io500-standard/20260906T092548.066543Z-2532717`.
All services and all ten mounts passed; the first client completed the new
metadata `mkdir`, and a second client observed it. This is the first direct
cross-guest proof that the formerly timing-out Meta mutation completes under
the shared CPU 1-3 mask. The preflight then stopped only because the minimal
guest rootfs has no standalone `rmdir` command (shell status 127). The runner
now invokes the existing BusyBox applet explicitly. Failure capture is also
bounded across all logs as one 300-line causal tail plus ten-line service
tails, because per-file 300-line tails could still saturate node 0's emulated
UART even after removing generic warning matches.

`out/cxl-riscv/phase1/io500-standard/20260906T093611.635322Z-2542492`
then passed all platform checks, ten mounts, all three metadata preflight
operations, affinity validation and ten-rank placement. It emitted the fixed
standard IO500 banner but `ior-easy-write` produced no result within the
600-second gate. Final coherence was complete with zero protocol/delivery/
copy errors and 358026 dirty completions. Client transport records contained
no fatal storage error. Relative to pre-workload cohorts, the coherence delta
is approximately twenty 512 KiB pulls: the first 1 MiB FUSE request from every
rank reached storage, while the next request did not begin before the gate.
The storage profile still admitted only four write jobs at a time, making the
twenty initial chunk commits run in five long TCG waves. The ten-client profile
now uses ten request/IO threads, twenty write workers and four update coroutine
threads. Its buffer geometry remains exactly 16 MiB (24 x 512 KiB plus one
4 MiB slot). This changes scheduling capacity only; IO500's profile, transfer
sizes, phase list, stonewall and validation gates remain unchanged.

The cohort also exposed avoidable failure-handoff work. The runner formerly
scanned whole logs before filtering and then printed another 100 lines per log
after service teardown. Failure capture now tails bounded service-specific
files before filtering, uses a supported BusyBox process listing, and skips
the duplicate raw-log pass. On failed MPI runs, the coordinator is stopped
before its colocated proxy so the client-0 console remains available for
evidence.

`out/cxl-riscv/phase1/io500-standard/20260906T101214.614649Z-2578274`
uses the larger storage worker profile and again passes all prerequisites. Its
coherence summary is complete with eleven registrations, 425142 dirty-data
completions and zero timeout, protocol, delivery or server-copy failures. The
600-second watchdog still recorded no completed phase, but the pre-shutdown
Meta snapshot shows IO500 had already advanced to creating `ior-hard`. The
official result lines were therefore retained in libc/Hydra buffering rather
than absent, so the host watchdog killed a progressing job. The benchmark
bundle now adds a source-bound, musl PTY relay in front of the unchanged pinned
IO500 executable. A PTY makes libc emit each result line immediately; the
relay performs no preload or benchmark-source modification, creates no new
session, and returns the exact child status. This preserves owned process-group
cleanup and makes the existing per-phase 600-second gate observe real
progress. Failed cohorts also use lazy FUSE unmount followed by owned-process
termination, and causal snapshots emit smaller service-specific tails.

The first PTY cohort is
`out/cxl-riscv/phase1/io500-standard/20260906T105920.180195Z-2596463`.
It proves the relay works: IO500's formerly hidden fatal line arrived before
MPI exit and identified the exact foreground operation. The standard job
failed while opening `result.txt`; client 0's Meta stat exhausted three
120-second RPC attempts and IO500 reported `Remote I/O error`, producing the
aggregate MPI status 221. The platform, ten mounts, metadata preflight, MPI
probe and rank placement all passed. Final coherence has eleven registrations,
223754 dirty completions and zero transport/coherence errors; every QEMU and
the simulator exited zero. Lazy failed-run teardown completed in about one
minute. The preflight directory removal immediately precedes IO500 and creates
work for Meta's 200 ms GC scanner. Earlier causal logs also show that scanner
building FDB TooOld storms. The ten-client finite profile now disables
asynchronous GC and gives the foreground Meta pool four processing and four IO
threads. Logical removes still complete synchronously; delayed physical space
reclamation is unnecessary for an initially empty finite standard run.
Failure tails now seek by byte count so a large Meta log cannot consume the
entire snapshot deadline merely to find its last records.

The next matching G0 attempt,
`out/cxl-riscv/g0-b/full-034/20260906T112304.183856Z-2607131`,
did not reach the smoke transfer: the single FUSE client exhausted all 120
mount-readiness iterations while its process remained alive. The result and
pre-shutdown server snapshot were preserved, then both QEMUs and the simulator
exited zero. This reproduces foreground Meta starvation even without the
ten-client IO500 preflight, so GC suppression now applies to every finite G0
and phase-1 qualification profile. It remains a runtime test configuration;
the machine-local FoundationDB transport stays TCP and production defaults are
unchanged.

The next standard cohort,
`out/cxl-riscv/phase1/io500-standard/20260906T114440.838717Z-2614292`,
passes the faster ten mounts, metadata preflight, MPI probe and rank placement
with GC disabled. IO500 creates `result.txt` but later exhausts three request
windows creating `config-orig.ini` and syncing its new inode. Final coherence
again has eleven registrations, 306794 dirty completions and zero protocol,
delivery, timeout or server-copy errors; every owned process exits cleanly.
This moves the remaining bottleneck from background GC to CPU scheduling: the
90-thread storage service and foreground Meta service still shared all three
server CPUs. Meta is now restricted to CPUs 1-2 and storage to CPUs 2-3, so
each has one private execution CPU and shares CPU 2 for burst capacity. Fabric
and the light management service retain CPUs 1-3, while the TCP FoundationDB
process remains isolated on CPU 0. This partition retains two CPUs for storage
without allowing its idle polling and worker population to consume every Meta
execution slot.
