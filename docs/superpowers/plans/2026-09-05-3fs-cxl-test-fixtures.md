# CXL storage and bulk fixture migration

Execution used one agent, kept all changes under 3FS and created no commits.
The fixture migration and functional phase-1/10c1s qualification have passed;
see the [accepted result](#accepted-phase-1-and-10c1s-result) below. Historical
checkpoints retain failed attempts and the evidence that led to each fix.

The disabled suites contain 58 storage/bulk test declarations before parameter
expansion. The basename exclusion also removes four unrelated common
FaultInjection tests. Preserve the existing test names, parameters, error
expectations, replication checks and failure injection. Restoring compilation
alone is not behavior coverage.

Each CXL service requires its own process and manifest endpoint. Use an exec
helper for the authority, management server and storage servers. The test
process owns the storage client endpoint. A private Unix socket carries only
test setup, configuration and introspection; application requests, replies and
bulk data use the real CXL transport. Do not enable self-endpoint connections
or replace storage RPC with in-process calls to make the suites pass.

Keep the native RDMA fixture available behind its existing build option. For
CXL, expose explicit test proxy methods for address discovery, stop/join,
routing snapshots/updates, local target state and hot configuration. Replace
the few direct component accesses with those methods. Fake routing remains
fake only where the original test requested it; real-management parameters
must still exercise the management service and heartbeat transitions.

Use a fresh run directory and immutable manifest per fixture. Keep production
queue depth 8 and cell size 65536. Size lanes and bulk arenas from the maximum
participants, concurrent connections and configured buffer pools. Process
restart retains the data directories, advances endpoint generation only after
the required retirement fence, and preserves original failure/recovery checks.
Every helper has a bounded command wait, captured stderr, explicit PID
ownership and checked exit status. Teardown stops clients, storage, management
and authority in order; forced cleanup fails the test and retains diagnostics.

- [x] Scope the CMake exclusions by source path and restore the four common
  FaultInjection cases. Record discovered test counts before and after.
- [x] Move `TestBulkControl.Normal` to a real authority/server/client process
  fixture with the existing callback assertion. Verify both callback directions
  use CXL and check clean endpoint retirement.
- [x] Add the exec helper, bounded Unix control protocol, manifest generation
  and process proxies. Test failed startup, unexpected helper exit and clean
  shutdown before applying it to the storage suites.
- [x] Adapt UnitTestFabric setup, routing updates and teardown. Run one RF1
  write/read case first, then the full Interface and SideError parameter sets.
- [x] Adapt service, incorrect-routing and dump fixtures; propagate hot config
  to the owning helper and preserve target-state assertions.
- [x] Adapt real-management and sync fixtures, including RF2/RF3, restart,
  generation changes, failure detection and resynchronization.
- [x] Adapt benchmark/fault/stress and migration fixtures. Restore storage_bench
  in CXL-only builds with explicit runtime configuration for external servers.
- [x] Remove each exclusion only when its original cases build and execute in
  RDMA=OFF. Run the complete restored suites and relevant transport regressions,
  record parameter-expanded counts and retain result/log evidence.

This migration is a phase-1 requirement. A passing host fixture does not replace
the RISC-V DAX scenarios, formal checks or phase-1 evidence gate. The current
task ends after phase-1 migration and the 10c1s standard IO500 measurement
with timing and root-cause closure; see
[the standard plan](2026-09-06-3fs-cxl-io500-standard.md). Machine-local
FoundationDB stays on TCP; phase 2 is outside this task.

The first restored five cases pass in the native RDMA=OFF build: the four
common FaultInjection cases and BulkControl.Normal. The latter first reproduced
`RPC::InvalidServiceID` on the reverse request because Client registered
BulkControlImpl only in Control. It now registers in Data, matching the
incoming CXL transport and the original RDMA service behavior. The helper uses
queue depth 8/cell size 65536, real exec participants, bounded process waits,
and validates all endpoint retirement records and generations after exit.
The eight-incarnation transport E2E and 27 service-plane tests also pass.
Evidence: `out/cxl-riscv/full-deps/common-fault-fixture-*.log` and
`out/cxl-riscv/full-deps/bulk-fixture-*.log`. The passing G0-B reference remains
bound to source closure 018; it predates this newly discovered callback fix.

The bounded process channel passes seven native cases, including large binary
frames, fragmented headers, failed startup, unexpected exit, oversized frames,
timeout poisoning/owned cleanup, and a snapshot transformation held inside one
transaction. Evidence: `out/cxl-riscv/full-deps/fixture-process-callback-test.log`.
The storage helper/proxies and original Interface cases now build and link.
The control channel uses existing binary serde for strongly typed routing-map
keys; configuration and manifest evidence remain TOML/JSON. Real mgmtd routing
edits hold the original writer lock across the test callback exchange.

The first RF1 write/read/restart run found an explicit-geometry omission in the
fixture's internal clients: their default queue depth 64 did not fit the
manifest's depth 8. All fixture socket configurations now use 8/65536. The
corrected RF1 test passes: 128 KiB write/read, restart with preserved bytes,
storage generation 2, all four endpoints CRC/identity/RETIRED checked, and four
helper incarnations exit 0 without forced cleanup. Passing evidence is in
`out/cxl-riscv/storage-fixtures/fixture-C7axpF/` and
`out/cxl-riscv/full-deps/storage-fixture-rf1-02.log`. Failed evidence remains in
`out/cxl-riscv/storage-fixtures/fixture-tajQPu/` and
`out/cxl-riscv/full-deps/storage-fixture-rf1-01.log`. Service and sync proxy call
sites have been adapted, but their source exclusions remain until validation.

The first full Interface/SideError execution ran 47 cases: SideError 8/8 and
the first nine InMem Interface cases passed. The remaining 30 failed in setup
with ENOSPC because the new fixture retained successful temporary storage
files. They have not yet validated RPC/SmallChunk behavior. After verifying
all successful helper exits and retirement records, remove only those owned
temporary data directories and preserve the result bundles/logs. This restored
138 GiB on the root filesystem. The fixture now cleans successful temporary
storage data automatically; failed data and backing remain available. Evidence:
`out/cxl-riscv/full-deps/storage-fixture-interface-side-01.{log,xml}` and each
passing fixture's `cleanup-verification.json`.

RF1 restart also exposed CXL Transport closing eventfd before epoll removal.
A regression with a duplicated observer descriptor proves the kernel interest
remains after retirement (`transport-retirement-before-test-04.log`). The fix
removes epoll interest before closing the socket. RF1 restart and the original
RF2 Interface Read case pass (2/2), with no invalid-fd epoll errors:
`storage-fixture-rf1-rf2-01.{log,xml}`, fixtures `o1GWK7` and `8CLTWy`.
Earlier regression attempts 01/02 failed on peer setup and the full /tmp;
attempt 03 lacked peer delivery initialization and did not exercise retirement.
The valid regression initializes both lane owners and asserts a trustworthy
publication snapshot before retiring the transport.

The corrected epoll regression and focused transport suite pass 28/28
(`transport-retirement-after-test-04.log`). Interface/SideError run 02 passes
44/47. One InMem setup hit the UID's pinned-memory budget while another RF3
fixture ran concurrently; serialize the large storage suites. Two RPC cases
exposed a product short-read error: BatchReadJob passed the full destination
capacity to an exact-length CXL push. It now validates and narrows the handle
to the completed byte count and skips zero-byte EOF transfers. The independent
short-read test checks the untouched destination suffix. All three failed cases
plus RF1 restart and short-read/EOF pass, 5/5, in
`storage-fixture-short-read-01.{log,xml}`. The full suite has not yet been rerun
on this source revision.

Real mgmtd first failed with ClusterIdMismatch because AppInfo inherits only
FbsAppInfo's serialized fields; its clusterId is process-local. The fixture
protocol now carries clusterId explicitly. RF3 real-management failure
detection passes in `storage-fixture-real-rf3-02.{log,xml}`, with six retired
endpoints (`fixture-xSJPgb`). Migration now prepares a separate exec service
and manifest endpoint before freeze; the original case starts/stops it after
the original write/read assertions. Service/sync/migration and the remaining
benchmark/fault/stress cases all build in RDMA=OFF. All source exclusions have
been removed, with execution still in progress. `storage_bench` also builds;
external client/cluster mode accepts an explicit Attach runtime `--cxlConfig`.

Canceled bootstrap activation reproduced duplicate active tuple failures in
two new regressions (`pending-cancel-before-test.log`). CxlConnectService now
retires pending acceptor sockets only after a CRC-valid terminal requester
record matches the exact session, endpoint/lane generations and nonce.
An old terminal record cannot cancel a new pending generation. The complete
CXL layout suite passes 83/83 (`pending-cancel-after-test.log`).

Failed storage data from fixtures tajQPu, GFpsdX, ELRbtc, 95GP3p and hmUvBc is
preserved in each result directory as `storage-data.tar.zst`. Every archive
was compared against the original files before removing the owned /tmp data;
`data-archive.json` records SHA-256, exact original paths and helper exit/PID
checks. Logs, configurations, raw fabric and retirement evidence remain.

The restored client suite passes 75/75 (2039.191 seconds), service 46/46,
sync 2/2 and migration 1/1, with
original parameter sets and assertions. Evidence is
`storage-fixture-{service,sync,migration}-01.{log,xml}`. Original disabled sync
and write-failure cases remain disabled and are not claimed as coverage. The
75-case client result is `storage-fixture-client-all-01.{log,xml}`. Bounded P checking passes
ten safe cases at 1000 schedules each and detects all five unsafe variants in
`out/cxl-riscv/formal/20260905T084857.297288Z-1498139/result.json`; this is bounded
schedule evidence, not an exhaustive or implementation-level proof.

Before the final guest build, export process-lifetime transport counters on
CXL runtime shutdown, bound to PID/session/endpoint/generation and shutdown
status. Count validated received RPC frames once (wire bytes before
decompression), successful bulk copies, RDMA creation attempts and actual TCP
receive bytes by service plane. Classify received TCP RPCs into Core (service
10001), CXL bootstrap (12), or other services; the latter is forbidden in the
phase-1 application cohort. Do not represent CxlConnectMetrics' constant zero
or sizeof(struct) metadata totals as measured wire traffic. Retain raw receive
totals so unclassified/incomplete traffic cannot disappear from evidence. FDB
is outside this RPC layer and remains independently verified local TCP.

The final 10c1s runner will place server roles and FDB in guest 0 and ten FUSE
clients in independent guests. The existing QEMU socket multicast transport,
bound to loopback with a run-owned group/port, carries Core/bootstrap TCP
without host network changes or a new relay implementation. Use the existing platform
binaries, distinct backing files, queue depth 8/cell size 65536, and a larger
declared logical CXL capacity/lane count sized for the complete cohort. Validate
new DAX geometry before application startup. Require finite per-client
write/fsync/direct-read/hash checks, evidence of overlapping client operations,
all endpoint retirement and owned process absence. This qualification is
functional simulation evidence and carries no physical-CXL performance claim.

The qualification geometry is 2 GiB logical shared CXL capacity (2 MiB probe
plus 2046 MiB application region), 800 lanes, 25 endpoint IDs and 15 active
processes. This gives each acceptor 32 lanes, covering ten normal/update
client pairs, and over 64 MiB of bulk arena per participant. The multicast
loopback socket behavior was checked on the selected host without changing
host networking. Six new Python evidence/config tests reject missing or
duplicate process counters, stale identity, unexpected TCP/RDMA traffic,
unclassified receive bytes, checksum mismatch, absent overlap and forced
cleanup. The 10c1s runner has not yet run on real guests.

Transport counter regressions pass 30/30 and layout/bulk regressions 83/83 in
`transport-evidence-{planes,layout}-01.log`. The standalone storage_bench CLI
passes finite read/write, data verification, truncate/remove and clean CXL
retirement in `storage-bench-cli-02.log` (`fixture-ZtLVGt`). It uses an explicit
private data directory and process-only `ulimit -n 65536`; run 01 failed on
the inherited low descriptor limit before starting services. External
`--cxlConfig` mode also passes against the real host FDB/mgmtd/meta/storage/FUSE
stack in `out/cxl-riscv/fuse-smoke-logs/transport-bench-02/result.json`.
The separate benchmark reuses the retired admin endpoint's next generation,
queries real mgmtd routing and performs finite write/read verification and
truncate/remove. An explicit Core get-config checks the retained TCP service.
The host FUSE payload SHA-256 is
`5cc1c8e61ba94bcc41a78a500b72ec4e0dc0486f5cd31eb33927161ebbb89e93`.
Measured totals: RDMA attempts, TCP Data bytes and bootstrap serving bytes are
all zero; Core TCP 11095 bytes, bootstrap control 13050 bytes, CXL requests and
responses 502 each, bulk pulls 18874368 bytes and pushes 44302336 bytes.
Every received TCP byte reconciles, all six endpoint records pass final CRC/
identity/generation/RETIRED checks, all owned PIDs are absent and FUSE is
unmounted. Run 01 passed FUSE and external benchmark but did not invoke Core;
its Core count correctly stayed zero. It is superseded by run 02 for that
stronger evidence scope.

The complete native CXL-only build passes after all fixture and counter
changes (`phase1-final-host-build-01.log`). The Python CXL/RISC-V suite passes
110/110 (`phase1-final-python-local-01.log`). The full CTest run is in progress;
its common suite has reproduced only the same three unrelated RenderConfig
failures. No first-phase acceptance claim is made before the latest guest
build and 10c1s result.

G0-B with build 017/source closure 019 passes in
`out/cxl-riscv/g0-b/full-010/20260905T100417.777523Z-1605864/result.json`.
FUSE payload SHA-256 is
`a4a89ed951b89e8fd5354cbb925c2e94c846faf9d176d99630c9328d5a33b56d`;
both QEMU guests and CXLMemSim exit 0, all residents retire cleanly, Core and
bootstrap TCP are measured, and bulk pull/push each complete 2097152 bytes.
The CLI returns 2 only because it correctly refuses to replace the existing
immutable canonical handoff; preserve that reference and publish new results
under their own result directory.

The actual guest console uses CR-CR-LF. The 10c1s start/end observation parser
now accepts that framing and rejects echoed shell commands/wrong guest IDs.
Readiness query loops append every admin receipt to a retained log, and the
10c1s validator rejects gaps in the admin generation sequence. Refresh the
source-bound build/G0 reference for these runner-only changes before 10c1s;
the application C++ code is unchanged.

Build 018/source closure 020 passes G0-B in
`out/cxl-riscv/g0-b/full-011/20260905T102434.582249Z-1648484/result.json`.
The 2 MiB FUSE hash is
`410811d292b8e04a24ebb5ec6c9dbd9bc2dd519349b1382b4adcebf486066047`.
All 15 transport receipts are present, admin generations are 1 through 10,
every TCP receive byte reconciles, forbidden counters are zero, and both
QEMUs plus CXLMemSim exit 0. The complete native aggregate `test_storage`
passes 137/137 with one original disabled case; its raw log and binary hash
are archived in `full-deps/phase1-final-storage-aggregate-01.{log,json}`.

The first real 10c1s run is
`out/cxl-riscv/phase1/10c1s/20260905T104308.844830Z-1648484/`.
All eleven guests report distinct 2 GiB DAX mappings, all twenty bidirectional
generation/checksum probes pass, and local TCP FDB set/get passes. Application
startup fails before finite I/O: the first FUSE mount does not become ready.
Read-only guest diagnostics confirm established TCP connections to all clients,
and mgmtd lease extension No.1374 takes from guest 00:19:21 to 00:26:08 to
reacquire a lease, exceeding the configured 180 seconds. FDB reports transaction
too old retries; reloading completes at 00:28:12. This identifies a service
availability failure during concurrent bootstrap, not its definitive cause.
Live debugfs snapshots can be stale/incomplete; `diagnostic-console-node0-*.log`
contains direct guest observations captured after the run had already failed.
Original images, traces, logs and the failed result remain intact.

Next calibration keeps RPC 120 seconds, lease 180 seconds, heartbeat checks,
wire geometry and all evidence gates. Bootstrap each FUSE mount before starting
the next, then enter the concurrent I/O barrier with all ten mounted clients.
Scale the experimental idle poll interval from 1 ms to 10 ms for ten clients
to keep aggregate polling comparable; the single-client G0 profile stays 1 ms.
An unmounted owned FUSE process now receives SIGTERM during failed-start cleanup
instead of waiting solely for an unmount that cannot succeed. Mounted FUSE still
requires exit 0, and the original startup failure cannot become a passing run.
The ordering regression fails before this change and the Python suite passes
112/112 afterward (`phase1-bootstrap-{before,after}-01.log`). Refresh the
source-bound build and G0-B reference, then rerun 10c1s to validate this candidate.

The complete native CTest run finishes in 8082.66 seconds: 18/19 targets pass.
Only test_common fails, with the same three RenderConfig cases; its other
375 cases pass and 20 skip. Restored test_storage passes 137/137, standalone
storage_client 75/75, storage_service 46/46, storage_store 14/14, storage_sync
2/2, and migration 1/1. Original disabled cases remain disabled. Counts from
aggregate and standalone targets overlap and must not be summed as unique
coverage. The summary, raw output and compressed complete CTest log are
`full-deps/phase1-final-ctest-01.{json,log}` and
`full-deps/phase1-final-ctest-details-01.log.gz`.

Build 019/source closure 021 passes G0-B in
`out/cxl-riscv/g0-b/full-012/20260905T112433.292234Z-1760343/result.json`.
FUSE payload SHA-256 is
`2a21aedfd454e8c34765f1dd4b9bcdc03b353c99efe93103b9dbd427af3e9d46`.
The rootfs references the immutable archived build manifest. Both guests and
CXLMemSim exit 0, application retirement and evidence validation pass.

The second 10c1s run,
`out/cxl-riscv/phase1/10c1s/20260905T114016.638367Z-1760343/`, fails before
concurrent I/O. All twenty DAX directions and local FDB pass. Sequential
bootstrap mounts clients 1 through 5, but client 6 exceeds the readiness
loop's 120 iterations. Offline extraction after all QEMUs stop shows that
client 6 is still initializing when cleanup sends SIGTERM; it finishes FUSE
client initialization at guest 00:24:24, then unblocks the pending signal.
Mgmtd also loses its lease: extension No.2706 spans 00:19:20 to 00:24:19,
with FDB transaction-too-old and commit-unknown retries. The longer readiness
budget below does not establish the cause of that service delay. Complete
diagnostics are `diagnostic-final-node0-mgmtd.log`,
`diagnostic-final-node6-hf3fs_fuse_main.stderr` and
`diagnostic-final-node6-fuse.log`. All eleven QEMUs and CXLMemSim exit 0 and
their exact owned PIDs are absent; failed application retirement remains a
qualification failure.

Give the ten-client profile an independent host startup deadline capped at
720 seconds per client, allowing the launcher's configuration and FUSE client
initialization to each consume a configured 360-second retry window. The
single-client profile, RPC/lease limits, concurrent I/O and evidence gates
stay as before. The regression fails with the old 120-iteration loop and the
Python suite passes 113/113 with this correction, in
`full-deps/phase1-startup-budget-{before,after}-01.log`. Refresh the source-bound
build/G0 evidence and run a third qualification; 10c1s acceptance is pending.

Build 020/source closure 022 passes G0-B in
`out/cxl-riscv/g0-b/full-013/20260905T122612.839538Z-1860445/result.json`.
The 2 MiB FUSE payload SHA-256 is
`0022b472e779bcd1c03778e1436ce6e081a8556ea811d7e398584bdf0bb195e1`.
Application retirement passes; both QEMUs and CXLMemSim exit 0, with no
validation errors. Application binary hashes remain identical to build 019.

The third 10c1s run is
`out/cxl-riscv/phase1/10c1s/20260905T123855.839201Z-1860445/`.
Twenty DAX directions and local TCP FDB pass. Clients 1 through 5 mount;
client 5 needs several minutes, but client 6 exceeds the independent 720-second
host deadline. It is marked unresponsive and the run fails before concurrent
I/O. Increasing the readiness budget alone therefore does not resolve this
qualification failure. Preserve all original logs/images and the failed result.

Read-only extraction from the second run's stopped guest adds
`diagnostic-final-node0-fdb-trace.xml` and its SHA-bound
`diagnostic-final-node0-fdb-summary.json`. It contains four CommitProxyTerminated
events with failed_to_progress at guest seconds 1123, 1193, 1340 and 1485.
The longest sampled SlowTask lasts 2.79585 seconds; its task ID 10500 is
FlushTrace in the private FDB source. This complements the observed 1007/1021
transaction errors without proving one exclusive cause of the delays. Client
6's random-generator initialization takes only about two seconds after its
launcher begins, so that event does not explain its multi-minute wait.

The next candidate changes only the ten-client guest FDB launch configuration.
FDB 7.3.63's private `fdbclient/ServerKnobs.cpp` sets VERSIONS_PER_SECOND to
1e6, both MAX_READ_TRANSACTION_LIFE_VERSIONS and
MAX_WRITE_TRANSACTION_LIFE_VERSIONS to five seconds of versions, and
COMMIT_PROXY_LIVENESS_TIMEOUT to 20 seconds. Set the two version windows to
60000000 (60 seconds) and the commit-proxy progress deadline to 120 seconds
for this TCG cohort. Keep local TCP, normal transaction semantics, diagnostic
logging, memory limits and the 3FS RPC/lease checks. The single-client G0 FDB
launch remains unchanged. The result must record the exact experimental knobs;
the validator rejects absent or altered values. Two regressions fail before
the change, and all 115 Python tests pass afterward in
`full-deps/phase1-fdb-budget-{before,after}-01.log`. Refresh the source-bound
build/G0 reference before the fourth 10c1s attempt. This candidate has not yet
passed a real ten-client application workload.

Build 021/source closure 023 passes G0-B in
`out/cxl-riscv/g0-b/full-014/20260905T131931.340263Z-1891759/result.json`.
Its 2 MiB FUSE SHA-256 is
`0dc2bbbbc01b369d2fa2908c7059ac5cb686b3e9b0cd11b21b7ac42cb80c3657`;
application retirement, all process exits and validation pass. The fourth
10c1s run is
`out/cxl-riscv/phase1/10c1s/20260905T133822.824835Z-1891759/`.
All twenty DAX directions and local TCP FDB pass with the recorded knobs.
Clients 1 through 3 mount, but client 4 exceeds the startup budget, before I/O.
Thus the FDB budget adjustment alone does not resolve the qualification failure.

During that already-stalled bootstrap, a read-only diagnostic on guest 0
captures date, load, thread CPU times and the management log tail. The script
checks the exact QEMU owner token, root image and idle server console before
writing a uniquely marked shell command; it sends no application RPC.
`diagnostic-console-node0-01.log` shows guest load 8.96 on four compute cores
and repeated Mgmtd::NotPrimary responses. Runtime polling threads have consumed
substantial CPU time. This is a scheduling observation, not proof that polling
is the only source of delay. Source inspection confirms CxlProgressEngine
does sleep between scans and does not omit the configured delay.

Next calibration sets poll_sleep to 100 ms throughout the ten-client CXL
cohort, retaining the 60-second FDB version windows, 120-second commit-proxy
deadline, 120-second RPC and 180-second management lease. Single-client G0
polling remains 1 ms. No production transport default or platform code changes.
The configuration regression rejects the old 10 ms value and all 115 Python
tests pass with the updated profile, in
`full-deps/phase1-poll-budget-{before,after}-01.log`. Refresh source/build/G0
identity before the fifth 10c1s attempt; application qualification is pending.


Build 022/source closure 024 passes G0-B in
`out/cxl-riscv/g0-b/full-015/20260905T142200.522914Z-1912816/result.json`.
Its 2 MiB FUSE SHA-256 is
`c9147366a6d4a59440e7501da7e367bad0f6c0ee1227112c3321d2b29025d2bd`.
The fifth 10c1s run is
`out/cxl-riscv/phase1/10c1s/20260905T143734.255065Z-1912816/`.
Twenty DAX directions, local TCP FDB and client mounts 1 through 8 succeed.
Client 9 exceeds its independent startup deadline; concurrent I/O never starts.
The final result fails, all eleven QEMUs and CXLMemSim exit 0, and exact owned
PIDs are absent (`cleanup-verification-final.json`). Coherence analysis is
complete with zero error events. Small evidence bundles and offline guest log
extractions are preserved locally and remotely; raw images/traces stay remote.

Offline `diagnostic-final-node9-fuse.log` shows repeated 120-second RPC
timeouts from guest 00:56:46, followed by 106 occurrences of
`no reusable CXL lane slots` starting at 01:15:21. Lane exhaustion is therefore
a later observed failure, not proof of insufficient initial partition size.
Server logs stop completing management work after 00:54:44. FDB traces contain
two failed_to_progress events and 34 storage-server-list fetch timeouts.
The 100 ms polling adjustment alone has not completed qualification.

Inspection finds that `CxlConnectService::allocateLocked` clears the full
frame/payload partitions synchronously under its allocator mutex for every
connection, including retries. At q8/cell65536 this writes 1 MiB of payload
before bootstrap can return. This contradicts the design's explicit allowance
for poisoned unpublished ring/payload bytes and adds avoidable coherent DAX
traffic. Preserve cursor-page initialization, range checks and both-owner
terminal fencing; omit clearing frame/payload ranges. Producers overwrite the
published frame and exact payload length; consumers still check absolute
sequence, session/lane generation, length and CRC. No lane count, timeout,
lease or platform change accompanies this candidate.

`BootstrapPreservesUnpublishedBytesAcrossLaneReuse` verifies poisoned storage,
two lane generations, bidirectional communication, empty reads before
publication, and a shorter new message with untouched destination suffix.
Its pre-change failure is preserved in
`full-deps/phase1-lane-init-before-02.log` (the first invocation built correctly
but used the wrong executable path). Native transport checks and fresh
source-bound RISC-V/G0/10c1s qualification follow this fix; do not interpret
removing the excess writes as proof that all stalls are resolved.

The post-change native checks pass 4/4 targets: transport planes, CXL layout
(including the new poisoned/reused-lane regression), actual multiprocess CXL
transport E2E and minimal layout. Build and CTest output are retained in
`full-deps/phase1-lane-init-after-01.log`. Freeze source closure 025 and build
023, then refresh G0-B before the sixth 10c1s attempt.


Build 023/source closure 025 passes G0-B in
`out/cxl-riscv/g0-b/full-016/20260905T161845.225393Z-1938793/result.json`.
The 2 MiB buffered-write/direct-read SHA-256 is
`7d2f7d75b5fc209758d44b226241091953441dd19e960660765ba077d87850f9`.
All application retirement and platform exits pass; validation errors are empty.
Native post-fix evidence adds exact binary hashes and the complete compressed
CTest log in `full-deps/phase1-lane-init-after-01.json`: planes 30/30, layout
84/84, actual multiprocess E2E 1/1 and minimal layout 58/58.

The sixth 10c1s run is
`out/cxl-riscv/phase1/10c1s/20260905T163126.333264Z-1938793/`.
Twenty DAX directions and local TCP FDB pass. All ten FUSE clients mount and
all ten independent guests emit I/O BEGIN. This resolves the prior observed
bootstrap failure in this run, but the buffered concurrent workload fails.
Clients report I/O errors after writing 983040 bytes; no full client checksum
proof completes. Retain the failed run; teardown is in progress at this source
freeze, so do not claim clean retirement yet.

The owner-checked, idle-console diagnostic
`diagnostic-console-node1-02.log` records the initial failure precisely:
FUSE combines the 64 KiB userspace writes into a 1048576-byte request at offset
zero, yielding two 512 KiB storage operations. The first batchWrite expires
at 120071 ms, the retry sees ChannelIsLocked, and after 246110 ms the client
reports ResourceBusy/Timeout. Subsequent metadata sync retries also expire.
`diagnostic-console-node0-01-complete.log` shows server-guest load 26.52 on
four compute cores and later Mgmtd::NotPrimary. These observations support a
request-size/concurrency investigation; they do not prove one exclusive cause.
No direct diagnostics modify application state. Nonatomic live-image extracts
are labeled diagnostic-only and must not replace final offline logs.

The next 10c1s candidate uses `oflag=direct` with 65536-byte writes, retaining
2 MiB per client, ten simultaneous actors, fsync, direct readback, size/cmp/SHA
checks, neighboring-client verification and observed interval overlap. Record
`io={block_bytes:65536,write:direct,read:direct}` for every completed client;
the validator rejects missing or altered profiles. G0-B continues to cover
buffered writes. This is explicit 64 KiB direct-I/O functional qualification,
not proof of the failed ten-way 1 MiB buffered-request workload or performance.
Keep the 120-second RPC, 180-second management lease, CXL geometry, FDB knobs
and all platform settings unchanged. The profile regression fails before the
change and all 116 Python tests pass afterward in
`full-deps/phase1-direct-io-{before,after}-01.log`. Freeze source closure 026,
build 024 and a fresh G0-B before the seventh 10c1s attempt.

Build 024/source closure 026 passes G0-B in
`out/cxl-riscv/g0-b/full-017/20260905T173316.610226Z-1980046/result.json`.
The buffered-write/direct-read 2 MiB SHA-256 is
`b9e68bb3da2b222fd709fdd97b796a24ab0bb54904678f8586d9063fa982c1ec`.
The latest native full relink also passes (41 steps), with exact evidence in
`full-deps/phase1-lane-init-host-full-build-01.{log,json}`. Run six finishes
with all eleven QEMUs and the simulator exiting zero and exact owned PIDs
absent; its application workload and retirement qualification still fail.

Run seven, `phase1/10c1s/20260905T174615.368193Z-1980046/`, passes all twenty
DAX directions, initial local TCP FDB transactions and ten FUSE mounts.
All ten clients begin direct I/O; seven eventually complete their individual
write/read/hash checks. Clients 4, 6 and 8 fail. Their first errors are metadata
sync timeouts after data writes have started; FDB loses progress and management
lease renewal stalls. Offline FDB traces contain two actual
CommitProxyTerminated/failed_to_progress events before the diagnostic restart.
CodeCoverage records mentioning that error are not additional terminations.
Host observations show no sustained host memory/CPU pressure; guest diagnostics
show load above twenty on four compute cores, with FDB's main thread receiving
roughly two CPU seconds per five or more elapsed guest seconds. Symbolized
run-loop samples span metrics collection, allocation, profiling and networking;
the reported last task priority alone does not identify an exclusive cause.

Run seven was explicitly invalidated before an owner-checked diagnostic FDB
restart with trace_flush_interval=30. The restart does not establish sustained
health or isolate the trace setting's effect. Preserve the intervention and
raw failed result; never count this run as qualification. All eleven QEMUs and
the simulator exit zero, coherence analysis completes without error events,
and `cleanup-verification-final.json` confirms exact owned PIDs are absent.
Offline logs, FDB traces, symbolized samples and the intervention receipt are
retained in the same result directory. No diagnostic trace knob is promoted
into the next qualification profile.

The next candidate isolates guest CPU scheduling: launch FDB on Linux CPU 0
and constrain the owned server console and its subsequently launched 3FS
children to CPUs 1-3. This changes process affinity inside the existing guest,
not QEMU/kernel/host configuration or FDB transport. Verify each resident
server/FDB thread's Cpus_allowed_list immediately before and after the ten-way
workload, retain both snapshots, and reject missing or overlapping masks.
Keep the current RPC/lease/FDB deadlines, direct 64 KiB workload, all checksums,
neighbor reads and overlap gates. Single-client G0 retains its prior scheduling.
This tests a scheduling hypothesis; qualification remains pending until a fresh
source/build/G0-bound run passes without intervention.

The affinity parser/validator rejects missing roles, duplicate thread records,
overlapping masks and a resident process restart across the workload. All 117
Python tests pass locally and remotely; raw logs are
`full-deps/phase1-cpu-isolation-after-02.log` and
`full-deps/phase1-cpu-isolation-remote-01.log`. Invoke the existing BusyBox taskset
and awk applets explicitly because the staged rootfs has no taskset symlink.
Freeze source closure 027/build 025/rootfs full-018 and refresh G0-B before the
eighth clean 10c1s attempt.

Build 025/source closure 027 passes G0-B in
`g0-b/full-018/20260905T190651.666320Z-2020239/result.json`, with 2 MiB SHA-256
`3408eb3e7caf7e40f39dfb726e718fbf3bd65a149ff5eacce5c500f69148bc00` and clean
application/platform exits. Run eight is
`phase1/10c1s/20260905T191257.303729Z-2020239/`. All twenty DAX directions,
initial FDB transactions and ten mounts pass. All ten clients enter concurrent
I/O. The actual pre-I/O affinity snapshot contains 327 threads: FDB 6 on CPU 0,
authority 3, mgmtd 28, meta 57 and storage 233 on CPUs 1-3. Two owner-checked,
read-only console observations show management leases continuing to renew
under guest load around 21. They do not restart or reconfigure applications;
capture deadlines exceeded 25/45 seconds, with complete outputs subsequently
preserved separately. Application qualification remains in progress.

The second observation also finds a concrete CXL error at guest 00:20:03:
storage bulk pull of a 65536-byte range from endpoint 24 rejects an
"unstable or unclaimed CXL endpoint record" as DataCorruption. Endpoint records
are republished on every heartbeat. As already done for fabric lifecycle
snapshots, inability to obtain a stable owner publication should return a
bounded, retryable timeout without copying any bytes; only a stable record's
invalid CRC/identity/lifecycle is corruption/staleness. Add a deterministic
paused-publication bulk regression, retaining exact-range copy counters,
untouched buffers on rejection and stable-CRC/generation rejection. Then fix
the endpoint snapshot classification and rerun native transport checks before
freezing another source/build/G0-bound guest run. Do not relabel run eight as
evidence for a later binary.

The endpoint snapshot regression fails on the old implementation. The first
bulk regression attempt rejected its own one-hour heartbeat/100 ms stale
configuration before exercising bulk; the corrected fixture scales the stale
budget with the heartbeat and waits for each initial publication. The valid
pre-fix run `full-deps/phase1-endpoint-publication-before-02.log` reproduces
both pull and push being classified as DataCorruption, plus the endpoint
snapshot failure. Preserve the first attempt separately.

Endpoint snapshots now return RPC::Timeout when bounded observations cannot
obtain a stable publication. Allocation snapshots use the same classification
because concurrent exports may republish permissions while older leases pin
the allocation. No copy occurs before stable CRC/identity/range/permission
validation. New tests cover endpoint and allocation publication, unchanged
buffers/counters on rejection, recovery, stable CRC rejection and absence of
peer-liveness renewal from an unstable snapshot.

The first full native regression also exposed an existing ordering race:
`check()` could observe backgroundError before acceptingSubmissions was cleared
after stderr logging. Close the submission gate before publishing the error.
The existing StuckLifecyclePublicationStillExpires test then passes 100
consecutive repetitions (`full-deps/phase1-fault-gate-repeat-01.log`).

Post-fix native checks pass 4/4 targets: planes 30/30, layout 87/87, actual
multiprocess E2E 1/1 and minimal layout 59/59. Exact binary/source hashes and
compressed complete CTest output are retained in
`full-deps/phase1-endpoint-publication-after-01.json` and
`phase1-endpoint-publication-ctest-details-01.log.gz`. The full native relink
passes all 44 build steps (`phase1-endpoint-publication-host-full-build-01`).
Freeze source closure 028/build 026/rootfs full-019 for the next G0/10c1s run,
retaining the verified guest CPU masks and existing workload/deadline profile.

Run eight subsequently reports a real client 9 I/O error and renewed management
lease stalls. Preserve the observed error before aborting the failed workload.
An owner-checked server sync completes, then all eleven owned QEMUs receive
SIGTERM; the runner stops its simulator. Final coherence analysis reports zero
error events and all twelve owned PIDs are absent. This aborted run fails the
application, receipt and retirement gates. Its teardown-console error is an
abort consequence, not the initial application failure. Retain the abort
receipts, final cleanup verification and offline server/client/FDB logs in
the run directory; offline image contents can omit unflushed log tails.

Source closure 028/build 026/rootfs full-019 completes cross compilation with
the endpoint/allocation/gate fixes. Its pipeline is held during prelaunch
image staging and then explicitly terminated after checking exact identity,
no children and no matching QEMU/simulator. Preserve those artifacts and the
prelaunch-superseded receipt; no ninth 10c1s qualification was launched by it.

The next profile tests diagnostic overhead in the machine-local TCP FDB server.
Run eight's available FDB metrics show roughly 4.5-5 CPU seconds per 5 elapsed
seconds, with no RunLoopBlocked events in the persisted traces. Prior
symbolized samples include system metrics, database metrics, allocation and
stack profiling; they do not prove one exclusive cause or a deadlock.
FoundationDB's NativeAPI database logger and CounterCollection both run at
FlushTrace priority, so priority 10500 alone does not identify trace flushing.
For ten clients only, set system_monitor_interval, worker_logging_interval
and storage_logging_delay to 30 seconds and run_loop_profiling_interval to 0.
The last setting disables the optional signal/backtrace profiler according
to Platform.actor.cpp; ordinary sampled SlowTask and warning/error traces
remain enabled. Preserve trace flush/severity settings, transaction/proxy/RPC
deadlines, leases, CPU masks and every workload/validation gate. G0 keeps its
existing default FDB profile. Test the exact command and evidence contract,
then freeze closure 029/build 027/rootfs full-020 for fresh G0 and 10c1s.

## Accepted phase-1 and 10c1s result

Acceptance time: 2026-09-06 Asia/Shanghai. Stop at the user's phase-1 boundary;
machine-local FoundationDB, Core and bootstrap remain TCP. Phase 2, physical
CXL validation and cross-system performance comparisons were not started.
No QEMU, cxlmemsim or kernel code or host configuration was changed. No commit
or push was created.

The diagnostic-profile regression first fails on the previous command/receipt
contract, then all 118 Python tests pass locally and remotely. Logs are
`out/cxl-riscv/full-deps/phase1-fdb-diagnostics-before-01.log`,
`phase1-fdb-diagnostics-after-01.log` and `phase1-fdb-diagnostics-remote-01.log`.
Build 027 retains exactly build 026's application hashes; only the runner
profile changes after the endpoint/allocation/gate fixes. Immutable identities:

- Source: `out/cxl-riscv/full-deps/source-closure-029.json`, SHA-256
  `85be86b21660cc6f3719381bae21b0bcc3d5f171941f2c5a7e1abb9725fefd26`.
- Build: `out/cxl-riscv/full-deps/build-027-manifest.json`, SHA-256
  `72ea0367b4f4598fb218fa2e5ce9a1aaa5d79a8591375aaf57056fe4c319d774`.
- Rootfs: `out/cxl-riscv/rootfs/full-020-rootfs-manifest.json`.
- Fresh G0-B: `out/cxl-riscv/g0-b/full-020/20260905T202248.249115Z-2074312/result.json`
  and its run-local `g0-b-handoff.json`. Buffered 2 MiB write/fsync and direct
  readback pass with SHA-256
  `7bd005a4c96e126a0985e9fa55ce0e8fedeaf8891bddb3aa2191cdc1af1483be`,
  clean application retirement and all platform processes stopped.
- Final 10c1s: `out/cxl-riscv/phase1/10c1s/20260905T203556.712060Z-2074312/result.json`,
  SHA-256 `ce8b9092e64030a32f960edb686a77998505c220d22a824407d1316e1f808b2c`.

This ninth actual qualification prints `HF3FS_CXL_10C1S_OK`, exits zero and
has `status=passed`, no first failure and an empty validation-error list.
All eleven independent guest images boot, all twenty fresh DAX directions
pass, and guest-local TCP FDB set/get succeeds. All ten FUSE mounts pass;
each client writes 2 MiB using 64 KiB direct requests plus fsync, directly
reads and byte-compares its file, and checks SHA-256. Another independently
mounted client reads each completed file and matches its hash. The ten actual
host-observed I/O intervals have a common overlap of 1935.132936211 seconds.
The workload transfers 20 MiB of unique write data and 40 MiB of self/neighbor
read data. This does not claim ten-way buffered 1 MiB request coverage; the
earlier buffered cohort failed and remains preserved.

The CPU snapshots before and after all I/O validate every resident thread.
FDB remains PID 107 on CPU 0 with five threads; authority PID 144, mgmtd PID
169, meta PID 542 and storage PID 204 remain on CPUs 1-3. The snapshots contain
326 and 330 threads respectively (four additional storage threads); process
identities do not change. Three owner-checked read-only console observations
retain process statistics and continuing management lease renewals, including
successful renewals around guest 00:40:57 beyond the prior run's stall.
Each initial 45-second capture is retained separately from its complete log.
There is no application restart, runtime reconfiguration or abort in this run.
Observed durations use log timestamps, not the upstream operation-latency label.

All fifteen endpoints have clean retirement receipts. The 24 total receipts
contain generations 1-10 for admin endpoint 7 and generation 1 for every other
endpoint. The summed receive-side counters are:

| Counter | Bytes/count |
| --- | ---: |
| TCP receive bytes | 46156 |
| Core TCP bytes | 10606 |
| Bootstrap control bytes | 35550 |
| TCP data-plane bytes | 0 |
| Bootstrap serving bytes | 0 |
| RDMA open attempts | 0 |
| CXL RPC requests / responses | 12040 / 12040 |
| CXL RPC bytes | 6846328 |
| CXL bulk pull / push bytes | 20971520 / 41943040 |

TCP bytes are fully accounted for by Core and bootstrap; no data-plane fallback
occurs. Coherence analysis completes with zero error events, eleven host
registrations and all twenty required dirty-handoff directions. It processes
4,673,744 requests, 4,550,513 snoop sends and matching acknowledgements, and
2,793,237 dirty completions. Preserve the 4,664,226,456-byte raw coherence trace
and guest images remotely. The local and remote small bundles retain result,
configuration, command/console logs, transport receipts, host observations,
offline service/FDB logs and `cleanup-verification-final.json`. Final validation
is repeated against the recorded result; exact owned PIDs and the owner token
are absent, all eleven QEMUs and the simulator exit zero, and the pipeline and
passive host observer finish. Parsed persisted FDB events contain 42
SS-list-fetch timeout warning records and no failed-to-progress or
CommitProxyTerminated events. The final XML file lacks a complete ending;
these counts describe available events, not a proof that FDB emitted no other
internal errors. The passing functional run does not establish a production
latency guarantee or isolate one exclusive cause of earlier FDB stalls.

The restored native suites, full CXL-only native/RISC-V builds, host FUSE and
external storage benchmark, latest publication/fault regressions, and bounded
formal results are recorded in the preceding checkpoints. The only broad-suite
failures remain `RenderConfig.testNormalV0`, `RenderConfig.testTemplateNotParsed`
and `RenderConfig.testNormalV1`, unrelated upstream template-render expectations.
This completes functional qualification only. The requested standard IO500
measurement remains open in the linked standard plan.
Only acceptance documentation is updated after the frozen qualification source;
application code and runtime settings remain those exercised by this result.
