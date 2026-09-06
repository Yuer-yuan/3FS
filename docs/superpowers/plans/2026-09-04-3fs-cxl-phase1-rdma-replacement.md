# 3FS CXL Phase 1 RDMA Replacement Implementation Plan

User stop condition (2026-09-05): complete phase-1 migration plus a 10c1s
standard IO500 measurement with root-cause/timing closure, then stop. Use ten client guests and one server guest on
the existing QEMU/CXLMemSim platform. Machine-local FoundationDB remains TCP;
phase 2 and the later cross-system performance comparison are out of scope.

Accepted result (2026-09-06 Asia/Shanghai): phase-1 migration and the requested
functional 10c1s qualification have passed. The standard IO500 measurement
remains open; see [the implementation plan](2026-09-06-3fs-cxl-io500-standard.md).
The functional source-bound run is
`out/cxl-riscv/phase1/10c1s/20260905T203556.712060Z-2074312/result.json`
(closure 029/build 027/rootfs full-020): `passed`, `HF3FS_CXL_10C1S_OK`, no
validation errors, all ten write/read/hash and neighbor-read checks pass,
clean retirement, eleven QEMUs and the simulator exit zero, and no owned
process remains. Core/bootstrap and machine-local FDB remain TCP. This result
does not satisfy the user's standard-measurement stop condition.

Restored native CXL storage clients pass 75/75, services 46/46, aggregate storage
137/137, sync 2/2 and migration 1/1 with original assertions/parameters.
All RDMA=OFF exclusions except actual `net/ib` sources have been removed.
The broad suite passes 18/19 CTest targets; its only failures are the three
pre-existing RenderConfig cases that expect rendering from the upstream
template-returning implementation. Latest focused checks pass planes 30/30,
layout 87/87, multiprocess E2E 1/1 and minimal layout 59/59; the fault-gate
regression passes 100 repetitions and the full native/RISC-V builds pass.
All 118 Python CXL/RISC-V tests pass locally and remotely. Bounded P checking
passes ten safe cases at 1000 schedules each and detects five unsafe variants;
it is not an exhaustive proof. Counts across aggregate/separate targets overlap.
The [fixture plan](2026-09-05-3fs-cxl-test-fixtures.md#accepted-phase-1-and-10c1s-result)
owns the exact evidence and functional/simulation limits. Historical checkpoints
below describe earlier states and do not reopen completed work.

> **Execution constraint:** Implement task-by-task in one agent. Do not spawn
> subagents unless the user separately requests them. Steps use checkbox
> (`- [ ]`) syntax for tracking.

**Goal:** Replace every 3FS RDMA RPC and one-sided storage transfer with a fail-closed CXL shared-region transport while retaining existing Core, FoundationDB and bootstrap TCP paths.

**Architecture:** First remove RDMA-specific predicates and buffer types from the generic RPC and storage layers. Then add a versioned DAX ABI, owner-separated SPSC lanes, an eventfd-backed progress engine, TCP-assisted lane bootstrap and CXL bulk arenas; existing serde, retry, storage and replication logic remains authoritative above those interfaces.

**Tech Stack:** C++20, Folly coroutines/IOBuf/event loops, Linux mmap/eventfd/epoll, CRC32C, CMake/Ninja, GoogleTest, P language formal model, RISC-V QEMU guests with `/dev/dax0.0`, CXLMemSim coherence-v2.

**Spec:** `docs/superpowers/specs/2026-09-04-3fs-cxl-transport-design.md`

## Global Constraints

- Pass G0-A in `docs/superpowers/plans/2026-09-04-3fs-cxl-g0-riscv-feasibility.md` before transport implementation, then pass G0-B after this plan's CXL-only source closure and before multi-guest integration.
- After G0-A and before Task 1's full-project configure, initialize all nested
  submodules at their recorded gitlinks and run `./patches/apply.sh`. Refuse a
  dirty/conflicting nested checkout and record recursive HEAD/diff/untracked
  identities; G0-A itself remains independent of those submodules.
- Modify files only inside `components/3FS` and test remotely only in the approved `vlm-server` superproject path.
- Keep Core, FoundationDB and connection bootstrap on TCP in phase 1; no other serving data may use TCP.
- Build RISC-V artifacts with `HF3FS_ENABLE_CXL=ON` and `HF3FS_ENABLE_RDMA=OFF` and verify no `libibverbs` dependency.
- Preserve the existing 512 MiB RPC maximum, serde framing, checksums, request UUIDs, storage `MessageTag`, UpdateChannel and `ReliableUpdate` semantics.
- Use logical DAX offsets, never serialized virtual addresses.
- Use one writer per shared cursor and release/acquire publication; do not require remote atomic RMW.
- Require native lock-free 32-bit and 64-bit atomic loads/stores on every host and guest;
  a `libatomic` lock fallback is invalid for shared CXL synchronization.
- A timeout never permits premature queue-cell or bulk-allocation reuse.
- CXL errors fail closed and increment evidence counters; no RDMA/TCP fallback is allowed.
- Do not optimize away the per-hop stable copy in the initial chain-replication implementation.
- Do not create commits; finish every task with an uncommitted review checkpoint.
- Unless a step explicitly names the superproject root, run its commands from the `components/3FS` checkout root.

---

## Execution checkpoint — 2026-09-05 host FUSE smoke

The remote host full stack passed a finite 2 MiB random-data write, per-file
fsync, direct readback and byte comparison with private FoundationDB 7.3.63,
mgmtd, meta, one storage process with four RF1 targets, and FUSE. The build is
`build/cxl-no-rdma-host`, Debug, Clang 18, CXL on, RDMA off and
`ENABLE_FUSE_APPLICATION=ON`. The launcher profile must be enabled for the
FUSE executable to consume its CXL runtime configuration.

Evidence on `vlm-server`, relative to the 3FS checkout:

- `out/cxl-riscv/fuse-smoke-logs/repeat-02/result.json`
- `out/cxl-riscv/fuse-smoke-logs/repeat-02/runner.log`
- `out/cxl-riscv/fuse-smoke-logs/repeat-02/cxl-fabricd.stdout`
- `out/cxl-riscv/fuse-smoke-logs/handle-alignment-regression.log`

The runner exits zero, reports `HF3FS_CXL_FUSE_SMOKE_OK bytes=2097152`, and
the fabric authority reports `HF3FS_CXL_FABRIC_RETIRED`. All six participant
records are RETIRED; no test services or FUSE mount remain. Input and output
SHA-256 are both
`47f50acc656711223419e15701157f8600ddc31e505bbcf1230edb0a8c7f88f1`.
These small evidence files also have local copies under the same relative
log path. This proves the host regular-file mapping path only. RISC-V full
application compilation, QEMU/DAX full-stack execution and the excluded
SetupIB storage/bulk fixtures remain open; this is not phase-1 acceptance.

The smoke exposed and fixed peer-close notification/lane reuse, an
over-aligned RPC buffer handle in coroutine frames, and test-runner startup
and shutdown gaps. Idle sockets now observe peer owner-record retirement and
wake their I/O worker; already published replies drain before orderly EOF.
Lane reuse still requires both owners to retire. `RemoteBufferHandle` keeps
its 64-byte size and field offsets with natural 64-bit alignment; shared
fabric records retain cache-line alignment. New close/drain/alignment tests
and an eight-client-process reuse regression pass, as do all four selected
CXL CTests. The alignment regression failed against the former 64-byte
alignment before the fix.

The runner raises only its inherited soft file-descriptor limit, writes
run-owned admin logs, and stops FUSE, meta, storage, mgmtd, FDB and fabric in
dependency order. A disconnected FUSE mount is explicitly unmounted. The
private FDB server's expected SIGTERM wait status 143 is accepted only for
processes deliberately stopped by cleanup; crashes and forced kills still
fail. Earlier failure logs and runtime directories are retained under
`out/cxl-riscv/fuse-smoke-logs/attempts/`. No commits or platform changes were
made.

## Execution checkpoint — 2026-09-05 full RISC-V application build

The host FUSE smoke also passed an independent `repeat-03` run with clean
retirement. Its matching 2 MiB input/output SHA-256 is
`29aeb70d80084bd9989ed9ec87de3fc8876b6e49b6e966701c9f8393e3bae3d2`;
the result and runner log are under
`out/cxl-riscv/fuse-smoke-logs/repeat-03/` locally and remotely.

The complete RISC-V application build now passes in `build/cxl-riscv-full`
with Clang 18, Release, CXL enabled, RDMA disabled and the FUSE application
launcher enabled. Verified RISC-V executables include `mgmtd_main`,
`meta_main`, `storage_main`, `hf3fs_fuse_main`, `admin_cli`, `cxl-fabricd`,
`simple_example_main`, `migration_main`, and the three G0 probes. Direct
dependencies contain no verbs library. The staged guest runtime closure is
also recursively checked for target ELF identity and verbs dependencies.

Reproducible private inputs and evidence:

- `deploy/cxl-riscv/full-deps.json`, `full_deps.py`: 58 hash-locked target
  packages, combined with the existing private GCC/FDB/FUSE sysroots.
- `deploy/cxl-riscv/compiler-rt.json`, `prepare_compiler_rt.py`: private
  Clang 18 RISC-V builtins for checked 128-bit multiplication.
- `deploy/cxl-riscv/build_riscv.py`: frozen preflight/source identities,
  target-only CMake paths, private Rust 1.90 GNU target, locked offline Cargo,
  artifact hashes, and explicit jemalloc runtime evidence.
- `deploy/cxl-riscv/arrow-cross.patch`: pinned offline Arrow's cross toolchain
  and prefixed jemalloc configuration. Arrow uses the verified target static
  zlib. Native dependency build branches remain available.
- `out/cxl-riscv/full-deps/build-manifest-007.json`: first passing full build.
- `out/cxl-riscv/full-deps/portability-native-ctest.log`: five CTests passed.
- `out/cxl-riscv/full-deps/portability-host-test.log` and
  `portability-native-utils-test.log`: eight tests each, using portable and
  native Status/shared-pointer representations respectively.

RV64 uses a full-pointer `Status` representation and C++20 atomic shared_ptr
for process-local ownership. This avoids assuming 48-bit virtual addresses or
Folly's hard-coded libstdc++ shared_ptr lock policy. Status wire serialization
is unchanged. The CXL shared ABI still requires lock-free 32/64-bit atomic
loads/stores; it contains neither Status objects nor atomic shared_ptrs.
RocksDB's two assertion-only Release variables are marked `maybe_unused` in
an idempotent repository-owned patch.

`stage_full_rootfs.py`, `full_stack.py` and the `run_g0.py --profile full-3fs`
path now stage and exercise a two-guest full stack: FDB/mgmtd/meta/storage and
fabric authority on guest 0, FUSE on guest 1. The initial run at
`out/cxl-riscv/g0-b/full-001/20260905T034947.064049Z-1275173/result.json`
passed all platform checks, including both dirty BI directions and the real
FDB transaction, but failed before application startup because the guest
hard fd limit was below the requested soft limit. All owned processes were
stopped. This is a failed G0-B result, not application acceptance.

The next attempt raises only the guest process's fd limits and reserves the
first 2 MiB for the platform probe. Applications map the remaining aligned
254 MiB with unchanged queue/cell geometry; this prevents probe payloads from
being interpreted as endpoint metadata. Debug information is removed only
from new guest copies, with before/after hashes recorded; original build
artifacts are retained. The full rootfs shrinks from about 4.8 GiB to 556 MiB.
The second attempt at
`out/cxl-riscv/g0-b/full-002/20260905T040343.118089Z-1289759/result.json`
started the fabric and initialized the cluster through `admin_cli`, then
`mgmtd_main` crashed in Folly's stacktrace string allocation. Disassembly
identified a null `nallocx` call: Folly was built with signed char, while RV64
consumers used unsigned char and interpreted the allocator cache's false
sentinel (-1) as true. The Folly target now exports `-fsigned-char` as a C++
usage requirement. `test_folly_allocator` simulates unsigned-char compiler
defaults and checks repeated false-cache reads and string growth. Its cache
regression failed before the fix, with all eight cached reads returning true.
Both failed G0-B runs retain their images and logs; owned platform processes
were stopped. The corrected build passed all eleven RV64 artifact checks;
six native CTests and 103 Python tests passed.

The third attempt,
`out/cxl-riscv/g0-b/full-003/20260905T042503.338677Z-1304391/result.json`,
passed mgmtd startup and its clean exit. Storage opened its listener but then
aborted because the guest lacked `blkid` and `lsblk`; opening a listener alone
is not application readiness. Meta's launcher first encountered 5-second
bootstrap RPC timeouts, then exhausted pending lane slots and aborted.
The owned QEMU/CXLMemSim processes stopped cleanly. `guest-tools.json` and
`prepare_guest_tools.py` now pin private Ubuntu Noble RV64 disk inspection
tools and their libraries; staging verifies their recursive ELF closure.
The guest profile gives bootstrap connections 120 seconds as well as the
existing 120-second serving timeout, and logs failed startup diagnostics.
Bootstrap cancellation/abandoned pending-lane behavior still needs separate
coverage; longer TCG deadlines do not establish that behavior.

The fourth attempt,
`out/cxl-riscv/g0-b/full-004/20260905T044038.580205Z-1322380/result.json`,
verified both disk tools, PRIMARY_MGMTD and connected META/STORAGE heartbeats,
and created all four RF1 targets and the chain table. FUSE reached its local
I/O ring watchers, then crashed in `sem_timedwait`: `/dev/shm` was absent and
the existing unchecked `sem_open` result was invalid. The full guest setup
now mounts a 64 MiB tmpfs at `/dev/shm`. `IoRingTable::init` returns an error
on semaphore or permission initialization failure, rolls back partial handles,
and `FuseClients::init` propagates the error before starting watcher threads.
`test_fuse_ioring_init` covers failure at each of the three semaphore opens,
rollback of earlier names, successful post/wait, and repeated initialization.
All owned platform processes from the fourth attempt were stopped.

The fifth attempt,
`out/cxl-riscv/g0-b/full-005/20260905T045508.268074Z-1331776/result.json`,
failed before reaching FUSE: an FDB lease extension took about 49 seconds
during cold TCG startup. The default 60-second lease with a 20-second suspicion
margin became untrusted, and meta aborted on its initial routing refresh.
Extracted complete guest logs confirm the lease gap. The G0-B guest profile
now sets a 180-second lease and heartbeat failure interval and a 120-second
heartbeat timestamp window; lease validation and production defaults remain
enabled and unchanged. Startup checks require the completed-server marker
and live process, and heartbeat polling stops when a service exits. This
profile does not qualify production failover timings. All owned processes
stopped; images, logs and the failed result are retained. The FUSE semaphore
regression passes both native cases, but its guest I/O proof is still pending.

The sixth attempt,
`out/cxl-riscv/g0-b/full-006/20260905T051636.255457Z-1342081/result.json`,
passed all server startup markers and connected heartbeats, then failed the
first create-target with RoutingInfoNotReady. Admin logs show bootstrap refused
because mgmtd's fabric was unavailable; mgmtd shutdown reports a lifecycle
monitor DataCorruption. The monitor treated sixteen observations of a writer's
unfinished odd sequence as corruption. A deterministic paused-publication test
reproduces this false permanent fault before the fix. The monitor now retries
incomplete snapshots under its unchanged stale-authority deadline, while
stable bad CRC/identity still faults immediately. A second regression checks
that a permanently odd sequence expires, and the existing bad-CRC case still
passes. Bootstrap now exposes the underlying fabric error and the monitor
prints its first detailed error. All four focused native transport CTests pass.
Before/after evidence is in `out/cxl-riscv/full-deps/lifecycle-*.log`.
The sixth run's owned QEMU/CXLMemSim processes stopped with exit zero; the
application result remains failed and its images/logs are retained.

The seventh attempt,
`out/cxl-riscv/g0-b/full-007/20260905T053516.569397Z-1351230/result.json`,
completed all services, four targets and FUSE mounting on guest 1. The finite
write failed at 983040 bytes while guest 0 reported `virtio: bogus descriptor
or out of resources`; disk I/O and the guest cleanup shell then stalled.
Storage's AIO pool was allocated from the CXL DAX arena and handed directly to
disk I/O. The pool now uses ordinary aligned memory, and write forwarding and
resynchronization create separate read-only CXL copies with request leases.
The placement regression fails before the fix and passes after it; the export
regression checks stable bytes, read-only permissions and lease release.
Four CXL CTests pass, including 81 layout cases.

FUSE exited normally in the seventh run, but guest 0 needed an explicitly
recorded SIGTERM after its cleanup command timed out. All owned platform
processes are stopped. `manual-teardown.json` records the action; the raw
`node0-root.ext4` remains unchanged. `node0-recovered.ext4` is a separate
diagnostic copy with filesystem recovery recorded in `filesystem-recovery.*`.
The runner now stops queueing commands to a guest after a console timeout and
lets outer QEMU teardown finish the failed run. 104 Python tests pass.

The corrected disk-buffer path passes the host full stack at
`out/cxl-riscv/fuse-smoke-logs/disk-buffer-02/result.json`, with 2 MiB SHA-256
`bb58e6bf0325f9cc4019be9536faa45da23539c3ceef3173634059ed63d0ad03`,
normal service exit and all six endpoint records retired. The first native
attempt is retained in `disk-buffer-01`: a preserved source mtime caused Ninja
to reuse an old StorageOperator object against the new pool. Rebuilding that
object corrected the artifact; subsequent syncs avoid preserving source times
across concurrent builds.

The eighth attempt,
`out/cxl-riscv/g0-b/full-008/20260905T061240.488602Z-1366759/result.json`,
uses build 015/source closure 017. All services and FUSE mounted; the first
1 MiB FUSE request still failed after `dd` accepted 983040 bytes. This time
there is no virtio descriptor failure, storage remains responsive, and the
FUSE log records a completed chunk write followed by a timed-out second
operation. StorageMessenger's explicit 10/20-second request deadlines and
60-second total retry budget overrode the network client's 120-second default;
MetaClient also retained its own 5-second RPC deadline. The G0-B renderer now
sets storage's explicit request budget and meta's RPC deadline to 120 seconds,
with bounded 360-second retry totals. These are simulator-only settings;
production defaults and validation remain unchanged. The rendered-config
regression fails before this correction. All owned applications and platform
processes exited without forced cleanup, and the fabric emitted RETIRED.
The complete guest logs and failed result are retained.

The ninth attempt passes G0-B:
`out/cxl-riscv/g0-b/full-009/20260905T063240.490316Z-1377219/result.json`.
Build 016/source closure 018 produced the complete RISC-V CXL-only image.
Guest 0 runs private FDB, fabric authority, mgmtd, meta and storage; guest 1
runs FUSE. The 2 MiB write/fsync and direct readback compare successfully with
SHA-256 `e1d9fead42730b50b702b00f2cbddd6d539837e0916d4309fa18487b08985275`
and `HF3FS_CXL_FUSE_SMOKE_OK`. The write takes 399.229 seconds and readback
119.716 seconds in this functional simulator; these are not performance
measurements. All five resident 3FS processes exit zero, the authority reports
retirement, FUSE unmounts, and both QEMU processes and CXLMemSim exit zero.
The result validator reports no errors. `cleanup-verification.json` additionally
checks no owned platform process remains, validates the immutable handoff hash,
and independently extracts both 2 MiB files from the stopped guest image to
verify the matching SHA-256. The passing reference is
`out/cxl-riscv/g0-b/g0-b-handoff.json`. Full logs and raw images are retained.
This is two-guest RF1 application feasibility with colocated server roles;
the separate phase-1 multi-role, RF2/RF3 and formal acceptance gates remain open.

At this checkpoint, excluded storage/bulk fixture migration, formal checks
and phase-1 acceptance remain
open. Phase 2 has not started. No commits or platform-side changes were made.

## File map

**Create in `src/common/net`:**

- `TransportKind.h` — transport identity and service-plane mapping.
- `CompletionDisposition.h` — publication-aware completion classification.
- `TransportRuntime.h`, `TransportRuntime.cc` — process-scoped backend
  lifecycle with conditional CXL/RDMA configuration.
- `Buffer.h`, `Buffer.cc` — generic shared local buffer and remote handle.
- `RemoteExportLease.h`, `RemoteExportLease.cc` — request-scoped exported
  allocation lifetime.
- `BulkTransfer.h`, `BulkTransfer.cc` — transport-neutral pull/push batches.
- `BulkControl.h`, `BulkControl.cc` — transport-neutral admission and completion control for delayed bulk work.

**Create in `src/common/net/cxl`:**

- `CxlAbi.h` — fixed-width shared structures and constants.
- `CxlFabric.h`, `CxlFabric.cc` — one mapping, endpoint, progress engine,
  lane authority and bulk arena per process.
- `CxlRegion.h`, `CxlRegion.cc` — file/DAX mappings and checked ranges.
- `CxlLayout.h`, `CxlLayout.cc` — manifest-derived layout validation.
- `CxlEndpointState.h`, `CxlEndpointState.cc` — phase-1 owner-only process
  lifecycle and heartbeat publication.
- `CxlLane.h`, `CxlLane.cc` — SPSC queue operations and byte-stream segmentation.
- `CxlProgressEngine.h`, `CxlProgressEngine.cc` — polling and eventfd notifications.
- `CxlSocket.h`, `CxlSocket.cc` — `Socket` implementation.
- `CxlConnectService.h`, `CxlConnectService.cc` — phase-1 TCP bootstrap.
- `CxlBuffer.h`, `CxlBuffer.cc` — endpoint-owned bulk arena allocation.
- `CxlBulkTransfer.h`, `CxlBulkTransfer.cc` — coherent pull/push implementation.
- `CxlMetrics.h`, `CxlMetrics.cc` — mandatory evidence counters.

**Create in the optional `src/common/net/ib` backend:**

- `RDMABulkTransfer.h`, `RDMABulkTransfer.cc` — build-compatible adapter from
  the neutral bulk interface to the existing verbs implementation. It is never
  compiled into the RISC-V CXL-only build.

**Create in `src/tools/cxl-fabricd`:**

- `CMakeLists.txt`, `main.cc` — dependency-minimal global layout/lifecycle
  authority colocated with a normal guest but owning a distinct endpoint ID.

**Create tests:**

- `tests/common/net/TestTransportKind.cc`
- `tests/common/net/cxl/TestCxlAbi.cc`
- `tests/common/net/cxl/TestCxlRegion.cc`
- `tests/common/net/cxl/TestCxlEndpointState.cc`
- `tests/common/net/cxl/TestCxlLane.cc`
- `tests/common/net/cxl/TestCxlSocket.cc`
- `tests/common/net/cxl/TestCxlConnect.cc`
- `tests/common/net/cxl/TestCxlFabric.cc`
- `tests/common/net/cxl/TestCxlBuffer.cc`
- `tests/common/net/cxl/TestCxlBulkTransfer.cc`
- `tests/common/net/ib/TestRDMABulkTransfer.cc`
- `tests/storage/service/TestCxlStorageService.cc`
- `tests/storage/sync/TestCxlSyncForward.cc`
- `tests/cxl_riscv/test_phase1_evidence.py`
- `specs/CXLTransport/` — P model, monitors and test drivers.

**Modify existing network/storage files:**

- `src/common/utils/Address.h`
- `src/common/net/Socket.h`, `Transport.*`, `TransportPool.*`, `IOWorker.*`,
  `Listener.*`, `ServiceGroup.*`
- `src/common/serde/Services.h`, `CallContext.*`
- `src/common/net/Processor.*`
- `src/client/storage/StorageClient.*`, `StorageClientImpl.*`
- `src/fbs/storage/Common.h`
- `src/storage/service/StorageService.h`, `StorageOperator.*`, `ReliableForwarding.*`, `BufferPool.*`
- `src/storage/aio/BatchReadJob.*`
- `src/storage/sync/ResyncWorker.cc`
- service/client configs and CMake files identified in the design spec.

## Task 1: Introduce transport identity and service planes

**Files:**

- Create: `src/common/net/TransportKind.h`
- Create: `src/common/net/CompletionDisposition.h`
- Create: `tests/common/net/TestTransportKind.cc`
- Modify: `src/common/utils/Address.h:18-103`
- Modify: `src/common/utils/StatusCode.h`
- Modify: `src/common/utils/StatusCodeDetails.h:88-114`
- Modify: `src/fbs/meta/Utils.h:136-153`
- Modify: `src/common/net/Socket.h:14-39`
- Modify: `src/common/net/tcp/TcpSocket.h`
- Modify: `src/common/net/ib/IBSocket.h`
- Modify: `src/common/net/Transport.h:38-64`
- Modify: `src/common/net/TransportPool.h`
- Modify: `src/common/net/TransportPool.cc`
- Modify: `src/common/net/Server.h`
- Modify: `src/common/serde/Services.h:12-39`
- Modify: `src/common/net/Processor.h:45-218`
- Modify: `src/common/net/ServiceGroup.h:38-54`
- Modify: `tests/common/utils/TestStatusCode.cc`
- Modify: `tests/common/net/TestProcessor.cc`
- Modify: `tests/common/net/TestEcho.cc`
- Modify: `tests/common/serde/TestService.cc`

**Interfaces:**

- Produces: `TransportKind`, `ServicePlane`, `ServiceEndpoint`,
  `PublicationSnapshot`, `CompletionDisposition` and
  `transportKind(Address::Type)`.
- Produces: `Socket::kind() const noexcept`, `Transport::kind()`, and `Transport::servicePlane()`.
- Produces: `Services::getServiceById(uint16_t, ServicePlane)`.
- Produces: transport-neutral RPC status aliases while preserving every
  existing numeric value and diagnostic string.
- Consumes later: CXL sockets identify as `TransportKind::CXL` and Data plane.

- [ ] **Step 1: Write enum, address and service-table tests**

```cpp
TEST(TestTransportKind, AddressMapping) {
  EXPECT_EQ(transportKind(Address::TCP), TransportKind::TCP);
  EXPECT_EQ(transportKind(Address::RDMA), TransportKind::RDMA);
  EXPECT_EQ(transportKind(Address::CXL), TransportKind::CXL);
  EXPECT_NE(ServiceEndpoint{tcpAddress(), ServicePlane::Control},
            ServiceEndpoint{tcpAddress(), ServicePlane::Data});
}

TEST(TestTransportKind, CxlAddressRoundTrip) {
  auto address = Address::fromString("CXL://10.0.0.7:8000");
  EXPECT_EQ(address.type, Address::CXL);
  EXPECT_EQ(Address::fromString(address.str()), address);
  static_assert(sizeof(Address) == sizeof(uint64_t));
}

TEST(TestTransportStatus, NeutralAliasesKeepWireValues) {
  static_assert(RPCCode::kNoSharedBuffer == RPCCode::kRDMANoBuf);
  static_assert(StorageClientCode::kNoDataPlaneInterface ==
                StorageClientCode::kNoRDMAInterface);
  EXPECT_EQ(StatusCode::toString(RPCCode::kNoSharedBuffer),
            "RPC::RDMANoBuf");
}
```

- [ ] **Step 2: Run the focused test and verify `Address::CXL` is absent**

Run:

```bash
cmake --build build/cxl-g0-host --target test_common
build/cxl-g0-host/tests/test_common --gtest_filter='TestTransportKind.*'
```

Expected: compilation FAIL because `CXL`, `TransportKind`, `ServicePlane` and
the neutral status aliases do not exist.

- [ ] **Step 3: Add the explicit identity types**

```cpp
enum class TransportKind : uint8_t { TCP, RDMA, CXL };
enum class ServicePlane : uint8_t { Control, Data };
enum class CompletionDisposition : uint8_t {
  Completed,
  RejectedBeforeExecute,
  OutcomeUnknown,
  LaneRetired,
};

struct PublicationSnapshot {
  uint64_t laneGeneration;
  uint64_t acceptedOffset;
  uint64_t publishedOffset;
  uint64_t peerDeliveredOffset;
  bool trustworthy;
};

struct ServiceEndpoint {
  Address address;
  ServicePlane plane;
  bool operator==(const ServiceEndpoint &) const = default;
};

constexpr TransportKind transportKind(Address::Type type) {
  if (type == Address::RDMA) return TransportKind::RDMA;
  if (type == Address::CXL) return TransportKind::CXL;
  return TransportKind::TCP;
}
```

Do not add a `servicePlane(TransportKind)` helper. CXL carries both planes in
phase 2. Store the plane on `Transport`, inherit it from an accepted listener
or resolved endpoint, and include it in `TransportPool`'s shard and
thread-local connection-cache keys. Resolve an `Address` to exactly one
`ServiceEndpoint` before the pool lookup: TCP defaults to Control, RDMA to Data,
and CXL obtains its mandatory plane from the phase-1 local route declaration or
phase-2 manifest. Reject a CXL address that resolves to zero or multiple
planes.
Legacy TCP and RDMA config parsing may infer Control and Data respectively,
but every CXL endpoint must declare a plane explicitly.

Append `CXL` to `Address::Type`, add `isCXL()`, and keep all existing numeric values unchanged.

Add neutral aliases for the existing transport-specific RPC status symbols:

| Value | Legacy symbol | Neutral alias |
| ---: | --- | --- |
| 2015 | `IBInitFailed` | `DataPlaneInitFailed` |
| 2016 | `IBDeviceNotFound` | `DataPlaneInterfaceNotFound` |
| 2017 | `RDMAPostFailed` | `BulkPostFailed` |
| 2018 | `RDMAError` | `BulkTransferError` |
| 2019 | `RDMANoBuf` | `NoSharedBuffer` |
| 2021 | `IBDeviceNotInitialized` | `DataPlaneNotInitialized` |
| 2027 | `IBOpenPortFailed` | `DataPlaneOpenFailed` |

Also add
`StorageClientCode::kNoDataPlaneInterface = kNoRDMAInterface`, preserving
StorageClient value 7016 and its current conversion/retry behavior.

Append `DataPlaneHandshakeFailed=2028`,
`TransportCapabilityMissing=2029`, `RemoteBufferAccessDenied=2030` and
`StaleGeneration=2031` to `StatusCodeDetails.h`. Define the seven aliases in
`StatusCode.h` after the generated status declarations, for example
`inline constexpr auto kNoSharedBuffer = kRDMANoBuf;`; do not add duplicate
`RPC_STATUS` rows because `StatusCode::toString` generates a switch. This keeps
old source buildable and preserves the legacy diagnostic string while new
business code uses only neutral names. Update Meta's prune/retry classification
to recognize `DataPlaneInitFailed`. The phase-1 closure allowlists the legacy
symbols only in this compatibility declaration and `common/net/ib`.

- [ ] **Step 4: Make Socket and Transport expose capabilities instead of casts**

Add to `Socket`:

```cpp
virtual TransportKind kind() const noexcept = 0;
virtual BulkTransfer *bulkTransfer() noexcept { return nullptr; }
virtual std::optional<PublicationSnapshot> publicationSnapshot() const noexcept {
  return std::nullopt;
}
```

Forward-declare `BulkTransfer` and implement `kind()` in TCP and IB sockets.
TCP and IB sockets keep the default unavailable publication snapshot; CXL
overrides it with generation-scoped byte watermarks.
Keep the existing synchronous `TcpSocket::close()` and coroutine
`IBSocket::close()` signatures: they cannot implement one virtual method because
their return types and drain semantics differ. Remove the public
`Transport::ibSocket()` escape hatch and replace business-path casts with
`kind()`, `servicePlane()` or `bulkTransfer()`. The existing IB-manager ownership
handoff remains encapsulated in `Transport` lifecycle code.

- [ ] **Step 5: Replace boolean service indexing**

Use:

```cpp
static constexpr size_t planeIndex(ServicePlane plane) {
  return static_cast<size_t>(plane);
}

CallContext::ServiceWrapper &getServiceById(uint16_t id, ServicePlane plane) {
  return services_[planeIndex(plane)].at(id);
}
```

Change `Services::addService` to accept an explicit non-empty set of planes,
convert the unique service to shared ownership once, and install that same
object only in those tables. `ServiceGroup::Config` carries `service_plane`;
existing TCP/RDMA configs may infer it during compatibility parsing, but CXL
configs must set it. Code that genuinely serves in both planes passes
`{Control, Data}` explicitly and has a duplicate-object ownership test.
`CxlConnectService` passes `{Control}`; the bootstrap TCP transport cannot
dispatch a Data-plane service ID. Add a negative test that invokes a Data
service through the bootstrap socket and expects `kInvalidServiceID`.

- [ ] **Step 6: Run common RPC tests**

Run:

```bash
cmake --build build/cxl-g0-host --target test_common
build/cxl-g0-host/tests/test_common --gtest_filter='TestTransportKind.*:TestEcho.*:TestEchoListener.*'
```

Expected: all selected tests PASS for existing TCP/IB-enabled host compilation.

- [ ] **Step 7: Record the uncommitted checkpoint**

Run `git diff --check && git status --short`.

## Task 2: Implement the fixed CXL ABI, mappings and layout validation

**Files:**

- Create: `src/common/net/cxl/CxlAbi.h`
- Create: `src/common/net/cxl/CxlRegion.h`
- Create: `src/common/net/cxl/CxlRegion.cc`
- Create: `src/common/net/cxl/CxlLayout.h`
- Create: `src/common/net/cxl/CxlLayout.cc`
- Create: `tests/common/net/cxl/TestCxlAbi.cc`
- Create: `tests/common/net/cxl/TestCxlRegion.cc`

**Interfaces:**

- Produces: `CxlSuperblock`, `CxlFabricLifecycleRecord`,
  `CxlEndpointRecord`, `CxlLaneOwnerRecord`, `CxlDeliveredRecord`,
  `CxlAllocationRecord`,
  `CxlFrameEntry`, `CxlRegion`, `CxlLayout`.
- Produces: `CxlRegion::mapFile`, `CxlRegion::mapDax`, and `checkedRange`.
- Produces: `CxlLayout::initializeAsAuthority(...)` and
  `CxlLayout::attach(...)`; there is exactly one manifest-declared global
  lifecycle writer.
- Produces: `CxlLayout::loadAndValidate(CxlRegion&, const LayoutExpectation&)`.
- Produces: one consumer-owned, 64-byte-aligned delivered-byte watermark per
  direction, placed on the consumer's cursor page and bound to the lane
  generation.
- Consumes later: lane, buffer and socket implementations use only validated typed ranges.

- [ ] **Step 1: Write ABI size, endian and malformed-layout tests**

```cpp
TEST(TestCxlAbi, FixedSizes) {
  static_assert(sizeof(CxlSuperblockHeader) == 128);
  static_assert(sizeof(CxlRangeEntry) == 24);
  static_assert(sizeof(CxlSuperblock) == 4096);
  static_assert(alignof(CxlSuperblock) == 4096);
  static_assert(sizeof(CxlFrameEntry) == 64);
  static_assert(alignof(CxlFrameEntry) == 64);
  static_assert(sizeof(CxlFabricLifecycleRecord) == 64);
  static_assert(alignof(CxlFabricLifecycleRecord) == 64);
  static_assert(sizeof(CxlEndpointRecord) == 64);
  static_assert(alignof(CxlEndpointRecord) == 64);
  static_assert(sizeof(CxlLaneOwnerRecord) == 64);
  static_assert(alignof(CxlLaneOwnerRecord) == 64);
  static_assert(sizeof(CxlDeliveredRecord) == 64);
  static_assert(alignof(CxlDeliveredRecord) == 64);
  static_assert(sizeof(CxlAllocationRecord) == 64);
  static_assert(alignof(CxlAllocationRecord) == 64);
  EXPECT_EQ(kCxlAbiVersion, 1);
}

TEST(TestCxlRegion, RejectsOverflowingRange) {
  auto region = ASSERT_RESULT(CxlRegion::mapFile(path_, 1_MB));
  auto result = region.checkedRange(UINT64_MAX - 4, 16);
  ASSERT_FALSE(result);
  EXPECT_EQ(result.error().code(), StatusCode::kInvalidArg);
}

TEST(TestCxlLayout, RejectsGenerationAndCrcMismatch) {
  writeValidLayout(region_, /*generation=*/9);
  corruptLayoutCrc(region_);
  ASSERT_FALSE(CxlLayout::loadAndValidate(region_, expectation(9)));
  writeValidLayout(region_, /*generation=*/9);
  ASSERT_FALSE(CxlLayout::loadAndValidate(region_, expectation(10)));
}

TEST(TestCxlLayout, AttachNeverFormatsAndSecondAuthorityIsRejected) {
  poison(region_);
  ASSERT_ERROR(RPCCode::kDataPlaneHandshakeFailed,
               CxlLayout::attach(region_, expectation(9), 10_ms));
  EXPECT_TRUE(stillPoisoned(region_));
  ASSERT_OK(CxlLayout::initializeAsAuthority(region_, manifest(9), EndpointId{1}));
  ASSERT_ERROR(StatusCode::kInvalidArg,
               CxlLayout::initializeAsAuthority(region_, manifest(9), EndpointId{2}));
}
```

- [ ] **Step 2: Build the test and observe missing CXL types**

Run `cmake --build build/cxl-g0-host --target test_common`.

Expected: compilation FAIL on the missing CXL headers.

- [ ] **Step 3: Define the immutable superblock and 64-byte records**

ABI v1 uses exactly eight fixed-order ranges:

```cpp
using le16 = uint16_t;
using le32 = uint32_t;
using le64 = uint64_t;
static_assert(std::endian::native == std::endian::little,
              "CXL ABI requires a little-endian process");

enum class CxlRangeKind : uint16_t {
  EndpointDirectory = 1,
  LaneDirectory = 2,
  CursorPages = 3,
  FrameRings = 4,
  RpcPayloadCells = 5,
  AllocationDirectory = 6,
  BulkArenas = 7,
  EvidenceCounters = 8,
};

struct CxlRangeEntry {
  le16 kind;
  le16 flags;
  le32 reserved;
  le64 offset;
  le64 length;
};
static_assert(sizeof(CxlRangeEntry) == 24);

struct CxlSuperblockHeader {
  std::array<std::byte, 8> magic;  // HF3FSCXL
  le16 abiMajor;
  le16 abiMinor;
  le32 superblockBytes;
  le32 endianMarker;
  le32 cacheLineBytes;
  le32 rangeCount;
  le32 endpointCount;
  le32 laneCount;
  le32 reserved0;
  le64 totalRegionBytes;
  le64 sessionGeneration;
  le64 lifecycleRecordOffset;
  std::array<std::byte, 32> manifestSha256;
  le32 superblockCrc32c;
  std::array<std::byte, 28> reserved1;
};
static_assert(sizeof(CxlSuperblockHeader) == 128);

struct alignas(4096) CxlSuperblock {
  CxlSuperblockHeader header;
  std::array<CxlRangeEntry, 8> ranges;
  std::array<std::byte, 3776> reserved;
};
static_assert(sizeof(CxlSuperblock) == 4096);
static_assert(alignof(CxlSuperblock) == 4096);
```

Encode it explicitly as little-endian bytes. Require the exact magic, ABI
`1.0`, `superblockBytes=4096`, endian marker, 64-byte cache line, eight entries
in numeric kind order, zero flags/reserved bytes and a nonzero session. The
binary manifest digest must match `LayoutExpectation`. Compute Castagnoli
CRC32C over all 4 KiB with `superblockCrc32c=0`. Once published, these bytes are
immutable; lifecycle/heartbeat changes use only the separate record below.
`attach()` first copies and validates the fixed block at offset zero, including
all checked arithmetic, before dereferencing its lifecycle offset. It then
acquire-snapshots the matching `READY` lifecycle record. CRC mismatch during a
concurrent format is a bounded retry; no unvalidated shared offset is followed.

```cpp
struct alignas(64) CxlFrameEntry {
  le64 absoluteSequence;
  le64 sessionGeneration;
  le64 laneGeneration;
  le64 streamOffset;
  le64 payloadOffset;
  le32 payloadLength;
  le32 flags;
  le32 crc32c;
  le32 reserved0;
  le64 reserved1;
};
static_assert(sizeof(CxlFrameEntry) == 64);
```

`streamOffset` is direction-local and generation-scoped; it is not an RPC
identity. Require `DATA` entries to start at the next expected byte offset,
and require all reserved fields to be zero. `ERROR` and `RETIRE` are
zero-payload control entries at the current stream offset.

Define the process-owned endpoint state used in both phases:

```cpp
struct alignas(64) CxlEndpointRecord {
  le64 recordSequence;
  le64 sessionGeneration;
  le32 endpointId;
  le32 lifecycle;
  le64 endpointGeneration;
  le64 capabilityBits;
  le64 heartbeat;
  le32 recordCrc32c;
  le32 reserved0;
  le64 reserved1;
};
static_assert(sizeof(CxlEndpointRecord) == 64);
```

Use a 64-bit `endpointGeneration` consistently with
`RemoteBufferHandle::ownerGeneration`. Endpoint lifecycle is `Free`,
`Starting`, `Ready`, `Draining`, `Retired` or `Faulted`; only the named process
writes its record. Heartbeat is a process-local counter published through the
record, never a shared `fetch_add`.

Define the owner-separated lane state explicitly:

```cpp
struct alignas(64) CxlLaneOwnerRecord {
  le64 recordSequence;
  le64 sessionGeneration;
  le64 laneGeneration;
  std::array<std::byte, 16> connectionNonce;
  le64 capabilityBits;
  le32 lifecycle;
  le32 crc32c;
  le64 endpointGeneration;
};
static_assert(sizeof(CxlLaneOwnerRecord) == 64);
```

Define each consumer-owned delivered watermark as a protected record:

```cpp
struct alignas(64) CxlDeliveredRecord {
  le64 recordSequence;
  le64 sessionGeneration;
  le64 laneGeneration;
  le64 deliveredOffset;
  le64 deliveredOffsetComplement;
  le32 crc32c;
  le32 reserved0;
  le64 reserved1;
  le64 reserved2;
};
static_assert(sizeof(CxlDeliveredRecord) == 64);
```

Require `deliveredOffsetComplement == ~deliveredOffset`, matching session and
lane generations, zero reserved fields, a stable final-even sequence and
Castagnoli CRC over the complete 64-byte final-even encoding with
`crc32c=0`. A reader treats any failed condition as an untrustworthy
publication snapshot; it must not substitute zero or the last cached value.

Define the sole-authority global lifecycle record separately from immutable
superblock fields:

```cpp
struct alignas(64) CxlFabricLifecycleRecord {
  le64 recordSequence;
  le64 sessionGeneration;
  le64 authorityGeneration;
  le64 heartbeat;
  le32 authorityEndpoint;
  le32 superblockCrc32c;
  le32 lifecycle;
  le32 recordCrc32c;
  std::byte reserved[16];
};
static_assert(sizeof(CxlFabricLifecycleRecord) == 64);
```

Only `cxl-fabricd` writes `CxlFabricLifecycleRecord`. Apply the owner
sequence/CRC protocol below; exclude it from the immutable superblock CRC and
require its embedded superblock CRC to match. Heartbeat and lifecycle updates must
never rewrite an immutable superblock cache line.

Define the owner-written bulk export record used by Task 6:

```cpp
struct alignas(64) CxlAllocationRecord {
  le64 recordSequence;
  le64 sessionGeneration;
  le64 ownerGeneration;
  le64 allocationGeneration;
  le64 allocationBaseOffset;
  le64 allocationLength;
  le32 ownerEndpoint;
  le32 arenaId;
  le32 stateAndPermissions;
  le32 recordCrc32c;
};
static_assert(sizeof(CxlAllocationRecord) == 64);
```

The record's array index is the `allocationSlot` carried by a remote handle.
Only its arena owner writes it. `stateAndPermissions` encodes
`Free|Exported|Retiring` plus read/write bits; readers reject unknown bits.
Use the same final-even sequence/CRC protocol and generation rules as other
owner records.

The immutable lane slot, not any mutable endpoint/lane/lifecycle record, owns
lane ID, allocation authority and range metadata. Phase 1's bootstrap result binds the slot to
endpoint/service/plane identities; phase 2's manifest supplies that binding.
Each side has a different record and never writes its peer's cache line. Use an
owner-written odd/even `recordSequence` snapshot: relaxed odd store without
RMW, a sequentially consistent fence, relaxed lock-free atomic body-word
stores, then release even store. Readers use acquire sequence loads around
relaxed body-word loads plus an acquire fence. Use Castagnoli CRC over the
complete 64-byte final-even encoding with `crc32c=0`; require every reserved
field defined by that record type to be zero.

Use explicit little-endian encode/decode helpers at the mapping boundary rather
than serializing the native struct with serde. The aliases document the wire
order and are permitted only because the build-time endian assertion makes an
unsupported host fail instead of byte-swapping implicitly.

Make both `std::atomic_ref<uint32_t>::is_always_lock_free` and
`std::atomic_ref<uint64_t>::is_always_lock_free` compile-time requirements for
the ABI. G0 repeats both checks on aligned words in the actual DAX mapping;
linking `libatomic` is not a permitted substitute.

Use reflected Castagnoli CRC32C (`0x82F63B78`) over the encoded 64-byte entry
with `crc32c=0`, followed by exactly `payloadLength` bytes. Add the standard
`123456789 -> 0xE3069283` vector and header-bit/payload-bit corruption tests;
do not use IEEE `zlib.crc32`.

- [ ] **Step 4: Define the region and layout API**

`CxlRegion` owns an fd and mapping and is movable but not copyable.
`checkedRange(offset, length)` checks addition overflow, mapped length and
requested alignment before returning `std::span<std::byte>`. `mapFile` uses
the regular file's `st_size`. `mapDax` requires a character device, resolves
its devdax sysfs identity, reads `size` and `align`, and requires offset/base/
length alignment to the stricter of that value and the system page size; it
must not use the character device's `st_size` as capacity. Destruction unmaps
and closes exactly once.

`CxlLayout` validates non-overlapping superblock, endpoint directory, lane
directory, cursor, ring, payload, allocation export-directory, arena and
evidence ranges. It rejects unsupported ABI version, non-64-byte cache lines,
non-little-endian marker, zero generation and a non-READY superblock.

For each direction, validate a producer cursor, consumer cursor and delivered
record. Each is manifest-derived and has exactly one writer. The delivered
record resides on a separate 64-byte cache line in the consumer-owned cursor
page, starts at offset zero, is monotonic, and may advance within the current
cell but never beyond the contiguous published stream.

The manifest identifies one fabric-authority endpoint. Its process alone may
initialize and transition the superblock; attach mode is read-only for those
global words and never zeroes a poisoned or stale region. The authority writes
the canonical immutable layout and CRC before release-publishing `READY` and
remains the sole lifecycle writer through `DRAINING`/`RETIRED`. Other
endpoints acquire-wait for exact session/layout identity, then initialize only
their owner-specific cursor/state/arena/counter pages. Authority failure faults
the session; there is no takeover in phase 1.

- [ ] **Step 5: Run focused ABI and mapping tests**

Run:

```bash
cmake --build build/cxl-g0-host --target test_common
build/cxl-g0-host/tests/test_common --gtest_filter='TestCxlAbi.*:TestCxlRegion.*:TestCxlLayout.*'
```

Expected: all selected tests PASS, including separately mapped views of the same temporary file.

- [ ] **Step 6: Run sanitizer tests for mapping lifetime**

Run the same filter from a Debug build configured with `-DSANITIZER=ASAN`;
run a second build with `-DSANITIZER=UBSAN` for integer and alignment checks.

Expected: PASS with no leak, double-close, unaligned access or integer-overflow report.

- [ ] **Step 7: Record the uncommitted checkpoint**

Run `git diff --check && git status --short`.

## Task 3: Implement owner-separated SPSC lanes and byte-stream segmentation

**Files:**

- Create: `src/common/net/cxl/CxlLane.h`
- Create: `src/common/net/cxl/CxlLane.cc`
- Create: `tests/common/net/cxl/TestCxlLane.cc`

**Interfaces:**

- Produces: `CxlLane::tryPush(Direction, FrameView) -> Result<size_t>`.
- Produces: `CxlLane::tryPop(Direction, MutableByteRange) -> Result<size_t>`.
- Produces: `readable(Direction)`, `writable(Direction)`,
  `peerDeliveredOffset(Direction)` and `retire(Status)`.
- Consumes: validated layout and region ranges from Task 2.
- Consumes later: `CxlSocket` maps byte-stream send/recv onto these calls.

- [ ] **Step 1: Write queue safety and byte-stream tests**

```cpp
TEST_F(TestCxlLane, FullQueueDoesNotOverwrite) {
  for (uint64_t i = 0; i < kDepth; ++i) ASSERT_OK(producer_.push(bytes(i)));
  auto before = snapshotSlot(producer_, 0);
  ASSERT_ERROR(StatusCode::kQueueFull, producer_.push(bytes(kDepth)));
  EXPECT_EQ(snapshotSlot(producer_, 0), before);
}

TEST_F(TestCxlLane, ConsumerControlsReuse) {
  fillQueue();
  ASSERT_RESULT_EQ(consumer_.pop(buffer_), payload(0));
  ASSERT_OK(producer_.push(payload(kDepth)));
  EXPECT_EQ(consumer_.nextSequence(), 1);
}

TEST_F(TestCxlLane, ReassemblesAcrossWrap) {
  auto message = deterministicBytes(3 * kCellBytes + 17);
  transferWithInterleavedProgress(message);
  EXPECT_EQ(received_, message);
}

TEST(TestCxlLane, CursorAtomicIsAlwaysLockFree) {
  static_assert(std::atomic_ref<uint64_t>::is_always_lock_free);
}
```

Add negative cases for stale session/lane generation, bad CRC, duplicate
sequence, non-zero reserved fields, a gapped/overlapping/reordered
`streamOffset`, invalid flag combinations, stream-offset overflow, and a
`payloadOffset` unequal to the immutable cell assigned to that sequence. Add
receive buffers smaller than one cell and verify that the unread suffix
remains ordered and is CRC-validated before its first byte is returned.
Verify that each successful partial pop publishes exactly the returned byte
count in a valid consumer-owned `CxlDeliveredRecord` before returning, while
the consumer slot cursor advances only after the complete cell is returned.
Also seed cursors near `UINT64_MAX`: the producer must drain and retire the lane
before arithmetic wraps, while ordinary ring-index wrap continues to work.

- [ ] **Step 2: Run tests and verify the lane is absent**

Run `cmake --build build/cxl-g0-host --target test_common`.

Expected: compilation FAIL on `CxlLane`.

- [ ] **Step 3: Implement cursor publication and backpressure**

Use aligned cursor words accessed through `std::atomic_ref<uint64_t>`:

```cpp
auto producer = producerRef.load(std::memory_order_relaxed);
auto consumer = consumerRef.load(std::memory_order_acquire);
if (producer - consumer == depth_) return makeError(StatusCode::kQueueFull);

writePayload(slot(producer), segment);
writeEntry(slot(producer), entryFor(producer, nextStreamOffset, segment));
std::atomic_thread_fence(std::memory_order_release);
producerRef.store(producer + 1, std::memory_order_release);
```

Make `std::atomic_ref<uint64_t>::is_always_lock_free` a compile-time
requirement in `CxlLane`; G0 repeats the check at runtime on an aligned word in
the DAX mapping. Linking `libatomic` for unrelated project code does not permit
an emulated cursor atomic.

The consumer acquire-loads producer, validates and copies the entry/payload,
then publishes a new `CxlDeliveredRecord` with the odd/even sequence, complement
and CRC protocol before returning those bytes. It release-stores the consumer
slot cursor only after the whole cell has been returned. The cursor pages are
selected by owner role so a process never writes the peer-owned cursor or
delivered record. The producer obtains a stable acquire snapshot of the peer
record for failure classification but never modifies it.

Reject `consumer > producer` and `producer - consumer > depth_` as shared-state
corruption. Do not use modular comparison for absolute cursors. At
`UINT64_MAX - depth_`, stop accepting frames, drain, retire and reconnect with
the next nonzero lane generation, whose cursors start at zero.

- [ ] **Step 4: Implement ordered stream segments**

Use 64 KiB default cells and monotonically increasing byte offsets scoped to
one direction and lane generation. The lane validates and transfers segments;
it does not parse `MessageHeader`, attach request UUIDs or infer message
boundaries. Preserve a partially consumed cell locally until `recv()` has
returned its unread suffix. Missing, duplicate, reordered, overlapping or
gapped segments fault and retire the lane. Message-size validation stays in
the existing `Transport::doRead()` path.

- [ ] **Step 5: Run lane tests under stress**

Run:

```bash
cmake --build build/cxl-g0-host --target test_common
build/cxl-g0-host/tests/test_common \
  --gtest_filter='TestCxlLane.*' --gtest_repeat=100 --gtest_break_on_failure
```

Expected: every repetition PASS; queue-full tests report no slot mutation.

- [ ] **Step 6: Record the uncommitted checkpoint**

Run `git diff --check && git status --short`.

## Task 4: Add the progress engine and CxlSocket

**Files:**

- Create: `src/common/net/cxl/CxlProgressEngine.h`
- Create: `src/common/net/cxl/CxlProgressEngine.cc`
- Create: `src/common/net/cxl/CxlSocket.h`
- Create: `src/common/net/cxl/CxlSocket.cc`
- Create: `src/common/net/cxl/CxlMetrics.h`
- Create: `src/common/net/cxl/CxlMetrics.cc`
- Create: `tests/common/net/cxl/TestCxlSocket.cc`

**Interfaces:**

- Produces: `CxlProgressEngine::registerSocket`, `unregisterSocket`, `start`, `stopAndJoin`.
- Produces: `CxlSocket : Socket` with non-blocking `send`, `recv`, `poll`, `flush`, `check`, `close`.
- Produces: `CxlSocket::publicationSnapshot()` with same-generation accepted,
  cursor-published and peer-delivered byte watermarks.
- Produces: `CxlSocket::createConnectedPairForTest(CxlRegion&, const CxlLayout&)`.
- Consumes: `CxlLane` from Task 3.

- [ ] **Step 1: Write Socket-contract and lost-wakeup tests**

```cpp
TEST_F(TestCxlSocket, PartialSendAndRecvPreserveByteStream) {
  auto payload = deterministicBytes(5 * kCellBytes + 3);
  sendWithProgress(*client_, payload);
  EXPECT_EQ(recvUntilComplete(*server_, payload.size()), payload);
}

TEST_F(TestCxlSocket, EventFdSignalsReadableAndWritableTransitions) {
  armRead(*server_);
  ASSERT_OK(client_->send(oneByteIovec(), 1));
  EXPECT_TRUE(waitFd(server_->fd(), EPOLLIN, 1s));
  EXPECT_TRUE(ASSERT_RESULT(server_->poll(Socket::kEventReadableFlag)) & Socket::kEventReadableFlag);
}

TEST_F(TestCxlSocket, ArmThenPublishCannotLoseWakeup) {
  repeatRace(10000, [&] { armAndPublishConcurrently(*server_, *client_); });
  EXPECT_EQ(timeouts_, 0);
}

TEST_F(TestCxlSocket, AcceptedPublishedAndDeliveredAreDistinct) {
  ASSERT_RESULT_EQ(client_->send(oneByteIovec(), 1), 1);
  EXPECT_EQ(client_->publicationSnapshot()->acceptedOffset, 1);
  EXPECT_EQ(client_->publicationSnapshot()->publishedOffset, 0);
  ASSERT_OK(client_->flush());
  EXPECT_EQ(client_->publicationSnapshot()->publishedOffset, 1);
  ASSERT_RESULT_EQ(server_->recv(oneByteRange()), 1);
  EXPECT_EQ(client_->publicationSnapshot()->peerDeliveredOffset, 1);
}
```

- [ ] **Step 2: Run tests and observe missing classes**

Run `cmake --build build/cxl-g0-host --target test_common`.

Expected: compilation FAIL for missing progress/socket classes.

- [ ] **Step 3: Implement local eventfd integration**

Create each eventfd with `EFD_NONBLOCK | EFD_CLOEXEC`. The progress thread scans registered lane cursors and writes value `1` only when local readiness transitions from false to true. `poll()` drains until `EAGAIN`, computes readiness from shared state, publishes the local armed mask, and rechecks shared state before returning.

Use bounded wait configuration:

```cpp
struct CxlPollConfig {
  Duration spin = 20_us;
  uint32_t yields = 8;
  Duration sleep = 10_us;
};
```

Record spin nanoseconds, yields, sleeps and wakeups.

- [ ] **Step 4: Implement the Socket methods**

`send` treats the iovecs as one continuation of an ordered byte stream. Before
it accepts the first byte into a local staging cell, it reserves a currently
free SQ slot; a full cell is published immediately. It returns zero when it
has neither a reserved cell nor free credit. `recv` copies at most the supplied
range, including partial-cell reads, and returns zero when no bytes are
available. `flush` publishes a non-empty reserved cell and is otherwise a
no-op; it must not need to acquire credit. `check` validates READY state and
generations. `close` unregisters, closes eventfd, initiates drain and is
idempotent. Add an invariant test proving every positive `send()` return is
either already published or held by exactly one reserved slot that `flush()`
can publish without blocking.

Bind each socket to exactly one `IOWorker`: only its serialized write task may
call `send`/`flush`, and only its serialized read task may call `recv`. The
existing MPSC `WriteList` is the multi-caller ingress. The progress engine may
only observe shared cursors and signal the local eventfd. Add a negative test
that a debug ownership check rejects a cross-worker producer call rather than
turning the shared lane into an accidental MPSC queue.

`publicationSnapshot()` returns `trustworthy=false` if any generation,
cursor or delivered-watermark invariant fails. It never substitutes a stale
watermark from another lane generation. The producer updates accepted and
published offsets in local state; it acquire-loads the peer-owned delivered
watermark. Require
`peerDeliveredOffset <= publishedOffset <= acceptedOffset`, monotonicity and
checked non-wrapping arithmetic. Add a two-small-message/one-cell test and a
cell-boundary test to
show that byte-range progress does not depend on frame boundaries.

Construct each socket with the bootstrap/manifest-declared logical peer
`Address`. `peerIP()` returns that declared value for existing logging and
diagnostics; it is not evidence of a TCP connection and is not an
authentication credential. CXL transports never synthesize Unix peer
credentials.

- [ ] **Step 5: Run socket, echo-size and sanitizer tests**

Run:

```bash
cmake --build build/cxl-g0-host --target test_common
build/cxl-g0-host/tests/test_common --gtest_filter='TestCxlSocket.*'
```

Expected: all selected tests PASS, including zero-length, 512 MiB boundary, partial iovec and concurrent close cases.

- [ ] **Step 6: Record the uncommitted checkpoint**

Run `git diff --check && git status --short`.

## Task 5: Integrate CXL with Transport, IOWorker, Listener and TCP bootstrap

**Files:**

- Create: `src/common/net/TransportRuntime.h`
- Create: `src/common/net/TransportRuntime.cc`
- Create: `src/common/net/PublicationLedger.h`
- Create: `src/common/net/PublicationLedger.cc`
- Create: `src/common/net/cxl/CxlFabric.h`
- Create: `src/common/net/cxl/CxlFabric.cc`
- Create: `src/common/net/cxl/CxlEndpointState.h`
- Create: `src/common/net/cxl/CxlEndpointState.cc`
- Create: `src/common/net/cxl/CxlConnectService.h`
- Create: `src/common/net/cxl/CxlConnectService.cc`
- Create: `src/tools/cxl-fabricd/CMakeLists.txt`
- Create: `src/tools/cxl-fabricd/main.cc`
- Modify: `src/tools/CMakeLists.txt`
- Create: `tests/common/net/cxl/TestCxlConnect.cc`
- Create: `tests/common/net/cxl/TestCxlFabric.cc`
- Create: `tests/common/net/cxl/TestCxlEndpointState.cc`
- Create: `tests/common/net/TestPublicationLedger.cc`
- Modify: `src/common/app/ApplicationBase.cc`
- Modify: `src/common/app/OnePhaseApplication.h`
- Modify: `src/common/net/Transport.cc:53-327`
- Modify: `src/common/net/WriteItem.h`
- Modify: `src/common/net/WriteItem.cc`
- Modify: `src/common/net/Waiter.h`
- Modify: `src/common/net/IOWorker.h:23-126`
- Modify: `src/common/net/IOWorker.cc:13-136`
- Modify: `src/common/net/Listener.h:18-78`
- Modify: `src/common/net/Listener.cc`
- Modify: `src/common/net/ServiceGroup.cc`
- Modify: `src/common/net/Server.cc`
- Modify: `src/common/serde/ClientContext.h`
- Modify: `src/core/app/ServerAppConfig.h`
- Modify: `src/core/app/ServerLauncher.h`
- Modify: `src/core/app/ServerLauncherConfig.h`
- Modify: `src/fuse/FuseAppConfig.h`
- Modify: `src/fuse/FuseConfig.h`
- Modify: `src/fuse/FuseLauncherConfig.h`
- Modify: `src/fuse/hf3fs_fuse.cpp`
- Modify: `src/mgmtd/MgmtdLauncherConfig.h`
- Modify: `src/client/bin/admin_cli.cc`
- Modify: `src/tools/admin.cc`
- Modify: `tests/common/net/TestEcho.cc`

**Interfaces:**

- Produces: `CxlConnectReq`, `CxlConnectRsp`, and `CxlConnectService` serde service.
- Produces: `cxl-fabricd --manifest PATH --device PATH --owner-lock PATH`,
  which initializes the layout before Core/FDB startup, owns the global
  lifecycle and exposes no serving RPC.
- Produces: `IOWorker::addCxlSocket(std::unique_ptr<CxlSocket>)`.
- Produces: `Transport::connect(ServiceEndpoint, Duration)` CXL branch after
  exact address-to-plane resolution.
- Produces: `Transport::closeDataPlane()` with backend-correct drain/retire semantics.
- Produces: publication-aware `CompletionDisposition` for every request that
  times out or is interrupted; it is recorded separately from `Status`.
- Produces: `PublicationLedger`, which maps ordered request byte ranges to one
  same-generation `PublicationSnapshot` and retains retryable serialized
  buffers until peer delivery.
- Produces: `TransportRuntime::start/stop` and a process-scoped `CxlFabric`
  shared by all I/O workers.
- Produces: `CxlFabric::StartMode::InitializeAuthority|Attach`; the exact mode
  comes from the hash-addressed, runner-owned immutable manifest, not a
  process-local default.
- Produces: `CxlEndpointState::publish`, `snapshot`, `heartbeat` and `fault`
  for every phase-1 process; phase 2 reuses the same record and implementation.
- Consumes: validated CXL region/layout/progress objects from `CxlFabric`.

- [ ] **Step 1: Write bootstrap validation and end-to-end Echo tests**

```cpp
TEST_F(TestCxlConnect, RejectsWrongSessionGeneration) {
  auto req = validRequest();
  req.sessionGeneration++;
  auto rsp = blockingWait(service_->connect(req));
  ASSERT_FALSE(rsp.result);
  EXPECT_EQ(rsp.result.error().code(), RPCCode::kDataPlaneHandshakeFailed);
}

TEST_F(TestCxlConnect, BootstrapCarriesNoServingPayload) {
  connectClientAndServer();
  callEchoOverCxl("payload");
  EXPECT_EQ(metrics_.bootstrapServingBytes(), 0);
  EXPECT_GT(metrics_.cxlRpcBytes(), 0);
}
```

Add `TestCxlFabric` cases for one mapping shared by multiple I/O workers,
duplicate live participant claims, start-before-client ordering, partial-start
rollback and drain-before-unmap shutdown. Also prove an attach process cannot
format the region, a second authority is rejected, and authority death faults
all attachers without takeover.

Add `TestCxlEndpointState` cases for owner-only writes, 64-bit generation
matching, torn/CRC-corrupt snapshots, heartbeat progress without shared RMW,
stale-peer detection based on local observation time and
`Starting -> Ready -> Draining -> Retired` acknowledgements.

Add a `Transport` test whose socket returns an error from `flush()`. Require
the write path to invalidate the transport and classify/retry its pending work;
the error must not be ignored.

Add publication-ledger tests for two small requests coalesced into one cell,
a request crossing two cells, accepted-but-unflushed bytes, a published but
partially delivered request, a response racing the delivered watermark, and a
corrupt/wrong-generation snapshot. After trustworthy retirement, the first
request in a partially delivered cell may be `OutcomeUnknown` while the second
is `RejectedBeforeExecute`; frame boundaries must not force one disposition
for both. Verify the original serialized buffer remains available for the
second request's bounded retry.

- [ ] **Step 2: Run the test and observe no CXL factory branch**

Run `cmake --build build/cxl-g0-host --target test_common`.

Expected: compilation FAIL for `CxlConnectService` and `addCxlSocket`.

- [ ] **Step 3: Define bootstrap messages and lane allocation**

The request contains ABI version, session generation, requester
endpoint/generation, target endpoint/service plane and requested queue geometry.
The response contains a Result, lane ID, lane generation, exact region offsets
and a 128-bit nonce. The acceptor owns the lane allocator and rejects duplicate
active endpoint/generation/nonce tuples. Each side then publishes only its own
`CxlLaneOwnerRecord`; `CxlSocket` becomes ready only after both records have
stable even sequences and exactly matching session, lane generation, nonce and
capability bits, with each writer's endpoint generation matching its stable
endpoint record.

The run ledger supplies a nonzero `getrandom` session generation and rejects a
duplicate; fresh sessions also use fresh backing. Endpoint generations are
64-bit incarnation ordinals, initially one and incremented exactly once per
runner-authorized process restart. The bootstrap acceptor similarly owns each
lane slot's nonzero generation ordinal and advances it before reuse. It obtains
the high-water value from the last stable shared owner record, never from a
process-local reset; a torn/corrupt prior record forces a fresh session because
frames do not contain endpoint generation. Rollback, skips and checked overflow
fail closed. Each new owner record also binds its writer's current endpoint
generation.

- [ ] **Step 4: Add CXL branches without changing TCP behavior**

`Transport::create(ServiceEndpoint)` creates `CxlSocket` for `Address::CXL`;
`connect` invokes the TCP serde bootstrap at `endpoint.address.tcp()`, includes
the requested plane, and then verifies that the returned lane declares the
same plane. `Listener` registers `CxlConnectService` only in the Control table
and passes accepted lanes plus their declared plane to
`IOWorker::addCxlSocket`. Destruction switches on
`Socket::kind()`: TCP keeps its current synchronous close, RDMA keeps its
`IBManager::close` ownership handoff behind `HF3FS_ENABLE_RDMA`, and CXL
unregisters from `CxlProgressEngine`, retires the lane and closes its eventfd.

Guard every `IBSocket` include, config field, callback and factory branch in
`Transport`, `IOWorker` and `Listener` with `HF3FS_ENABLE_RDMA`; a CXL-only
translation unit must not need verbs headers. Add `CxlSocket::Config` to
`IOWorker::Config` under `HF3FS_ENABLE_CXL` and keep the serialized config
blocks conditional on the compiled backend.

Change `Transport::doWrite()` to check the `Result<Void>` returned by
`Socket::flush()`. On error, follow the same cleanup and publication-aware
retry path as a failed `send()`; do not suspend the writer after a failed
flush. This applies consistently to TCP, RDMA and CXL sockets.

For a socket with publication snapshots, assign each ordered `WriteItem` an
immutable `[beginOffset, endOffset)` when it enters that lane generation.
Extend `WriteListWithProgress::advance()` with a completion sink instead of
unconditionally destroying a fully accepted request item. The CXL sink moves
that item into `PublicationLedger`; responses may be released after cursor
publication, but request buffers stay retryable until their end offset is
peer-delivered or a final response arrives. Query the snapshot after every
`send()`, successful `flush()`, writable event and close transition. TCP and
RDMA retain their existing unavailable-snapshot behavior.

On CXL failure, first stop new publication, take one acquire snapshot for the
matching generation, and retire/drain the lane. A request with
`peerDeliveredOffset < endOffset` becomes retryable only after trustworthy
retirement guarantees that its missing suffix can never be published. A
request with `peerDeliveredOffset >= endOffset` is `OutcomeUnknown` without a
final response. If the snapshot generation/invariants are untrustworthy,
report `LaneRetired`, treat it as unknown for business retry policy and do not
automatically replay it. A final response always resolves the matching UUID to
`Completed`.

Replace unconditional `IBManager` calls in the application, launcher, FUSE and
admin entry points with `TransportRuntime`. Its local configuration contains a
conditional `cxl` block and, only in an RDMA-enabled build, an `ib_devices`
block. `CxlFabric` maps and validates the DAX region once, claims exactly one
process-unique participant ID/generation, and owns the progress engine, local
lane allocator and bulk arena. It must be ready before constructing a client
that fetches remote config. Shutdown first drains clients/servers, then stops
progress, retires lanes and unmaps; duplicate initialization or two live
processes claiming the same participant fails closed. Remove now-unused IB
headers from `Server`, app configs and launchers.

`CxlFabric::start()` selects `InitializeAuthority` only when the immutable
manifest names the dedicated `cxl-fabricd` endpoint as fabric authority; every
application/netd endpoint uses `Attach`. The authority daemon starts before
Core/FDB and stays alive as global lifecycle owner. Normal shutdown
first disables submission and publishes global `DRAINING`, then waits for
every endpoint's drained acknowledgement and publishes `RETIRED` before
unmapping. If it dies,
all peers fault the session and the runner creates a fresh epoch/backing set;
no process promotes itself or repairs the old superblock.

Each attaching process publishes its phase-1 `CxlEndpointRecord` as
`Starting`, claims only the manifest-owned cursor/arena/counter ranges, then
publishes `Ready`. Its heartbeat thread advances the owner record from a local
counter. Shutdown publishes `Draining` only after submissions stop and
`Retired` only after its lanes and bulk operations drain. `cxl-fabricd` waits
for these endpoint acknowledgements before the global transition; timeout or
an untrustworthy endpoint snapshot fails the session rather than treating a
stale zero as an acknowledgement.

Because fabricd is always placed in exactly one declared guest, it also takes
an exclusive nonblocking `flock` on a run-owned local owner file whose token
and manifest hash match the runner receipt. A second local authority exits
before mapping. The shared manifest/endpoint check rejects an attach process
calling the authority API; this is a trusted-cluster correctness guard, not a
claim of hardware memory protection against a malicious guest.

Rename shared data-plane settings
`read_write_rdma_in_event_thread`/`rdma_connect_timeout` to
`read_write_data_in_event_thread`/`data_connect_timeout`, then update the Echo
parameter tests to cover `Address::CXL`, TCP and UNIX. Keep verbs-only tuning in
the guarded `ibsocket` subsection.

Replace `closeIB()` in `ClientContext` timeout handling with
`closeDataPlane()`. RDMA retains its graceful-QP close; CXL records the
published-request disposition and retires the lane without reusing live cells;
TCP control behavior remains unchanged. Use the request's stream range and
peer-delivered watermark, not `send()`'s accepted byte count or a ring-frame
boundary, to choose `RejectedBeforeExecute` versus `OutcomeUnknown`.
`LaneRetired` is reserved for retirement with an untrustworthy publication
snapshot and is handled at least as conservatively as unknown. Preserve the
original RPC UUID for reconciliation and never encode these dispositions as
ad-hoc new status-code names.

- [ ] **Step 5: Run existing and new network tests**

Run:

```bash
cmake --build build/cxl-g0-host --target test_common
build/cxl-g0-host/tests/test_common \
  --gtest_filter='TestCxlFabric.*:TestCxlConnect.*:TestCxlSocket.*:TestEcho.*:TestEchoListener.*'
```

Expected: all selected tests PASS; TCP Echo behavior is unchanged and CXL Echo increments only CXL serving counters.

- [ ] **Step 6: Record the uncommitted checkpoint**

Run `git diff --check && git status --short`.

## Task 6: Introduce generic buffers and bulk transfer

**Files:**

- Create: `src/common/net/Buffer.h`
- Create: `src/common/net/Buffer.cc`
- Create: `src/common/net/RemoteExportLease.h`
- Create: `src/common/net/RemoteExportLease.cc`
- Create: `src/common/net/BulkTransfer.h`
- Create: `src/common/net/BulkTransfer.cc`
- Create: `src/common/net/BulkControl.h`
- Create: `src/common/net/BulkControl.cc`
- Create: `src/common/net/cxl/CxlBuffer.h`
- Create: `src/common/net/cxl/CxlBuffer.cc`
- Create: `src/common/net/cxl/CxlBulkTransfer.h`
- Create: `src/common/net/cxl/CxlBulkTransfer.cc`
- Create: `src/common/net/ib/RDMABulkTransfer.h`
- Create: `src/common/net/ib/RDMABulkTransfer.cc`
- Create: `tests/common/net/cxl/TestCxlBuffer.cc`
- Create: `tests/common/net/cxl/TestCxlBulkTransfer.cc`
- Create: `tests/common/net/ib/TestRDMABulkTransfer.cc`
- Create: `tests/common/net/TestBulkControl.cc`
- Delete after replacement: `src/common/net/RDMAControl.h`
- Delete after replacement: `src/common/net/RDMAControl.cc`
- Delete after replacement: `tests/common/net/TestRDMAControl.cc`
- Modify: `src/common/net/Client.h`
- Modify: `src/common/net/RequestOptions.h`
- Modify: `src/common/net/Waiter.h`
- Modify: `src/common/net/ib/IBSocket.h`
- Modify: `src/common/net/ib/IBSocket.cc`
- Modify: `src/common/serde/MessagePacket.h`
- Modify: `src/common/serde/ClientContext.h`
- Modify: `src/common/serde/CallContext.h:94-114`
- Modify: `src/common/serde/CallContext.cc`
- Modify: `tests/common/net/Echo.h`
- Modify: `tests/common/net/TestEcho.cc`

**Interfaces:**

- Produces: `SharedBuffer`, `RemoteBufferHandle`, `RemoteExportLease`,
  `BulkTransfer`, `BulkTransferBatch`.
- Produces: `CxlBufferArena::allocate(size_t) -> CoTryTask<SharedBuffer>`.
- Produces: `SharedBuffer::exportRemote(RemoteAccess) -> Result<RemoteExport>`,
  where `RemoteExport` owns both the handle and its move-only lease.
- Produces: `BulkTransfer::pull` and `push`.
- Produces: `BulkControl`, `BulkTransmissionLimiter` and the neutral
  `ControlBulk` request flag while preserving service ID 10 and flag bit 2.
- Produces: an optional `RDMABulkTransfer` adapter so an RDMA-enabled source-audit build still compiles without changing business-layer types.
- Consumes later: storage client/server and forwarding use no RDMA buffer types.

- [ ] **Step 1: Write allocation, subrange, permission and generation tests**

```cpp
TEST_F(TestCxlBuffer, SubrangeKeepsAllocationGeneration) {
  auto buffer = ASSERT_RESULT(arena_.tryAllocate(4096));
  auto exported = ASSERT_RESULT(buffer.subrange(512, 1024).exportRemote(RemoteAccess::Read));
  const auto &handle = exported.handle();
  EXPECT_EQ(handle.offset(), buffer.offset() + 512);
  EXPECT_EQ(handle.length(), 1024);
  EXPECT_EQ(handle.allocationSlot(), buffer.allocationSlot());
  EXPECT_EQ(handle.allocationGeneration(), buffer.allocationGeneration());
}

TEST_F(TestCxlBulkTransfer, RejectsWriteThroughReadOnlyHandle) {
  auto handle = readOnlyHandle(4096);
  ASSERT_ERROR(RPCCode::kRemoteBufferAccessDenied,
               blockingWait(transfer_.push(handle, local_)));
}

TEST_F(TestCxlBulkTransfer, TimeoutDoesNotFreeAllocation) {
  auto allocation = allocateAndPublish();
  forceConsumerDelay();
  ASSERT_ERROR(RPCCode::kTimeout, blockingWait(transfer_.push(allocation.handle, local_)));
  EXPECT_TRUE(arena_.isAllocated(allocation.handle));
}

TEST_F(TestCxlBulkTransfer, ReusedSlotRejectsStaleSubrangeHandle) {
  auto stale = exportSubrangeThenRelease(/*offset=*/512, /*length=*/1024);
  auto replacement = allocateInSameSlot();
  ASSERT_NE(stale.allocationGeneration(), replacement.allocationGeneration());
  ASSERT_ERROR(RPCCode::kStaleGeneration,
               blockingWait(transfer_.pull(stale, local_)));
}
```

- [ ] **Step 2: Build and observe missing generic buffer APIs**

Run `cmake --build build/cxl-g0-host --target test_common`.

Expected: compilation FAIL on `SharedBuffer` and `BulkTransfer`.

- [ ] **Step 3: Define the stable generic interfaces**

```cpp
struct RemoteBufferHandle {
  le16 abiVersion;
  uint8_t transportKind;
  uint8_t permissions;
  le32 ownerEndpoint;
  le32 arenaId;
  le32 allocationSlot;
  le64 sessionGeneration;
  le64 ownerGeneration;
  le64 allocationGeneration;
  le64 offset;
  le64 length;
  le32 checksum;
  le32 reserved0;
};
static_assert(sizeof(RemoteBufferHandle) == 64);

class BulkTransfer {
 public:
  virtual ~BulkTransfer() = default;
  virtual CoTryTask<void> pull(const RemoteBufferHandle &, std::span<SharedBuffer>) = 0;
  virtual CoTryTask<void> push(const RemoteBufferHandle &, std::span<SharedBuffer>) = 0;
};

class BulkTransferBatch {
 public:
  Result<Void> add(RemoteBufferHandle remote, SharedBuffer local);
  CoTryTask<void> post();
};
```

Encode the reserved field as zero and compute Castagnoli CRC32C over all 64 bytes
with the `checksum` field zeroed. Reject any nonzero reserved value or checksum
mismatch before consulting the arena.

`SharedBuffer` holds shared ownership of storage, a local pointer, range, and
optional remote-export metadata. Its destructor returns a CXL allocation only
after the owner marks it releasable, no local views remain and the export-lease
count is zero.

`exportRemote()` returns a `RemoteExport` whose move-only lease pins the
allocation. `Client`, `Waiter` and `CallContext` bind that lease to the request
UUID, session, lane generation and assigned stream range and transfer it into
`PublicationLedger` before any request byte can be accepted. `Completed` and
`RejectedBeforeExecute` release it at their defined safe points.
`OutcomeUnknown` retains it until request reconciliation. `LaneRetired` also
retains it until a session or owner-endpoint generation transition plus
membership evidence makes the old handle inaccessible; retiring only the lane
is insufficient because `RemoteBufferHandle` deliberately contains no lane
generation. A caller-visible timeout cannot destroy the last lease. Add race
tests for validate-then-timeout, accepted-but-unpublished request bytes,
partial peer delivery, delayed remote copy and arena-slot reuse.

- [ ] **Step 4: Implement endpoint-owned CXL arenas and coherent copies**

Only the local endpoint allocator mutates its bitmap/free lists and fixed export
directory. Allocation selects an `allocationSlot` and increments its generation.
Export publishes that slot's `CxlAllocationRecord`, including the full base and
allocation length, before returning a subrange handle. `pull` copies from the validated remote DAX
range into local buffers; `push` copies local buffers into the validated remote
range and publishes completion with a release operation. Both verify exact
aggregate lengths and account bytes.

The remote side indexes the owner-written allocation record by
`allocationSlot`, validates its final-even CRC snapshot, owner/arena, active
permissions, full allocation range and generations before copying. Correctness
does not depend on that snapshot staying live
by chance: the request's export lease prevents reuse until the acknowledged or
reconciled completion disposition permits release.

- [ ] **Step 5: Add the optional verbs compatibility adapter**

`RDMABulkTransfer` lives entirely under `common/net/ib`. It validates
`transportKind == RDMA`, treats `offset` as the existing remote virtual address
and `arenaId` as the verbs rkey, then calls the current `IBSocket` batch API.
`IBSocket::bulkTransfer()` returns this adapter. A pure descriptor-translation
test compiles and runs without opening a verbs device; hardware behavior tests
remain optional and are not a phase-1 gate.

This adapter preserves source/build compatibility only. The new 64-byte
storage handle is a protocol-versioned wire change, so mixed old/new 3FS
processes must reject one another during negotiation rather than attempting
legacy interoperability.

- [ ] **Step 6: Neutralize the delayed-transfer control service**

Rename `RDMAControl` to `BulkControl`, `RDMATransmissionLimiter` to
`BulkTransmissionLimiter`, request option `enableRDMAControl` to
`enableBulkControl`, and `EssentialFlags::ControlRDMA`/`controlRDMA()` to
`ControlBulk`/`controlBulk()`. Preserve service ID `10`, method ID `1`, flag bit
`2`, request UUID behavior, concurrency limiting and the point at which the
limiter is released by `Waiter::post/error`.

Rename metrics from `common.rdma_control.*` and
`common.apply_rdma_transmission*` to `common.bulk_control.*` and
`common.apply_bulk_transmission*`. Add a test in which a CXL response releases
the limiter exactly once after its bulk completion; cover timeout and duplicate
response paths.

Replace the Echo test payload's `rdma_bufs` field with neutral remote handles
and run the same delayed bulk-control cases over a file-backed CXL region. No
generic test header may include `common/net/ib/RDMABuf.h`.

- [ ] **Step 7: Replace `RDMATransmission` in CallContext**

Expose:

```cpp
BulkTransmission readTransmission();
BulkTransmission writeTransmission();
```

The constructor obtains `tr_->bulkTransfer()` and returns a precise
`RPCCode::kTransportCapabilityMissing` error if the active transport has no bulk
capability. It never casts to `IBSocket` or names a verbs opcode.

- [ ] **Step 8: Run focused buffer and bulk tests**

Run:

```bash
cmake --build build/cxl-g0-host --target test_common
build/cxl-g0-host/tests/test_common \
  --gtest_filter='TestCxlBuffer.*:TestCxlBulkTransfer.*:TestBulkControl.*'
```

Expected: all selected tests PASS, including stale-handle, overflow, zero-length, multi-segment and timeout-lifetime cases.

- [ ] **Step 9: Configure the optional RDMA source-audit build**

On a host with verbs headers/libraries but without requiring an RDMA device:

```bash
cmake -S . -B build/cxl-rdma-source-audit -G Ninja \
  -DSHUFFLE_METHOD=stdshuffle \
  -DHF3FS_ENABLE_CXL=ON \
  -DHF3FS_ENABLE_RDMA=ON
cmake --build build/cxl-rdma-source-audit --target common test_common
build/cxl-rdma-source-audit/tests/test_common \
  --gtest_filter='TestRDMABulkTransfer.DescriptorTranslation'
```

Expected: build and pure translation test PASS; no test opens an RDMA device.

- [ ] **Step 10: Record the uncommitted checkpoint**

Run `git diff --check && git status --short`.

## Task 7: Move the storage wire format and client buffers to generic handles

**Files:**

- Modify: `src/fbs/storage/Common.h:309-405`
- Modify: `src/client/storage/StorageClient.h:44-66,416-527`
- Modify: `src/client/storage/StorageClient.cc`
- Modify: `src/client/storage/StorageClientImpl.cc`
- Modify: `src/fuse/FuseClients.h`
- Modify: `src/fuse/FuseClients.cc`
- Modify: `src/fuse/FuseConfig.h`
- Modify: `src/fuse/UserConfig.h`
- Modify: `src/fuse/UserConfig.cc`
- Modify: `src/client/cli/admin/FileWrapper.cc`
- Modify: `src/client/cli/admin/FillZero.cc`
- Modify: `src/client/cli/admin/QueryChunk.cc`
- Modify: `src/client/cli/admin/ReadBench.cc`
- Modify: `tests/storage/store/TestCommonStruct.cc`
- Modify: `tests/storage/client/TestStorageClientInterface.cc`
- Modify: `tests/common/serde/TestSerdeTo.cc`
- Modify: `tests/common/serde/TestService.cc`
- Modify: `tests/lib/UnitTestFabric.cc`

**Interfaces:**

- Produces: `ReadIO::remoteBuf` and `UpdateIO::remoteBuf` of type `RemoteBufferHandle`.
- Produces: `StorageClient::allocateIOBuffer(size_t) -> Result<IOBuffer>`.
- Preserves: `registerIOBuffer(uint8_t*, size_t)` through a managed CXL shadow.
- Consumes: CXL arena and handle APIs from Task 6.

- [ ] **Step 1: Write wire round-trip and client-buffer tests**

```cpp
TEST(TestRemoteBufferHandleSerde, CxlRoundTrip) {
  auto expected = makeCxlHandle(/*owner=*/7, /*offset=*/1_MB, /*length=*/4096,
                                RemoteAccess::Read);
  auto encoded = serde::serialize(expected);
  RemoteBufferHandle actual;
  ASSERT_OK(serde::deserialize(actual, encoded));
  EXPECT_EQ(actual, expected);
}

TEST_F(TestStorageClientInterface, NativeCxlBufferExportsRequestedSubrange) {
  auto buffer = ASSERT_RESULT(client_->allocateIOBuffer(1_MB));
  auto io = client_->createWriteIO(chain_, chunk_, 0, 4096, chunkSize_,
                                   buffer.data() + 8192, &buffer);
  auto exported = ASSERT_RESULT(io.buffer->subrange(8192, 4096).exportRemote(RemoteAccess::Read));
  EXPECT_EQ(exported.handle().length(), 4096);
}
```

- [ ] **Step 2: Build and observe the old RDMA-only API failure**

Run `cmake --build build/cxl-g0-host --target test_storage`.

Expected: compilation FAIL because storage structs and IOBuffer still require RDMA types.

- [ ] **Step 3: Change storage request fields and serde handling**

Replace the serialized buffer field type while retaining its field position in
the structure. Validate the handle kind against the request transport before
processing. Add ABI-version rejection tests so mixed legacy/CXL requests fail
with a protocol error rather than being misread.

- [ ] **Step 4: Implement native and shadow IOBuffer modes**

`IOBuffer` owns a `SharedBuffer`. `allocateIOBuffer` returns CXL-mapped memory.
`registerIOBuffer` allocates a same-sized CXL shadow, copies user data into it
before writes, and copies completed read ranges back before returning to the
caller. Track `shadow_copy_in_bytes` and `shadow_copy_out_bytes` separately.

- [ ] **Step 5: Migrate FUSE, admin and shared test buffer consumers**

Replace `RDMABufPool` in `FuseClients`, `FileWrapper`, `FillZero`, `QueryChunk`
and `ReadBench` with the neutral `SharedBufferPool` backed by the configured
CXL arena. Rename `rdma_buf_pool_size` to `shared_buf_pool_size` and the user
configuration key `storage.net_client.rdma_control.max_concurrent_transmission`
to `storage.net_client.bulk_control.max_concurrent_transmission`. Replace
`RPCCode::kRDMANoBuf` at these call sites with
`RPCCode::kNoSharedBuffer` (the same numeric value 2019) while preserving the
original retry/return behavior.

Update serde round-trip tests to exercise both a CXL handle and an RDMA-tagged
handle without including `RDMABuf.h` in the generic test. Change
`UnitTestFabric` to configure CXL queue/bulk limits and `enable_bulk_control`;
IB tuning remains only in an RDMA-guarded test fixture.

- [ ] **Step 6: Select CXL routing endpoints**

Replace the hard-coded `Address::RDMA` filter in `StorageClientImpl` with
`Address::CXL`. If no CXL endpoint exists, return
`StorageClientCode::kNoDataPlaneInterface`. Do not select TCP as a substitute.

- [ ] **Step 7: Run storage wire/client tests**

Run:

```bash
cmake --build build/cxl-g0-host --target test_storage test_common
build/cxl-g0-host/tests/test_storage --gtest_filter='TestRemoteBufferHandleSerde.*'
build/cxl-g0-host/tests/test_storage --gtest_filter='TestStorageClientInterface.*'
build/cxl-g0-host/tests/test_common --gtest_filter='TestSerde.*RemoteBufferHandle*:TestService.*CXL*'
```

Expected: selected tests PASS for native and shadow buffers; malformed or wrong-kind handles fail closed.

- [ ] **Step 8: Record the uncommitted checkpoint**

Run `git diff --check && git status --short`.

## Task 8: Convert storage service, forwarding and resync to BulkTransfer

**Files:**

- Modify: `src/storage/service/StorageService.h:14-31`
- Modify: `src/storage/service/StorageOperator.h`
- Modify: `src/storage/service/StorageOperator.cc:82-230,340-614`
- Modify: `src/storage/service/ReliableUpdate.h`
- Modify: `src/storage/service/ReliableUpdate.cc`
- Modify: `src/storage/service/ReliableForwarding.h`
- Modify: `src/storage/service/ReliableForwarding.cc`
- Modify: `src/storage/service/BufferPool.h`
- Modify: `src/storage/service/BufferPool.cc`
- Modify: `src/storage/aio/BatchReadJob.h`
- Modify: `src/storage/aio/BatchReadJob.cc`
- Modify: `src/storage/sync/ResyncWorker.h`
- Modify: `src/storage/sync/ResyncWorker.cc`
- Create: `tests/storage/service/TestCxlStorageService.cc`
- Create: `tests/storage/sync/TestCxlSyncForward.cc`

**Interfaces:**

- Consumes: `CallContext::readTransmission/writeTransmission`, `SharedBuffer`, and `RemoteBufferHandle`.
- Produces: no storage service signature or state field that names RDMA.
- Preserves: stable per-hop forwarding buffers, checksums, commit versions and retry identities.

- [ ] **Step 1: Add end-to-end storage and chain tests on file-backed CXL**

```cpp
TEST_F(TestCxlStorageService, WritePullsThenReadPushesExactBytes) {
  auto source = nativeClientBuffer(deterministicBytes(4_MB));
  ASSERT_OK(writeChunk(source));
  auto destination = nativeClientBuffer(4_MB);
  ASSERT_OK(readChunk(destination));
  EXPECT_EQ(bytes(destination), bytes(source));
  EXPECT_EQ(metrics_.cxlBulkReadBytes(), 4_MB);
  EXPECT_EQ(metrics_.cxlBulkWriteBytes(), 4_MB);
}

TEST_F(TestCxlSyncForward, HeadUsesOwnedStableForwardBuffer) {
  pauseSuccessorBeforePull();
  auto request = startClientWrite(patternA());
  waitUntilHeadCopiedInput();
  overwriteClientView(patternB());
  resumeSuccessor();
  ASSERT_OK(request.get());
  EXPECT_EQ(readReplica(head_), patternA());
  EXPECT_EQ(readReplica(successor_), patternA());
  EXPECT_GT(metrics_.forwardOwnedCopyBytes(), 0);
}
```

Also cover batch reads, inline flags, checksum mismatch, RF=1, RF=2, successor restart and resync.

- [ ] **Step 2: Build and observe RDMA signatures blocking CXL tests**

Run `cmake --build build/cxl-g0-host --target test_storage`.

Expected: compilation FAIL on `IBSocket*`, `RDMARemoteBuf` or `RDMABuf` signatures.

- [ ] **Step 3: Replace service-level RDMA access**

Pass `CallContext` or `BulkTransfer&` to storage operators instead of
`ctx.transport()->ibSocket()`. Replace read response RDMA writes with
`writeTransmission`, and write request RDMA reads with `readTransmission`.
Replace per-IB-device semaphores with a transport-local concurrency limiter
keyed by endpoint/lane class. Rename `controlRDMA()` checks and
`rdma_transmission_req_timeout` configuration to `controlBulk()` and
`bulk_transmission_req_timeout`, preserving the exact before/after-storage-
semaphore ordering. Rename `BatchReadJob` RDMA-write counters to CXL bulk-push
counters and account only successfully validated bytes.

- [ ] **Step 4: Convert BufferPool to SharedBuffer**

Rename configuration fields to transport-neutral names while accepting legacy
TOML aliases for one release of the experimental branch:

```text
buffer_size, buffer_count, big_buffer_size, big_buffer_count
```

The pool produces server-owned `SharedBuffer` objects. For forwarding, export a
new server-owned CXL handle after the client data has been copied; do not pass
the client handle directly to the successor.

- [ ] **Step 5: Convert forwarding and resync**

`ReliableForwarding` accepts `RemoteBufferHandle`. The target successor address
selection explicitly chooses a CXL endpoint. Retry retains the same exported
buffer until a final response or lane retirement/reconciliation. Resync uses
the same generic handle and bulk path.

- [ ] **Step 6: Run storage suites**

Run:

```bash
cmake --build build/cxl-g0-host --target test_storage
build/cxl-g0-host/tests/test_storage --gtest_filter='*Cxl*:*StorageService*'
build/cxl-g0-host/tests/test_storage --gtest_filter='*Cxl*:*SyncForward*'
build/cxl-g0-host/tests/test_storage --gtest_filter='*StorageClientInterface*:*FaultInjection*'
```

Expected: selected tests PASS; data and checksum match on every replica and no timeout test frees a live allocation.

- [ ] **Step 7: Audit business source for forbidden RDMA types**

Run:

```bash
rg -n 'IBSocket|IBDevice|IBManager|RDMABuf|RDMARemoteBuf|IBV_|kIB|RDMA' \
  src/client src/fuse src/fbs src/storage src/meta src/mgmtd src/common/serde
```

Expected: no matches. Verbs compatibility and guarded lifecycle glue remain
only in `common/net/ib` and the generic `common/net` backend boundary.

- [ ] **Step 8: Record the uncommitted checkpoint**

Run `git diff --check && git status --short`.

## Task 9: Switch phase-1 configs and prove a CXL-only binary closure

**Files:**

- Modify: `src/storage/service/Components.h:28-66`
- Modify: `src/meta/service/MetaServer.h:45-67`
- Modify: `src/meta/components/Forward.h`
- Modify: `src/mgmtd/MgmtdServer.h:23-36`
- Modify: `src/client/meta/MetaClient.h`
- Modify: `configs/storage_main.toml`
- Modify: `configs/storage_main_launcher.toml`
- Modify: `configs/meta_main.toml`
- Modify: `configs/meta_main_launcher.toml`
- Modify: `configs/mgmtd_main.toml`
- Modify: `configs/mgmtd_main_launcher.toml`
- Modify: `configs/hf3fs_fuse_main.toml`
- Modify: `configs/hf3fs_fuse_main_launcher.toml`
- Modify: `configs/hf3fs_client_agent.toml`
- Modify: `configs/hf3fs_client_agent_launcher.toml`
- Modify: `configs/monitor_collector_main.toml`
- Modify: `configs/admin_cli.toml`
- Modify: `tests/fuse/config/admin_cli.toml`
- Modify: `tests/fuse/config/hf3fs_fuse_main.toml`
- Modify: `tests/fuse/config/hf3fs_fuse_main_launcher.toml`
- Modify: `tests/fuse/config/meta_bench.toml`
- Modify: `tests/fuse/config/meta_main.toml`
- Modify: `tests/fuse/config/meta_main_launcher.toml`
- Modify: `tests/fuse/config/storage_main.toml`
- Modify: `tests/fuse/config/storage_main_launcher.toml`
- Modify: `tests/FakeMgmtdClient.h`
- Modify: `tests/main/TestMain.cc`
- Modify: `tests/migration/TestMigrationService.cc`
- Modify: `tests/storage/client/TestFaultInjection.cc`
- Modify: `tests/storage/client/TestStorageBenchmark.cc`
- Modify: `tests/storage/client/TestStorageClientFastFailover.cc`
- Modify: `tests/storage/client/TestStorageClientHCStress.cc`
- Modify: `tests/storage/client/TestStorageClientInterface.cc`
- Modify: `tests/storage/client/TestStorageClientSideError.cc`
- Modify: `tests/storage/service/TestDumpMeta.cc`
- Modify: `tests/storage/service/TestIncorrectRoutingInfo.cc`
- Modify: `tests/storage/service/TestSingleProcessCluster.cc`
- Modify: `tests/storage/service/TestStorageService.cc`
- Modify: `tests/storage/service/TestStorageServiceFailStop.cc`
- Modify: `tests/storage/sync/TestSyncForward.cc`
- Modify: `tests/storage/sync/TestSyncStartAndDone.cc`
- Create: `configs/cxl/phase1-common.toml`
- Create: `tests/cxl_riscv/test_phase1_source_closure.py`

**Interfaces:**

- Produces: phase-1 configs with Data services on `Address::CXL`, Core on TCP, and explicit DAX/layout/bootstrap settings.
- Produces: source and ELF closure checker.
- Produces: `out/cxl-riscv/phase1/source-closure.json`, the required G0-B handoff.

- [ ] **Step 1: Write config/source closure tests**

```python
def test_all_former_rdma_configs_select_cxl(self):
    for path in PHASE1_CONFIGS:
        text = Path(path).read_text()
        self.assertNotIn("network_type = 'RDMA'", text)
        self.assertIn("network_type = 'CXL'", text)

def test_core_remains_tcp_in_phase1(self):
    for path in SERVER_CONFIGS:
        config = tomllib.loads(Path(path).read_text())
        core = find_group(config, "Core")
        self.assertEqual(core["network_type"], "TCP")
        self.assertEqual(core["service_plane"], "Control")

def test_data_clients_cannot_force_tcp(self):
    for path in DATA_CLIENT_CONFIGS:
        config = tomllib.loads(Path(path).read_text())
        self.assertFalse(find_data_client(config)["force_use_tcp"])
```

For every former RDMA service group, also assert
`service_plane = 'Data'`. Validate that each CXL address in the generated
phase-1 route declaration resolves to exactly one plane and that no TCP
listener registers a Data service.

The same test builds a checked source inventory. Outside
`src/common/net/ib`, the legacy status compatibility declaration and
explicitly RDMA-only tests, reject `IBSocket`, `IBDevice`, `IBManager`,
`RDMABuf`, `RDMARemoteBuf`, `IBV_`, `kIB*` and other legacy `RDMA` symbols.
The compatibility enum/mapping and neutral status-alias declarations have
exact file-and-line-pattern allowlist entries. The test rejects every new
unclassified match so that allowlist cannot silently grow.

- [ ] **Step 2: Run the closure test and observe RDMA config matches**

Run `python3 -m unittest -v tests/cxl_riscv/test_phase1_source_closure.py`.

Expected: FAIL and list the current RDMA config paths.

- [ ] **Step 3: Change defaults and checked-in experimental configs**

Change only groups that currently use RDMA to CXL in every checked-in main,
launcher, client-agent, monitor, admin and FUSE-test configuration. Rename the
bulk-control and shared-buffer fields introduced in Tasks 6-8. Add CXL config
fields for device path, mapped offset/length, endpoint ID, session generation,
queue depth, cell bytes, poll policy and bootstrap timeout. Keep Core group
ports and FDB cluster-file behavior unchanged. Set every former RDMA group to
`service_plane = 'Data'`, every phase-1 Core group to
`service_plane = 'Control'`, and emit a unique address-to-plane route entry for
each CXL listener.

Convert storage, sync, migration and main test fixtures that directly create
an `IBDevice::Config` to a shared file-backed `CxlFabric` fixture. Keep genuine
verbs tests under `tests/common/net/ib` and compile them only when RDMA is
enabled. `FakeMgmtdClient` advertises CXL Data-plane addresses in CXL tests;
address-parser compatibility cases may still cover the legacy RDMA enum but
must not initialize an IB device in a CXL-only build.

In `Client::serdeCtx`, reject `force_use_tcp` when the selected address is a
Data-plane CXL endpoint; do not turn it into an implicit fallback switch.

- [ ] **Step 4: Build with RDMA disabled**

Run:

```bash
cmake -S . -B build/cxl-phase1-host -G Ninja \
  -DSHUFFLE_METHOD=stdshuffle \
  -DHF3FS_ENABLE_CXL=ON \
  -DHF3FS_ENABLE_RDMA=OFF \
  -DHF3FS_ENABLE_CXL_NETD=OFF
cmake --build build/cxl-phase1-host --parallel 8
```

Expected: all phase-1 targets compile without verbs headers or libraries.

- [ ] **Step 5: Verify source and ELF closure**

Run:

```bash
python3 -m unittest -v tests/cxl_riscv/test_phase1_source_closure.py
python3 tests/cxl_riscv/test_phase1_source_closure.py \
  --build-dir build/cxl-phase1-host \
  --write-evidence out/cxl-riscv/phase1/source-closure.json
find build/cxl-phase1-host/bin -type f -perm -111 -print0 | \
  xargs -0 -n1 sh -c '! readelf -d "$0" 2>/dev/null | rg -q "libibverbs"'
```

Expected: tests PASS, every executable inspection returns success and the
handoff records `schema=hf3fs.cxl-source-closure.v1`, `gate=phase1`,
`status=passed`, CMake cache options, base `HEAD`, tracked binary-diff hash, a
sorted path/mode/type/content hash for every non-ignored untracked file,
tracked tombstones, config hashes and inspected ELF hashes. The combined source
identity contains a root record plus one record per initialized nested
repository with its gitlink, HEAD, tracked diff, tombstones and untracked
content hashes. It must match the full-profile remote sync receipt; a top-level
`git status` pathname without nested or untracked content hashes is
insufficient. Write it atomically only after every closure check passes.

- [ ] **Step 6: Record the uncommitted checkpoint**

Run `git diff --check && git status --short`.

## Task 10: Add the CXL formal model and full host regression gate

**Files:**

- Create: `specs/CXLTransport/CXLTransport.pproj`
- Create: `specs/CXLTransport/PSrc/CXLTransport.p`
- Create: `specs/CXLTransport/PSpec/SystemSpec.p`
- Create: `specs/CXLTransport/PTst/TestDriver.p`
- Create: `specs/CXLTransport/PTst/TestScript.p`
- Create: `specs/CXLTransport/README.md`

**Interfaces:**

- Produces: P events for byte acceptance, cursor publication, peer delivery,
  response, timeout, retire, generation change, retry, allocation-record
  export/retirement and slot reuse.
- Proves: no overwrite, no duplicate consume, no stale-generation acceptance,
  `delivered <= published <= accepted`, no executed request classified as
  rejected, no unknown request automatically replayed, no lease release while
  an old handle remains accessible, and eventual drain for live peers.

- [ ] **Step 1: Write monitors that fail against an intentionally unsafe model**

```p
spec NoOverwrite observes ePublish, eConsume {
  var live: set[(int, int)];
  start state Watching {
    on ePublish do (entry: (lane: int, slot: int, generation: int)) {
      assert !((entry.lane, entry.slot) in live), "published over a live slot";
      live += ((entry.lane, entry.slot));
    }
    on eConsume do (entry: (lane: int, slot: int, generation: int)) {
      assert (entry.lane, entry.slot) in live, "consumed an unpublished slot";
      live -= ((entry.lane, entry.slot));
    }
  }
}
```

Add monitors `NoDuplicateConsume`, `NoStaleGeneration`,
`OrderedStreamWatermarks`, `NoFalseRejectedBeforeExecute`,
`NoUnknownReplay`, `NoPrematureRelease` and hot/cold `EventuallyDrained`.

- [ ] **Step 2: Run the unsafe model and verify a monitor failure**

Run from `specs/CXLTransport` using the same P toolchain documented by `specs/RDMASocket`:

```bash
p compile
p check -tc tcUnsafeOverwrite
```

Expected: the unsafe case fails with `published over a live slot`.

- [ ] **Step 3: Implement the bounded safe model and scenario tests**

Model depth four, absolute sequences, owner-only cursors, accepted/published/
delivered byte watermarks, two request ranges sharing a cell, generation
changes, timeouts that retain ownership and one allocation slot exported as a
subrange before reuse. Add ping-pong, one-way,
bidirectional, queue-full, accepted-before-flush, partially delivered shared
cell, timeout-before-delivery, crash-after-delivery, corrupt delivered record,
stale subrange access after slot reuse and restart-with-new-generation cases.
Only a trustworthy retirement may replay a range whose end was not delivered;
no old handle may validate against the reused slot generation.

- [ ] **Step 4: Run all model checks**

Run:

```bash
p compile
p check -tc tcPingPong
p check -tc tcQueueFull
p check -tc tcTimeoutOwnership
p check -tc tcCoalescedRequestDisposition
p check -tc tcRestartGeneration
```

Expected: every safe case passes every safety and liveness monitor.

- [ ] **Step 5: Run the complete host test suite**

Run:

```bash
cmake --build build/cxl-phase1-host --parallel 8
ctest --test-dir build/cxl-phase1-host --output-on-failure
```

Expected: all enabled tests PASS. Existing IB hardware tests may be excluded by `HF3FS_ENABLE_RDMA=OFF`; their generalized CXL equivalents must run.

- [ ] **Step 6: Record the uncommitted checkpoint**

Run `git diff --check && git status --short`.

## Task 11: Prove phase 1 in RISC-V multi-guest execution

**Files:**

- Create: `deploy/cxl-riscv/run_phase1.py`
- Create: `deploy/cxl-riscv/phase1_topology.json`
- Create: `tests/cxl_riscv/test_phase1_evidence.py`
- Modify: `deploy/cxl-riscv/guest_image.py`

**Interfaces:**

- Produces: two-guest Echo, client/storage, multi-storage chain and externally
  configured IO500 scenarios.
- Produces: deterministic RF1/RF2/RF3 and 1/2/4-client topology derivation with
  stable, disjoint base/storage/client guest and endpoint ID ranges for the
  later comparison runner.
- Accepts for the IO500 scenario: an absolute config, client-rank count,
  immutable resource-envelope JSON, seed, measurement marker, cache-state,
  warm-up count, `--result-class qualification|measured` and runner-owned output
  directory, plus an immutable platform contract and declared endpoint backing policy; each argv/config/hash and the
  realized measurement boundaries are recorded in `result.json`.
- Produces paths such as `out/cxl-riscv/phase1/20260904T120000Z-1234/result.json` plus serial, QEMU and CXLMemSim logs.
- Produces: `validate_phase1_result(result: dict) -> list[str]`.
- Consumes: a passing G0-B result whose source/artifact hashes match the phase-1 build.
- Consumes: the G0-pinned RISC-V `fdbserver` and `libfdb_c`; storage and chain
  scenarios use a real FDB guest over the retained TCP network.

- [ ] **Step 1: Write phase-1 evidence gate tests**

```python
def test_phase1_rejects_rdma_or_serving_on_bootstrap(self):
    result = passing_phase1_result()
    result["counters"]["rdma_open_attempts"] = 1
    result["counters"]["bootstrap_serving_bytes"] = 64
    errors = validate_phase1_result(result)
    self.assertIn("rdma_open_attempts must be zero", errors)
    self.assertIn("bootstrap carried serving bytes", errors)

def test_phase1_requires_replica_checksum_match(self):
    result = passing_phase1_result()
    result["chain"]["replicas"][1]["checksum"] ^= 1
    self.assertIn("replica checksum mismatch", validate_phase1_result(result))

def test_phase1_rejects_missing_or_changed_g0_b(self):
    result = passing_phase1_result()
    result["g0_b"]["artifact_manifest_sha256"] = "0" * 64
    self.assertIn("G0-B artifact identity mismatch", validate_phase1_result(result))

def test_phase1_requires_real_pinned_fdb_guest(self):
    result = passing_phase1_result()
    result["fdb"]["server_machine"] = "Advanced Micro Devices X86-64"
    result["fdb"]["transaction_committed"] = False
    errors = validate_phase1_result(result)
    self.assertIn("fdbserver is not the pinned RISC-V artifact", errors)
    self.assertIn("FoundationDB transaction did not commit", errors)

def test_phase1_io500_command_is_resource_bounded(self):
    command = phase1_command(
        scenario="io500", clients=4, resource_envelope=envelope(),
        cache_state="preconditioned", warmup_iterations=1,
    )
    self.assertIn("--io500-config", command.argv)
    self.assertIn("--client-ranks", command.argv)
    self.assertIn("--resource-envelope", command.argv)
    self.assertIn("--cache-state", command.argv)
    self.assertIn("--warmup-iterations", command.argv)
    self.assertIn("--result-class", command.argv)
    self.assertEqual(command.aggregate_resources, envelope().threefs_totals)
```

- [ ] **Step 2: Run the test before creating the runner**

Run `python3 -m unittest -v tests/cxl_riscv/test_phase1_evidence.py`.

Expected: FAIL with an import error.

- [ ] **Step 3: Implement owned-process orchestration**

The runner creates a unique run directory, sparse Type-3 backing files
according to the validated `all-distinct`/`all-shared` profile and separate
storage disks, starts one CXLMemSim process and the declared QEMU guests, and
records every PID plus a random owner token. It refuses pre-existing shared
memory objects or ports. Cleanup signals only processes whose PID start time
and owner token match its manifest.

Use the same run-owned QEMU socket/multicast L2 mechanism already proven by the
parent RISC-V runners, with a fresh multicast port, fixed guest IP/MAC
assignments and no host-service fallback. This network carries only phase-1
Core, FDB and CXL-bootstrap TCP. Record per-class byte counters and route/socket
snapshots so a Data-plane RPC on TCP fails the gate.

The topology declares distinct guest IDs, process-unique DAX endpoint IDs and
non-overlapping lane/arena partitions for:

```json
{
  "scenarios": {
    "echo": ["server", "client"],
    "storage": ["mgmtd", "meta", "storage", "fuse-client", "fdb"],
    "chain": ["mgmtd", "meta", "storage-head", "storage-successor", "fuse-client", "fdb"]
  }
}
```

Each scenario also names exactly one `fabric_authority` endpoint owned by
`cxl-fabricd`. The daemon is colocated with the Echo server guest for `echo`
and the mgmtd guest for `storage`, `chain` and IO500, but has a distinct
process/endpoint identity and no serving lane. Start it first in
initialize-authority mode, wait for the exact session, manifest SHA-256 and
superblock CRC32C,
then start attach-only application/netd peers. A missing/duplicate authority,
peer formatting attempt or authority exit before `RETIRED` fails the run. The
runner never writes the shared superblock through host backing files.

`fdbserver` has a dedicated guest and TCP address but no DAX endpoint. Every
3FS process that maps DAX has its own endpoint identity even if a later
resource profile co-locates processes in one guest. Start the FDB guest first,
verify the cluster-file and server/client version hashes from G0, and only then
start mgmtd/meta.

`run_phase1.py --replication-factor 1|2|3` derives the storage chain from this
checked topology. Adding replicas appends stable storage guest/endpoint IDs and
the required forwarding/resync lanes without renumbering RF1 participants;
range overlap or region-capacity exhaustion is a configuration failure.

For `--scenario io500`, also accept `--client-ranks 1|2|4`,
`--io500-config`, `--resource-envelope`, `--seed`, `--measurement-marker` and
`--cache-state cold|preconditioned`, `--warmup-iterations` and `--output`, plus
`--result-class qualification|measured`, `--platform-contract` and
`--endpoint-backing-policy all-distinct|all-shared`. Rank 0 keeps the base
client guest/endpoint IDs. Additional ranks
use a reserved client-only guest/endpoint ID range that cannot collide with
storage replicas, and each rank runs in an independent client QEMU guest with
its own FUSE process. The immutable envelope supplies every guest's vCPU/RAM,
storage size, shared logical CXL capacity and CPU affinity; reject an aggregate
or per-role mismatch before QEMU starts. Count the logical shared fabric
capacity once, not once per guest aperture.

The normal correctness profile requires the G0-B embedded `all-distinct`
contract. A comparison profile may select `all-shared` only when that exactly
matches the realized read-only LegoFS QEMU policy; every other normalized
platform field must still equal G0-B. Record all backing `[st_dev, st_ino]`
values and re-run the bidirectional DAX checksum/trace smoke before starting
IO500. `mixed` identity is always invalid.

Phase-1 MPI/Hydra coordination uses the run-owned QEMU socket/multicast network
and is classified as `benchmark_control_tcp_bytes`, distinct from Core, FDB,
CXL bootstrap and serving data. Record process/socket/port attribution. It is
allowed only for the IO500 harness and never makes a Data-plane request valid
on TCP.

After every declared service reaches readiness, a `preconditioned` IO500 or
transport run executes exactly `--warmup-iterations` unmeasured workloads in a
run-owned namespace without stopping any QEMU guest, CXLMemSim process or
storage backing. Each warm-up must validate, drain all pending requests and
remove only its run-owned namespace before the next iteration. The runner then
captures transport, network, simulator and process-counter snapshots, publishes
the manifest-bound measurement marker to every participant, and starts the
measured workload. It captures matching end snapshots and guest boot/session
identities so the validator can prove that warm-up and measurement used the
same cohort. A failed warm-up invalidates the slot and no measured workload is
started.

A `cold` run requires `--warmup-iterations 0`, never-before-used backing files
created before guest launch and a new session generation. Reject every other
cache-state/count combination. Cold and preconditioned evidence have distinct
profile hashes and are never aggregated.

- [ ] **Step 4: Implement scenario gates**

Echo transfers 64 B through 512 MiB framed messages. Storage writes and reads
4 KiB, 64 KiB, 1 MiB and 4 MiB deterministic payloads. Chain mode verifies
head/successor checksums, kills and restarts the successor, confirms lane
generation change, and exercises resync. Every guest must report `/dev/dax0.0`
geometry and CXL READY, except the FDB-only guest, which reports the pinned
server identity and a committed transaction. Storage/chain require positive
Core TCP, FDB TCP, bootstrap-control, CXL RPC and CXL bulk evidence while
`bootstrap_serving_bytes` and TCP Data-plane bytes remain zero.

In a separate unmeasured Echo fault subcase, pause the consumer and retire the
lane at three deterministic byte offsets: accepted but not cursor-published,
published but short of the request's delivered end, and delivered through the
request end before response. Require, respectively,
`RejectedBeforeExecute`, `RejectedBeforeExecute` after trustworthy retirement,
and `OutcomeUnknown`; then verify only the first two may be automatically
retried with the original UUID. Corrupt one delivered record and require
`LaneRetired`, no automatic replay and no allocation reuse until the stronger
generation/membership fence.

- [ ] **Step 5: Build and stage the RISC-V phase-1 image**

Run on `vlm-server`:

```bash
cd /home/guokc/mypro/gnn-mount-sda1/CXLMemSim-riscv/components/3FS
python3 deploy/cxl-riscv/build_riscv.py \
  --preflight out/cxl-riscv/preflight.json \
  --profile full-3fs \
  --source-closure out/cxl-riscv/phase1/source-closure.json \
  --dependency-contract deploy/cxl-riscv/full-deps.json \
  --build-dir build/cxl-riscv-full --jobs 8
python3 deploy/cxl-riscv/guest_image.py \
  --profile full-3fs \
  --rootfs out/cxl-riscv/rootfs \
  --build-manifest build/cxl-riscv-full/build-manifest.json \
  --source-closure out/cxl-riscv/phase1/source-closure.json \
  --output out/cxl-riscv/images/phase1.ext4 \
  --size-gib 16
```

Expected: RISC-V binaries and image manifests pass G0 closure checks.

- [ ] **Step 6: Run the three remote scenarios**

Run:

```bash
python3 deploy/cxl-riscv/run_phase1.py --scenario echo --timeout 900 \
  --g0-b out/cxl-riscv/g0-b/g0-b-handoff.json
python3 deploy/cxl-riscv/run_phase1.py --scenario storage --timeout 1200 \
  --g0-b out/cxl-riscv/g0-b/g0-b-handoff.json
python3 deploy/cxl-riscv/run_phase1.py --scenario chain --timeout 1800 \
  --g0-b out/cxl-riscv/g0-b/g0-b-handoff.json
```

Expected: each prints an absolute result path with `status: "passed"`.

- [ ] **Step 7: Validate phase-1 closure**

For every result, require:

```text
rdma_open_attempts == 0
bootstrap_serving_bytes == 0
tcp_data_plane_bytes == 0
core_tcp_bytes > 0 in storage/chain
fdb_tcp_bytes > 0 in storage/chain
bootstrap_control_bytes > 0 in every CXL connection scenario
forbidden_fallbacks == 0
cxl_rpc_requests > 0
cxl_bulk_read_bytes > 0 in storage/chain
cxl_bulk_write_bytes > 0 in storage/chain
io500_clean_valid == true for a measured IO500 scenario
qualification_semantics_passed == true and performance omitted for a
  qualification-only IO500 scenario
mpi_ranks == client_guests == requested client-ranks in the IO500 scenario
actual aggregate vCPU/RAM/storage/fabric capacity == resource envelope
generation_mismatch == 0 outside the deliberate restart case
publication_snapshot_errors == 0 outside the deliberate corruption case
false_rejected_before_execute == 0
unknown_request_replays == 0
pending_publication_receipts == 0 after reconciliation/drain
all replica checksums equal
all owned processes cleaned up
```

Run `python3 -m unittest -v tests/cxl_riscv/test_phase1_evidence.py` and pass each result to its CLI validator.

- [ ] **Step 8: Record the uncommitted phase-1 checkpoint**

Run:

```bash
git diff --check
git status --short
```

Expected: only phase-1 and G0 files under 3FS plus approved documentation are modified; no commit is created.
