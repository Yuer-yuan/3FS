# 3FS CXL phase-1 fault investigation implementation record

The user-supplied plan is the approved design. Execute inline with one agent;
modify only components/3FS, preserve existing work, do not commit or push.

Goal: establish the first failed request transition, fix it with a failing
regression, then pass matching G0-B, two 10c1s qualifications (one without
tracing) and the unmodified 13-stage IO500 standard/verifier.

Constraints: reuse the existing vlm-server QEMU/simulator/kernel/rootfs caches;
FDB remains machine-local TCP, Core/bootstrap remain phase 1. queue_depth=8,
cell_bytes=65536, 300-second stonewall and 600-second phase gates stay fixed.
Do not infer causality from incomplete logs or compare guest clocks directly.

- [ ] Compare closure029/build027/rootfs020, full057 and qualification008;
  preserve manifest hashes and identify missing source snapshots.
- [x] Reproduce literal command newlines and premature serial marker matching
  with /bin/sh in test_serial_diagnostics.py before changing run_g0.py.
- [ ] Export bounded diagnostic snapshots with lengths/hashes and independent
  collection errors; stop additional sampling when the workload finishes.
- [ ] Add bounded dedicated request traces with overflow counters and precise
  service/method, process, lane generation and stream identities.
- [ ] Exercise named minimal requests on 10 connected clients; locate the first
  missing transition and create a deterministic regression for that failure.
- [ ] Fix the evidenced request/retirement failures, keeping semantics and ABI.
- [ ] Run affected native and deployment tests; preserve migrated fixture coverage.
- [ ] Create content-bound source/build/rootfs records and matching G0-B.
- [ ] Pass qualification twice and official standard, retain timing/hash evidence.

Evidence lives under out/cxl-riscv/investigation-20260908. Historical acceptance
is not current acceptance. No new BI workload has been run at record creation.

## User steering: smaller cohorts and FDB core comparison

The user explicitly requests faster localization using1 then2 clients and more
FDB cores. Prioritize `diagnostic-scale` over further ten-client long runs. Keep
the ten-client service configuration independent of active participant count;
compare FDB CPU0 / CPU0–1 / CPU0 on the same cohort, recording actual per-thread
affinity. Increase client count only after the lower-count request stages are
understood. A failed diagnostic stage stops the ramp. Final acceptance remains
unchanged at10c1s, including the original standard workload and deadlines.
