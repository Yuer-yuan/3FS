# Phase-1 baseline findings (2026-09-08)

Current acceptance is **not complete**. The records below are historical evidence,
not acceptance for the instrumentation being implemented in this session.

| Comparison | Verified finding | Evidence |
| --- | --- | --- |
| Historical acceptance | closure029/build027/rootfs020 matches the passing full-020 run, 20260905T202248.249115Z-2074312 | full-020/result.json; full-020-rootfs-manifest.json |
| Latest passing G0-B | full057 uses closure089/build087/**rootfs079**; the similarly named rootfs057 is a different artifact | full-057/20260908T013035.773899Z-349863/result.json; full-079-rootfs-manifest.json |
| Failing qualification | qualification008 uses exactly the same source/build hashes as full057 | qualification-008/20260908T014201.577453Z-358440/result.json |
| Dependencies | All nested dependency identities and the entire build dependency-evidence dictionary are unchanged between closure029/build027 and closure089/build087 | source-closure-029/089.json; build-027/087-manifest.json |
| CPU affinity | full020 starts Meta/storage without individual taskset prefixes. Both full057 and qualification008 start Meta with mask `c`, storage with `8` | full-commands.json from each run |
| Guest memory | full020 G0-B uses 4G; full057 G0-B uses 2G | qemu_commands in the corresponding result.json |
| Mount readiness | Both historical G0-B records report 120-second FUSE startup budgets | full_stack.fuse_startup_budget_seconds |
| Business failure | All ten qualification008 clients fail their 2 MiB direct write/read commands, at 378.6–401.2 host seconds | full-command-075..084 entries in full-commands.json |
| Diagnostic failure | Commands 087, 090, 093 contain a physical newline inside printf and hit the 30-second collection deadline. Commands 086/089/092 also emit 16–21 KiB and take 27–29 host seconds | exact command strings, durations and output byte counts in full-commands.json |
| Response evidence gap | Logs contain Meta stat (`service=4, method=2`) call completion, but the previous server_response_queued event ignores Transport::send's return value | qualification008 service tails; pre-session CallContext.h |
| Retirement failure | fabricd command 128 waits and force-kills; command 129 cannot find FABRIC_RETIRED. Application clean_retirement is false | qualification008 full-commands.json and result.json |

Complete machine-readable comparisons, original manifest hashes, and failed
command timings are retained in
`out/cxl-riscv/investigation-20260908/baseline-comparison.json`.
The fetched original records are in its sibling `baseline/` directory; the
comparison generator is `compare_baseline.py`.

Tracked source patches were represented by a digest in these old closures, not
by patch contents. The historical per-file tracked changes, GC settings, and
all rendered timeout/scheduling values cannot be reconstructed from a digest.
Untracked source-file hashes identify changed orchestration, CxlSocket, and
probe files. These limitations are explicit in the JSON; they are not evidence
that a fixture migration must be repeated.

The old serial tails are incomplete. In particular, the 209-byte completion
reported at socket close cannot be labeled a stat/create response without its
serialized identity; health checks share connections. Guest timestamps are not
subtracted across machines. No business root cause is claimed from these logs.

Verification completed so far: 176 Python deployment tests pass (11.737 host
seconds), including regression evidence for physical newlines, complete marker
matching, pending-command serialization, long UART input, bounded binary export,
corrupt/truncated/overflow records, and independent business/collection/retirement
failures. The UART regression limits accepted physical line length, reproduces
truncation before the fix, and verifies shell-variable persistence afterwards.
The trace receipt merge regression fails before its fix and passes afterwards.
A standalone C++ recorder test checks 400 concurrent records, explicit overflow
and torn-file rejection. The final native CXL/publication/Processor/service/
retirement regression passes 115 tests in 21 suites (19.873 host seconds).
The named syscall probe passes native existing-directory stat, ENOENT stat,
and create/fsync/stat/unlink checks.

Current G0-B `investigation-002/20260908T031929.516997Z-443458` passed with
`closure-004.json`, `build-004.json`, and `investigation-004-rootfs-manifest.json`.
The 2 MiB input/readback SHA-256 is
`ed124e7855882f332bc2bdbcb4057e006523a5c00c02f1396da2c15d8f1978b2`.
Storage bulk read/write counters are each 2097152, forbidden data-plane
TCP/RDMA counters are zero, application clean retirement is true, and all owned
processes stopped. Host elapsed time is 646.474 seconds. The collector exported
one complete trace per guest but failed while merging its export and validation
receipts (`bytes` duplicate keyword). Consequently the Meta trace is unavailable:
this run is platform/I/O evidence, **not** a business root-cause closure. A late
attempt to retain the temporary QEMU root overlays could not open the already
exited QEMU processes; it recovered no additional evidence. All existing logs
and traces are retained, with hashes recorded before deleting six run-owned
temporary disks. Reusable caches remain intact.

The corrected collector is represented by closure/build/rootfs `005`; all 23
staged binary SHA-256 values match `004`, so the existing handoff's exact binary
and platform compatibility check is reused. The named 10-client diagnostic
run `diagnostic-minimal-001/20260908T033618.201929Z-457493` failed before
submitting its first RPC: the UART transcript says `/bin/sh: mv: not found`.
Its newly added probe publisher used an absent applet symlink and then entered
its wait loop despite the failed publish. This is a diagnostic harness defect,
not a reproduction of qualification008. A restricted-PATH native regression
fails before the fix and passes with explicit `/bin/busybox mv` plus fail-fast
publication. The probe budget remains 600 seconds.

The run's timed-out console caused its client to be excluded from subsequent
serial teardown commands. fabricd's complete trace identifies the remaining
participant as endpoint 16, generation 1, lifecycle 2. This explains this run's
retirement failure, not necessarily qualification008's separate exit failure.
All 11 QEMUs and cxlmemsim stopped. Eleven original ephemeral QCOW2 overlays
were retained through read-only descriptors before exit. QEMU keeps the backing
link in memory, so recovery attaches the recorded immutable base to an overlay
copy, converts it, replays its journal on a working copy, and exports logs.
The original snapshots/cache are unchanged. All 12 recovered request traces
pass length/sequence/count/hash validation, including the timed-out client's
527 records. Neither client nor Meta contains a method-50 submission/reception.
The complete recovery receipts and trace validation are saved with the run.

An additional failed-file export regression verifies that a damaged process
trace does not prevent collecting later processes on the same guest. Source/
build/rootfs generation006 includes these Python fixes; another named minimal
run is underway using the unchanged staged application binary hashes.
Two qualifications and IO500 standard remain outstanding. No business or
qualification008 retirement root cause has yet been established.


## Finite diagnostic request evidence (generation006)

`diagnostic-minimal-002/20260908T040255.128297Z-481641` passes one-client
and ten-client testRpc, and one-client existing-directory stat. In the ten-way
existing-directory stat, client 1 returns without another Meta RPC (cache is a
possible explanation), while clients 2–10 hit the unchanged 600-second diagnostic
command gate. Missing-file/create/bulk probes are not executed.

All 12 process request traces recovered after QEMU exit pass the recorder
integrity checks. Joined request records are saved in
`investigation-20260908/minimal-002-request-chains.json`. The focused identity
`endpoint=17, UUID=1536, service=4, method=2` has these stages:

- Client submission to caller wake: 120.029090 process-monotonic seconds;
  wake status 2005 (RPC timeout).
- Server call begin to end: 689.392714 process-monotonic seconds;
  final status 1003, followed by serialized response and actual send rejection
  2007. The response's late rejection is downstream of the business operation.
- The first FDB future registered on that server request's thread (sequence490)
  registers at 424.129545, receives callback at 1112.495249, and resumes at
  1112.496453 on the server's own monotonic clock, with FDB status1007.
  No cross-guest timestamp subtraction is used.

FDB's pinned source defines 1007 as transaction_too_old. Its recovered 5.31 MB
server trace provides independent scheduling evidence: DurableVersion remains
75052057 for many minutes, DiskMetrics shows WriteQueue=1, FinishedQueries stays
1909 while LowPriorityQueries accumulates, and NetworkMetrics reports entire
30-second windows starved below priority3500, with a maximum observed continuous
starvation interval of786.654 seconds. DiskWrite has priority3010 and
LowPriorityRead2100 in the pinned TaskPriority.h. Callback-to-coroutine resumption
is prompt; the first localized delay is before FDB completion.

This identifies starvation and its downstream request failure, but not one
exclusive source of high-priority work. In particular, priority10500 is shared
by trace flushing, NativeAPI database logging and CounterCollection. A proposed
trace-flush interval change was withdrawn before any build/run, after rereading
the historical profiling record and its unsuccessful, non-isolated restart.
FDB trace/transaction/proxy settings remain unchanged.

Generation007 is an explicitly unproven, single-factor diagnostic candidate:
ten-client CXL shared-state polling cadence is100 ms instead of10 ms. All other
runtime settings and staged application binary bytes are unchanged; G0 keeps
1 ms. An older100 ms attempt failed before subsequent connection initialization
fixes, so its result does not prove this candidate will work. The candidate is
being compared using the same finite request sequence and FDB starvation
metrics. The old polling-value assertion failed as expected; its updated exact
configuration contract passes locally. No qualification or standard acceptance
is claimed for this candidate.


## Generation007 fails sustained I/O; collection channel correction

`diagnostic-minimal-003/20260908T044252.792018Z-506686` passes all43 named
probes. The following original ten-way2MiB direct-I/O workload fails: client6
returns I/O error after894.364 host seconds with131072 bytes written, while the
other nine reach the original1800-second host command budget. Client6's complete
trace and service log report repeated Meta sync RPC timeouts followed by
Meta::OperationTimeout(3203). Recovered FDB NetworkMetrics again contains full
30-second windows starved below priority3500. The100ms polling candidate is
rejected as a cure; the source default is restored to the10ms cohort baseline.

All11 QEMU processes and cxlmemsim stop with returncode0, but application
clean_retirement is false. After offline journal replay on disposable working
copies, ten request files fail length/count validation; only client6 validates
(1386 records, SHA2563cab28ff5a4f1464b50746efa0a215248106fda3cb1775d53eea8745f2b810ab).
No missing-stage conclusion is drawn from damaged traces. Original snapshots,
recovered logs and hashes are retained;33 owned temporary data/configuration
files were removed only after stopped-process and recovery checks.

Foreground workload commands occupy the only guest control shell until they
return. When a business command times out, this prevents timely diagnostics and
application retirement. The host workload runner now launches each finite probe
or dd workload in an identified session and waits for its separate completion
marker, keeping the control shell available. The original business budget and
exit status remain authoritative. Cancellation validates session leader PID and
start ticks; cancellation errors and remaining session members are retirement
failures. Native FIFO-gated tests exercise a blocked workload released by a
second control command, timeout followed by working diagnostics and scoped
cancellation, and nonzero business status. This fixes a collection/lifecycle
obstacle, not the FDB starvation cause or qualification008's retirement root.

A separate native TCP FDB7.3.63 server with the same memory/cohort knobs completes
ten concurrent fdb_client_smoke probes in90–112ms each, including callback and
coroutine-wrapper paths. It does not reproduce the BI delay. Artifacts are in
`investigation-20260908/native-fdb-probe-001`; this is diagnostic evidence only.


## Failure cleanup signal race: deterministic regression

Qualification008 commands115–124 all report FUSE exit143 in1.26–1.99 host
seconds, whereas Meta/storage/mgmtd exit0. Command128 then kills cxl-fabricd
after39.43 host seconds without its retirement marker. The failure cleanup
branch executes lazy unmount and immediately sends SIGTERM to the same process.
`FuseMainLoop.cc` removes libfuse signal handlers as the loop exits, before
`FuseApplication::stop` / `FuseClients::stop` finish CXL participant retirement.
Thus this extra signal can terminate a process during normal cleanup.

`test_fuse_retirement.py` places a real child process at that exact boundary
using FIFO acknowledgements: signal handler removed, cleanup waiting for the
parent's first wait-loop iteration. The original command fails deterministically
with exit143 and no retired marker (`fuse-retirement-before-2.log`). The fix
sends no additional signal after a mounted client is unmounted; it keeps the
existing wait/force-stop budgets. The same test now exits0 and writes the retired
marker (`fuse-retirement-after.log`). The first test-harness attempt blocked its
own FIFO release after the child died; that timeout remains recorded separately
in `fuse-retirement-before.log` and is not the causal regression result.

This source-level race and deterministic fix explain a concrete failure cleanup
path consistent with qualification008. BI confirmation is still required;
business/FDB starvation remains a separate issue. Generation008 is already
running with its frozen earlier cleanup code and is not modified in place.


Generation008's `diagnostic-minimal-004/20260908T053451.587371Z-542984`
reproduces the ten-way existing-stat timeout with10ms polling and the original
FDB scheduler quantum. All12 serial-exported process traces validate. The client
at endpoint17 submits UUID1536 and wakes with status2005 after120.019708 of its
own monotonic seconds. Meta's matching call lasts707.953926 seconds and then
rejects the late response with2007. FDB future442, registered immediately after
that call's begin on the same thread, resumes with1007 after703.962994 seconds;
the future/thread adjacency is recorded as supporting evidence, not an explicit
RPC-to-future context propagation claim.

All ten clients again exit143 and none of their complete traces enters
runtime_stop_begin. The fabricd trace explicitly reports
`retirement_wait endpoint=16 generation=1 lifecycle=2 heartbeat=872` after its own
stop begins. This reproduces the failure cleanup symptom with intact records.
Joined requests are in `minimal-004-request-chains.json`.

The next business experiment changes only FDB's scheduler quantum before the
finite workload:1,000,000 to100,000,000 nanoseconds. The pinned RISC-V
`timestampCounter` uses CLOCK_MONOTONIC nanoseconds; Net2 processes ready timers
again each time its1ms quantum expires. Failed windows average only about2.3
executed tasks per reactor iteration and show long low-priority starvation.
The candidate tests whether allowing more queued work per iteration restores
storage/read progress. Transaction, RPC, operation, lease and phase deadlines,
trace settings,10ms CXL polling and q8/cell65536 remain unchanged. A native
FDB7.3.63 startup/ten-client probe accepts this knob and passes in90–109ms per
client; this sanity test does not establish a BI cure.

The cleanup signal fix is applied only after the workload ends. A separately
named `diagnostic-minimal-retirement` workload preserves the complete finite
business result and, only if it passes, injects an explicit failure to exercise
failure cleanup. An earlier real business error is never replaced. The overall
run remains diagnostic and is not qualification/standard evidence; the existing
acceptance validator is not relaxed for the intentional failure.

Generation009 (`diagnostic-minimal-retirement-001/20260908T060459.545717Z-571625`)
passes all43 named finite probes with the100ms FDB quantum, but the ten-way2MiB
workload still reaches its original1800-second observation gate. Client6 logs a
Storage batchWrite RPC timeout (2005) and StorageClient timeout7008. Therefore
the quantum candidate is not a demonstrated starvation fix. No qualification or
standard score has been obtained for these artifacts.

The failure cleanup now reports `clean_retirement=true`, no retirement errors,
12 validated serial request traces, and no collection errors. All11 owned QEMU
processes and cxlmemsim exit0. Combined with the deterministic original-command
rc143/fixed-command rc0 native regression and generation008's still-active
endpoint16, this validates the cleanup signal-race fix in the BI cohort. This
is separate from the unresolved business starvation.

At the user's request to localize faster, an explicitly diagnostic A/B/A
intervention reused the active cohort: nine FUSE processes were briefly stopped
with independent30-guest-second resume watchdogs, leaving client6 and every VM
running. Explicit resume verified all nine process identities/states. Identical
server scheduler collection took22.373/5.917/33.825 host seconds before/during/
after. Host QEMU thread runnable waits remained small. This supports a shared
activity-pressure hypothesis, not a claim that a particular CXL lock was proven.
The guest FDB thread with the largest cumulative runqueue wait is TID131,
`fdb-trace-log`, nice10 onCPU0; FDB's main thread is nice0 onCPU0. Cumulative
waiting is not proof that the logger caused transaction starvation. Client6's
post-intervention file position was983040 bytes, but no adjacent pre-pause sample
exists, so write progress cannot be attributed to this intervention.

The first one-off helper misrecognized doubled carriage returns in the serial
end marker. Its raw complete output and failed receipt are retained; no STOP
was issued by that attempt. The corrected helper retained each command, host
anchor, output length/hash and explicit restoration. A host debugger attach
was unavailable due ptrace restrictions; no host/QEMU configuration was changed.

`diagnostic-starvation` now provides a short replacement for repeated long
localization runs: submit one machine-local TCP FDB smoke transaction alongside
ten CXL-backed existing-stat probes, observe for20 host seconds, and if work
remains, pause nine FUSE processes briefly while client6 stays active. A120-host-
second observation bound leaves transaction/RPC timeouts unchanged; unfinished
jobs use the existing owned-session cancellation and application cleanup.
Guest watchdogs and explicit PID/starttime-checked restoration cover lost STOP
acknowledgements and host exceptions. Before/during/after per-thread scheduler
records have individual guest uptime anchors; no guest/host subtraction is used.
A during sample is identified as a verified pause window only if all nine
processes are still stopped before and after it. All requests finishing before
the baseline ends is recorded as no reproduced stall, not a successful fix.
The workload always remains diagnostic and cannot pass the acceptance validator.
The187 deployment tests pass, including real-process watchdog/identity checks
and partial-pause error restoration. This diagnostic has not yet been run on BI.

The first short run (`diagnostic-starvation-001/20260908T070158.151175Z-597962`)
does not reproduce a business timeout: all ten existing-stat calls finish in
5.88–8.46 host seconds. The local FDB program exits0 after27.50 host seconds and
its raw JSON explicitly reports successful set/get, callback and wrapper checks
(callback166ms, wrapper229ms in its own clock). The diagnostic parser incorrectly
rejected that JSON because an interactive `hf3fs-g0-0# ` prompt preceded it.
That parser error is not a failed FDB transaction. The raw record is retained;
normalization of this exact prompt is confined to the diagnostic, with the raw
output saved. The official verifier and strict probe validator remain unchanged.
The pause experiment loses guest5's command acknowledgement; its own watchdog
explicitly reports resume rc0. The attempted partial intervention is invalid for
a causal comparison, and its collection/control errors are retained separately.

The user explicitly authorizes scaling from1 to2 clients and trying more FDB
cores. `diagnostic-scale --diagnostic-clients N` now boots exactly N client VMs
plus one server. A separate `cohort_config` switch preserves the ten-client
service TOMLs at N=1/2:10ms polling, same thread counts, GC, FDB path, deadlines,
q8/cell65536 and full2GiB fabric geometry. The acceptance workloads reject a
client-count override; their ten-client validators are unchanged.

Within each diagnostic cohort, `scaling_probe` compares all FDB threads onCPU0,
then allowed onCPU0,4, thenCPU0 again. Other service affinities stay fixed. FDB's
single main event loop does not become parallel; the extra core allows its
logger and I/O helper threads to run separately. Each round advances through
existing-stat, create/fsync/stat/unlink and2MiB direct write/read/compare, with an
independent local TCP FDB transaction alongside each stage. It stops advancing
at the first failing stage and restoresCPU0 in a finally block. Producer output
is written to per-probe files and exported with length/hash verification, so
interactive prompts cannot corrupt JSON. Business completion and diagnostic
export have separate host timestamps. These measurements are diagnostic only;
none substitutes for qualification or standard. The first1-client run is next.

The existing startup assertion and successful run logs establish that `sifive_u`
with QEMU `-smp 5` exposes Linux CPU0–3, not CPU0–4. The management hart accounts
for the difference. Earlier discussion of CPU4 as idle in that configuration
was incorrect. `diagnostic-scale` therefore uses `-smp 6` only for the server,
retaining `-smp 5` for each client. The cached kernel has CONFIG_NR_CPUS=64, so no
kernel rebuild is needed. Startup explicitly requires server CPUs0–4 online;
every FDB affinity change checks each thread's actual Cpus_allowed_list against
`0` or `0,4`, detecting kernel mask intersection instead of trusting taskset's
exit code. Both halves of the in-place comparison use this same server VM.
Normal G0, qualification and standard startup keep their existing CPU geometry.
The191 deployment tests pass, including both config disks for a1-client cohort.

The cached QEMU rejects `-smp 6`: machine `sifive_u` supports at most5 harts.
`diagnostic-scale-1c-001/20260908T072318.694271Z-609101` stops before guest boot
and before any application starts; its startup error is not a business failure.
No QEMU/kernel modification is made. The executable diagnostic is corrected to
use the existing Linux CPU0–3: FDB CPU0 / CPU0–1 / CPU0. CPU1 remains shared with
mgmtd and transient control work, whose affinities are unchanged. This compares
additional scheduling eligibility, not two dedicated cores. Actual per-thread
mask verification now requires `0` or `0-1`. The previous CPU4 proposal above
is superseded and was never applied to a running guest.

`diagnostic-scale-1c-002/20260908T072814.559807Z-614796` uses exactly two QEMU VMs
on the unchanged5-hart machine. With FDB onCPU0, existing-stat completes in2.48
host seconds and create/fsync/stat/unlink in4.96. The2MiB I/O observation ends
at120 host seconds, while the local FDB transaction alongside it completes in
4.36 seconds (callback67ms, wrapper450ms in the producer's clock). The2-core
round was not reached because the first failed stage stops the ramp. All three
request traces export intact; application retirement is clean, both QEMUs and
cxlmemsim exit0, and six temporary disks are cleaned after offline recovery.

The final Storage counters show1,638,400 CXL bulk-read bytes (25*65536) and zero
bulk-write bytes. These are completed pulls into Storage over the process
lifetime, including cleanup, not25 proven completed caller writes within exactly
120 seconds. They establish positive data movement during this run and narrow
the slow portion to the write-side path; no bulk readback was observed. No
Storage/FUSE Timeout or Failed lines were found in the recovered service logs.
The diagnostic observation timeout alone does not establish an RPC failure or
deadlock. The local FDB sample completes near the start of the I/O interval and
does not prove FDB stayed healthy throughout the entire interval.

The next1-client diagnostic starts with FDB eligible onCPU0–1. It inserts a named
single64KiB-cell probe before the unchanged2MiB write+fsync/read invocation.
The cell probe separates write/close, fsync/close and read/close. Each dd child
is sampled via its actual output descriptor's target and position, with guest
uptime anchors, to distinguish continuing low throughput from a fixed offset.
Failed-producer output is exported as a bounded snapshot; its collection error
cannot replace the original observation failure. A complete same-cohort
CPU0–1 / CPU0 / CPU0–1 comparison remains conditional on those stages completing.
The192 deployment tests pass, including real one-cell write/fsync/read/compare
and stage-marker verification. Client count stays1 until this slow layer is
understood; no new ten-client long run or acceptance workload is started.


### Single-client CPU0–1 result and persistent diagnostic session

Run `diagnostic-scale-1c-003/20260908T075552.328011Z-630870` completed the
64KiB write/close, fsync/close and read/close in approximately12.15,3.60 and
5.39 guest uptime seconds respectively; content comparison passed. The2MiB
operation exceeded the120 host-second diagnostic observation bound. Concurrent
local FDB probes completed in4.44–6.22 host seconds, without establishing health
throughout the full I/O interval. Extra FDB scheduling eligibility alone has
therefore not eliminated slow I/O. This is not yet a controlled paired core
comparison or proof of FDB causality. Both QEMUs and the simulator stopped;
logs were recovered before six temporary disks were removed.

Optional bulk uprobes were not installed: the guest lacks a standalone `cut`
applet link, and binary hash verification failed before event registration.
This collection failure is separately recorded, with no inferred copy timing.
The auxiliary helper now explicitly uses `/bin/busybox cut`.

The next diagnostic keeps the two-VM cohort alive for a bounded900 host-second
interactive session. This is session lifetime, not a changed RPC deadline.
Atomic host request files drive bounded commands, individual64KiB operations,
verified exports and shutdown. A timed-out workload is cancelled by its owned
session identity and verified stopped before another experiment; uncertain
cancellation ends the session. The cached executables and rootfs are unchanged.


### Paired core comparison and instruction-level localization

Persistent run `diagnostic-scale-session-1c-001/20260908T081157.054389Z-634238`
completed CPU0 / CPU0–1 / CPU0 single-cell probes in one cohort. Client dd write
reported11.160 /8.917 /8.722 seconds; restoring CPU0 did not regress, so these
samples do not support an FDB core-count remedy. Storage64KiB pull copies took
1.594 /1.454 /1.180 guest-monotonic seconds; push copies0.090 /0.033 /0.068.
Each trace has exactly6 events, verified export hashes/lengths and zero drops.

A fourth CPU0 cell added nine verified instruction probes to the unchanged FUSE
executable (SHA256 `46075d60cc02dcbc5caf26b51f8ae79e67c93329f8b1d714c2e72f2b388ddadb`).
Within that guest, the write callback took4.364 seconds: buffer allocation0.0011,
copy into CXL1.0034, synchronous flush3.2819, FUSE reply0.0022. Storage's pull
copy independently took1.2043 seconds. Do not subtract timestamps across guests
or treat the per-guest durations as a complete RPC UUID chain. This localizes
slow work without proving the ten-client starvation root cause. All nine FUSE
and six Storage events are retained with no buffer loss. The local analyzer
`out/cxl-riscv/investigation-20260908/analyze_session.py` validates export receipts,
event counts and drops before calculating within-guest durations; results are
in `session-1c-001/analysis.json`.

The initial automatic probe setup hit its30-second serial command observation
while hashing Storage; the command subsequently completed with rc0. The live
session recovered by checking that completion and registering probes without
rebooting. Initial setup errors remain preserved; manual registration receipts
8–14 establish actual enablement. A first malformed manual command was rejected
before guest execution (receipts2–7). Future verification uses a bounded owned
hash job and verified file export, keeping the control shell available.


### Two-client sessions: narrow slow scheduling, do not claim starvation closure

`diagnostic-scale-session-2c-001/20260908T082621.909344Z-640227` has three VMs,
two independently mounted clients and detailed RPC tracing disabled. Both
64KiB chains exceeded their60 host-second diagnostic window. Recovered producer
logs nevertheless show completed write phases26.03/26.18 guest seconds, fsync
9.63/9.34 and readback9.56/10.86. Neither complete-chain success marker exists.
The concurrent local FDB smoke completed in14.33 host seconds. Final Storage
bulk counters are131072 bytes in each direction, including cleanup. Thus this
is slow progressing I/O, not proof that the first write never completes.
The manually recovered probe enablement was not recognized by the original
wrapper and its kernel trace was not saved; a late rescue command reached an
already halted guest and has no completion marker. It is excluded from causal
analysis. All three QEMUs and simulator stopped; logs/FDB were recovered before
nine temporary disks were deleted. Clean retirement is false due to workload
cleanup errors and must not be relabeled a pass.

One cleanup defect is independently fixed in `guest_jobs.py`: an owned job can
finish after its diagnostic deadline, leaving its leader gone by cleanup. The
old cleanup then tried to authorize a kill from a missing PID and reported an
error. Cleanup now accepts only a terminal marker following the matching token,
PID and start-ticks marker. It records the real late return code without erasing
the original timeout/status. A FIFO-controlled native test fails on the old
implementation and passes after the fix, including a late nonzero return7.
All193 deployment tests pass in18.283 host seconds. Closure/build/rootfs016
reuses the unchanged executable set; manifest/build work15.20 seconds, staging
9.96 seconds. No new C++ executable was introduced.

`diagnostic-scale-session-2c-002/20260908T084137.928524Z-648387` uses that host
cleanup fix and bounded instruction probes. Cached build/rootfs identity is
verified by the existing handoff, and each actual guest probe instruction is
checked before registration. It avoids a redundant whole-file guest hash read.
The trace is now saved on every wrapper exit, including optional setup failure;
a mock failure test confirms that export still runs and the workload error is
preserved. All diagnostic write phases below use unchanged64KiB cells and
60-second observation bounds; fsync/read/unlink are deliberately separate from
this named write-only diagnostic, not acceptance workload substitutions.

With two connections but one active writer, dd reports19.64 and21.68 guest
seconds. The first Storage copy is1.37 seconds. The profiled second request's
FUSE callback takes10.934 seconds: allocation0.00123, copy2.2005, flush8.7164,
reply0.00077. With two active writers the first paired dd phases take30.77 and
33.79 seconds; Storage copies take5.9307 and6.7336. All4/8/9-event captures
export with matching length/hash and zero buffer loss. These distinguish
connection count from active work but do not isolate the reason for all
cross-run differences: the1-client session had detailed RPC tracing enabled,
the2-client sessions did not, and run-to-run timing varies substantially.

Filtered scheduler events then measure the same Storage copy intervals on the
server's monotonic clock. The two copies in a97-event, zero-loss capture take
3.1632/2.2927 seconds. Of that, guest Linux schedules them out while runnable
for1.6458/1.0525 seconds; scheduled intervals total1.5174/1.2402 seconds.
Scheduled time may include host QEMU/emulation stalls and is not host CPU time.
This is direct evidence of appreciable runnable scheduling delay in Storage,
not proof of the original ten-client FDB timeout's complete root cause.

A same-session CPU3 / CPU2–3 / CPU3 test changes only the four verified Storage
UpdatePool thread affinities, leaving FDB and other services unchanged. Paired
dd times are27.71/22.42,25.55/29.62,16.01/27.44 seconds. Extra eligible CPUs
provide no stable improvement. In the middle round, copy runnable waits are
2.4293/1.9407 seconds; in the restored round0.1048/2.8631. The middle and final
captures contain204 and124 events, with no loss. CPU2 is shared with Meta; this
is not additional dedicated capacity. The original CPU3 mask is restored and
verified. No diagnostic affinity change is promoted into deployment defaults.

`out/cxl-riscv/investigation-20260908/analyze_scheduler.py` validates receipts,
record counts, loss counters, frame/TID pairing and scheduler state transitions
before summing within-guest intervals. Its outputs are
`session-2c-002/scheduler-analysis-{203,225,233}.json`. Current evidence narrows
slow work to CXL copy plus substantial scheduling/flush delay. The next causal
question is which competing work and shared CXL accesses account for the
runnable gaps and flush wait. Current10c qualification and IO500 standard remain
uncompleted; neither a passing short write nor a CPU-mask experiment closes
that acceptance goal.

The final2-client session has clean application retirement, no collection
errors, three QEMU exit codes0 and simulator exit0. Both final profile exports
are verified. Offline service/FDB recovery completed before nine owned
transient disks were deleted. Reusable caches and failure evidence remain.

## LegoFS comparison: forced exclusive reads are a concrete amplification hypothesis

The current 3FS QEMU builder (`deploy/cxl-riscv/run_g0.py`) hard-codes
`coherence-v2-read-exclusive=on` for node0, including phase1 business runs.
LegoFS documents this exact policy mistake as P01 in
`../legofs/docs/cxl-bi/current-problems-and-directions.md`: ordinary server
reads acquire exclusive ownership, destroying read sharing. Its current runner
keeps this switch only as an explicit negative control. QEMU's
`../qemu/hw/cxl/cxl_type3_memsim_v2.c` selects `load_exclusive` for these reads;
this is not a change to only a checksum operation or a particular service.

3FS's progress engine scans registered sockets under one mutex. Each socket
progress scan caches four shared cursors and recomputes readiness, including
peer owner-record snapshot validation and repeated cursor reads. The C++ default heartbeat is10ms, but the deployed cohort explicitly uses
heartbeat_interval='1s'; its progress polling interval is10ms. These are concrete sources of shared
memory work. Forced ownership can amplify that work; their relative contribution
has not yet been counted by address. No O(N^2) complexity conclusion follows
from the present latency samples.

Relevant LegoFS lessons also include I25--I28:8-byte MMIO/64-byte line misses can
make apparently ordinary memory access expensive; page prefetch increased GETS
and worsened performance despite unchanged integrity. I18 removed serialized
writeback round trips with explicit fence draining. These suggest measuring
protocol work per operation and critical-path serialization, while preserving
ordering and resource lifetime. They do not justify copying LegoFS's protocol,
removing3FS UUID/Waiter checks, extending timeouts, or modifying the simulator.

A matched finite read-policy experiment uses rootfs016, unchanged binaries,
actual two clients, FDBCPU0, StorageCPU3, qdepth8/cell65536, RPC detail tracing
disabled and the same16-event bounded Storage uprobes. Sequence: one64KiB direct
write, two concurrent64KiB direct writes, paired fsync/direct-read/zero comparison,
then unlink. The auxiliary OFF driver changes only node0's read-exclusive flag.
It remains diagnostic, not qualification or standard.

First OFF run `diagnostic-read-shared-2c-001/20260908T090700.598612Z-655715`:
all seven jobs complete with verified producer markers and hashed exports.
Guest dd single-write4.469473s; paired writes6.303365/6.671281s. Host observation
intervals (excluding log export)5.163723s and6.650981/7.028950s respectively.
Storage pull copies1.301138/1.195845/1.225674s; push copies1.186259/1.353263s.
The16/16 kernel events and all loss counters validate. Host scheduled execution
and guest monotonic intervals must not be conflated.

OFF still has authoritative coherence evidence: dirty data completions10608,
SNP_DATA_INV37, protocol/delivery/timeouts0, active bindings0. These are whole-run
counters, including bootstrap/shutdown, not per-I/O deltas. Forbidden TCP data
bytes and RDMA attempts are0. All applications retire cleanly and all three
QEMUs plus simulator exit0. One auxiliary client1 service-log snapshot fails
with `diagnostic chunk missing at 0`; it remains recorded independently. The
finite-job logs and kernel profile exports are verified. Offline service and
FDB recovery completed before nine transient disks were deleted. Current
formal10c acceptance is not passed. The matched ON result is recorded below.

Matched ON run `diagnostic-read-exclusive-2c-001/20260908T091357.802350Z-657965`
completes all seven jobs with16/16 lossless Storage events. Guest dd single
write23.143843s; paired writes30.306471/35.357497s; paired direct reads
13.501622/14.440161s. Corresponding OFF direct reads are2.985194/3.439188s.
The matched single write is5.178x slower with forced ownership, and paired
writes4.808x/5.300x slower. These are per-guest elapsed measurements, not
cross-guest timestamp subtraction. One of two connected clients is active in
the single-write stage; this is not a one-guest topology comparison.

ON Storage pull copies2.496194/4.129496/4.508353s are slower than OFF. ON push
copies0.171744/0.218221s are *faster* than OFF's1.186259/1.353263s, despite
slower end-to-end reads. Thus the evidence does not support claiming every
instruction-level stage improves. Ownership policy redistributes costs across
handoff and data access; end-to-end measurements are required.

Whole-run ON/OFF SNP_INV counts are58515/6952 and UPGRADE58353/6935. ON/OFF
SNP_DATA_INV7806/37; both remain positive. Different total run durations include
different amounts of background polling, so these are not normalized per-I/O
protocol amplification factors. Both runs retain zero protocol/timeouts/
delivery errors, zero forbidden TCP data bytes/RDMA attempts, clean application
retirement, all QEMU exit0 and simulator exit0. ON has no collection errors.
All logs were recovered before nine ON transient disks were removed.

Implementation now defaults `build_qemu_command` to shared reads. G0 explicitly
opts into `server_read_exclusive=True` to retain its original dirty-handoff
proof; phase1 explicitly uses False. No QEMU/backend, timeout, queue geometry,
RPC ABI or validation rule changes. The new configuration regression fails on
the old hard-coded server policy (`read-policy-before.log`); all194 deployment
Python tests pass in15.327s after the change (`python-tests-19.log`).

Closure/build/rootfs017 records the host-runner change. All23 executable hashes
match016, and the rootfs payload cache hits identity
`f85530050213ab012927364cb86cde3b2fbab0883b5f7151acf6d87d8a236079`.
The matching executable G0 handoff is reused; no executable rebuild or new G0
claim is implied. A repeat OFF run uses the actual revised phase1 runner without
a policy monkeypatch. Its result is recorded below. This establishes a concrete
configuration amplification cause; it does not close the original ten-client
FDB timeout, prove quadratic complexity, or satisfy IO500 standard acceptance.


Repeat OFF with revised deployment scripts,
`diagnostic-read-shared-2c-002/20260908T092202.353371Z-664358`, completes all
seven jobs: single write4.495992s, paired writes6.293245/6.197211s, direct reads
3.664574/4.631930s. This gives OFF/ON/OFF single-write4.47/23.14/4.50s and
paired-write6.30,6.67 /30.31,35.36 /6.29,6.20s. The repeat is a deployment
configuration regression check, not a new application executable candidate.
Storage pulls1.282370/1.336635/1.284490s and pushes1.291478/1.377436s again
have16/16 verified events and no loss. No collection errors, clean retirement,
all three QEMU exit0, simulator exit0, forbidden TCP data/RDMA attempts0.
Readback comparison and genuine dirty coherence events remain successful.

Evidence analyzers validate hashes, byte counts, producer completion, per-CPU
loss counters and TID/frame pairing before reporting timings. Driver sources
and hashes are saved with each run. Build017 host manifest refresh15.53s and
rootfs staging9.86s reused the executable and image caches. The post-run text
here is a subsequent evidence update, not part of closure017's earlier source
snapshot. Formal10c qualification and IO500 standard remain outstanding.

Repeat OFF service/FDB offline exports are complete; all processes stopped
before its nine transient disks were removed. Across the matched OFF/ON/OFF
sequence all27 transient disks are cleaned, while snapshots, raw evidence and
reusable caches remain. Repeat OFF whole-run SNP_DATA_INV37 and dirty data
completions10636 are positive, with no coherence protocol/delivery/timeouts.


## Stage-1 resumed: first formal qualification passes; live sampling policy corrected

Rootfs018 refreshes post-run evidence-document identity; its23 executable hashes
remain identical to017. The10-client short probe
`diagnostic-read-shared-10c-001/20260908T093237.939118Z-672339` completes31 jobs,
64/64 lossless kernel events, clean retirement, zero forbidden data TCP/RDMA
and zero coherence protocol/timeouts/delivery errors. It remains diagnostic.

Formal `qualification-read-shared-001/20260908T094036.718868Z-675530` passes the
original validator and independent replay. All ten random2MiB files pass own
and neighbor hashes. CXL bulk read20MiB/write40MiB; forbidden TCP data/RDMA0;
coherence errors/timeouts0; all applications retire cleanly and all11 QEMU plus
simulator exit0. Host total2071.968575s: startup to first job252.785539s,
write/own-read/hash interval union1328.782387s, neighbor-read/hash412.849079s,
last job to runner return77.548441s. Guest write/fsync643--730s, own reads577--649s,
neighbor reads385--412s. Host and guest intervals are not mixed. Original1800s
per-job observation limits remain unchanged. Service/FDB recovery precedes
removal of all33 transient disks.

Timing review found an observation-policy omission: `rpc_trace=False` does
correctly disable detailed RPC trace, but full_stack.execute's default90s live
sampler still wrapped formal qualification. First qualification performed ten
samples/fifty verified sections; their overlapping host intervals total
756.260081s. This is elapsed capture time, not exclusive CPU time, and cannot
be subtracted from business time to claim a speedup. The earlier statement that
qualification had no guest sampling was incorrect. Custom diagnostics and
IO500 standard do not enter this wrapper; the2-client OFF/ON/OFF comparison
was therefore unaffected by it.

Live sampling now defaults to0 (explicit opt-in). The result records both RPC
trace policy and live sampling interval. A deterministic regression proves the
old default enters the diagnostic worker even for normal qualification; the
new default executes the operation directly. Explicit positive-interval
sampling still passes its existing test. All195 deployment tests pass16.385s.
No application executable, ABI, workload, deadline or acceptance gate changes.

Second qualification `qualification-read-shared-002/20260908T101617.504317Z-688080`
also passes with018 and its original sampling policy: zero validation errors,
all own/neighbor hashes correct, clean retirement and all processes stopped. A further qualification with both RPC trace and live sampling disabled
will establish the no-sampling control; then standard still must pass all13
phases and its official verifier. Current live progress and run identities are
recorded in `out/cxl-riscv/investigation-20260908/stage1-resumption.md` and
`stage1-active.json`; first-pass evidence is not yet complete stage-1 acceptance.

### Unsampled qualification and standard staging correction

Qualification-no-live-001/20260908T105525.270619Z-701286 passes the original
validator and independent replay with RPC trace disabled and live interval0.
The dedicated audit verifies zero live commands/files. All ten2MiB writes,
own reads and neighbor hashes pass; guest dd write455.350--488.665s,
own read677.457--740.676s, neighbor315.092--349.297s. Host wall1885.631840s
includes startup, business, export and teardown; guest times are never
subtracted across guests. Bulk read20MiB/write40MiB, RPC7340/7340, forbidden
TCP data/RDMA0. BI dirty completions1061523/SNP_DATA_INV142; protocol errors,
timeouts and delivery failures0; active bindings0. Application retirement is
clean and all11QEMU/simulator exit0. Service/FDB logs were recovered and hashed
before33 transient disks were removed. This completes the additional unsampled
control following the two consecutive18 qualification passes; all23 service
and platform binary identities are unchanged.

Standard-read-shared-001/20260908T112923.627140Z-743346 failed **before any QEMU
or simulator started**: io500.stage_roots assumed opt/io500/etc existed inside
its sparse configuration overlay. The shared immutable base has the payload,
but full_stack.stage_roots intentionally returns only overlay directories.
The existing hostname test incorrectly created the full directory up front.
Changing that fixture to an empty overlay reproduces the exact guest-id
FileNotFoundError (standard-sparse-overlay-before.log). The bounded fix creates
the configuration parent with parents=True/exist_ok=True. No binaries, workload,
ABI, deadline or validator semantics change. Standard acceptance remains pending.


### Standard first-write failure and synchronous bulk scheduling

Rootfs020 standard-read-shared-002/20260908T113259.999038Z-748267 entered the
real ten-rank standard, then failed its first2MiB POSIX write at offset0 with
EIO. No scoring phase completed. FUSE split the request; StorageClient sends
512KiB chunks as separate RPCs. The first chunk timed out at120s; retries hit
ChannelIsLocked while the original ReliableUpdate retained its channel.
Application retirement failed independently of all QEMU processes exiting0.
The original business failure and offline service/FDB logs are preserved.

The bounded2-client size ramp passed64KiB,512KiB,1MiB and paired1MiB writes.
Moving the four UpdatePool threads CPU3 -> CPUs2--3 -> CPU3 did not improve
paired writes consistently (56.48/57.17s ->65.47/65.84s ->62.03/64.31s).
Keep the original affinity. This does not support assigning more FDB cores.

Ten simultaneous512KiB writes in diagnostic-large-write-10c-001/
20260908T121754.390349Z-765354 passed in101.38--111.61 guest seconds, with
40/40 kernel probe events, zero loss, zero forbidden traffic and clean
application retirement. Four synchronous copies occupied all four UpdatePool
threads; each worker immediately entered another copy. Request client UUID
401f79ec-3a07-4d71-ad5b-d245b418db1d/request1/channel1/seq1, owner19,
allocationGeneration1/offset1542413824, finished its copy at server monotonic
312.854321s but logged Processed write request at server realtime345.436966s.
Server clock anchors bound realtime approximately equal to monotonic for this
run; the approximately32.58s gap is on one server, not a cross-guest subtraction.
The last two requests had only0.61s and0.22s gaps. These gaps include storage
work and scheduling, so they are not pure executor queue-latency measurements.

A deterministic ManualExecutor regression fails the old inline algorithm:
copy completes on the RPC executor despite the copy executor not being run.
The baseline includes only an inert executor-injection constructor seam.
The candidate dispatches bulk validation/copy to an independent CPU executor,
owning the fabric and local buffers by value and awaiting actual completion
with cancellation disabled for the copy. It retains fences, counters, wire
ABI, generation checks, RPC timeouts and retry semantics. New tests cover
queued cancellation before/after copy, transfer/local-owner destruction,
stopped fabric and allocation generation reuse. Current15 buffer tests pass.
Original standard failure is not yet root-cause closed: the new candidate
still needs matching build/G0, original10c validation and complete standard.
