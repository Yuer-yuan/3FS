# CXL transport model

This is a bounded P model of phase-1 transport ownership and request outcomes.
It complements the C++ regression tests and multi-guest DAX evidence; it does
not prove the C++ implementation, CRC algorithm, atomic instruction lowering,
cache coherence, storage persistence or FoundationDB behavior.

The model has two independent directions, depth four, absolute cell sequences,
four bytes per cell, and requests that can share a cell. Acceptance, publication,
partial delivery, execution, response and timeout are separate actions. The
random communication scenarios explore 64 nondeterministic actions followed
by a drain that represents weak fairness for live peers. Each state update has
one logical owner. The ABI's atomic/coherent owner-record publication is an
assumption here and is checked separately by the runtime and DAX tests.

Retirement follows PublicationLedger's conservative boundary: any delivery
past a request's **beginning** makes an unanswered request OutcomeUnknown.
Only a trustworthy retirement with no byte of that request delivered allows
automatic retry. A corrupt snapshot produces LaneRetired without replay.
Crash-after-delivery quarantines the allocation; no timeout grants an owner
fence, release or reuse. Other scenarios explicitly supply a verified owner
retirement fence before advancing the lane/allocation generation.

The allocation abstraction has one 16-byte slot per direction and exports
subranges. It models identity, bounds, accessibility, lease retention and
generation reuse, rather than allocator fragmentation or performance.

The eight monitors check slot overwrite, duplicate consume/execution,
generation isolation, ordered byte watermarks, false rejection, unknown
replay, premature release and eventual drain. Safe scenarios cover ping-pong,
one-way, bidirectional traffic, queue-full backpressure, accepted-before-flush
timeout, a partially delivered shared cell, corrupt delivery evidence,
crash-after-delivery, restart and stale subrange access. Five unsafe scenarios
deliberately violate overwrite, partial-request classification, unknown replay,
lease retention and stale-handle validation, respectively.

Use the repository's pinned P 2.3.2 (see `../.config/dotnet-tools.json`) with
.NET SDK 8.0 and Java 11 or later. The task's private toolchain uses SDK 8.0.408
and records its upstream SHA-512 in `out/cxl-riscv/formal-tools/toolchain.json`.
It does not require a global tool install or shell-profile changes.

```sh
python3 specs/CXLTransport/check.py \
  --toolchain out/cxl-riscv/formal-tools/toolchain.json \
  --output out/cxl-riscv/formal --schedules 1000 --seed 20260905
```

The runner copies the model into a fresh result directory, hashes every input,
compiles it, verifies the unsafe assertion traces, and requires all requested
schedules to complete for every safe case with zero bugs. It uses 2000 steps
per schedule, treats that bound as a failure, and bounds/owns each checker
process group. Result JSON, compiler/checker logs and counterexample traces are
retained even on failure. A successful run is a bounded schedule check, not
an exhaustive proof over unbounded executions.
