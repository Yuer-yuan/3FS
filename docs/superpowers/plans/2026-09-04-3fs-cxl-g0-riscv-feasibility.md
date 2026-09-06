# 3FS CXL G0 RISC-V Feasibility Implementation Plan

> **Execution constraint:** Implement task-by-task in one agent. Do not spawn
> subagents unless the user separately requests them. Steps use checkbox
> (`- [ ]`) syntax for tracking.

**Goal:** First prove the RISC-V compiler/sysroot, guest DAX/FUSE contract and a
compatible RISC-V FoundationDB client/server transaction independently of
today's RDMA-coupled 3FS common library (G0-A), then prove the complete CXL-only
3FS build/start after phase-1 neutralization (G0-B).

**Architecture:** A Python preflight freezes all host tools and parent-platform
artifacts to absolute paths. A standalone smoke CMake project builds DAX and
direct FDB probes without `common` and stages the matching RISC-V `fdbserver`;
after the phase-1 source-closure handoff exists, the same toolchain builds
CXL-enabled/RDMA-disabled 3FS binaries. A 3FS-owned image packer supports both
profiles and feeds the existing QEMU/CXLMemSim platform.

**Tech Stack:** Python 3 standard library and `unittest`, CMake/Ninja, C++20,
GNU or Clang RISC-V cross compiler, Linux devdax, ext4, QEMU `sifive_u`, and
FoundationDB C API 710 with an exact G0-pinned server/client bundle.

**Spec:** `docs/superpowers/specs/2026-09-04-3fs-cxl-transport-design.md`

**Verified checkpoint (2026-09-05):** G0-B now passes with the complete native
RISC-V CXL-only image and two-guest FUSE write/read smoke. The immutable
reference is `out/cxl-riscv/g0-b/g0-b-handoff.json`; the owning phase-1 plan
records the exact result, source/build identities, checksums and proof scope.
This closes application feasibility, not phase-1 distributed acceptance.

## Global Constraints

- Modify files only inside `components/3FS`.
- Run integration work only under `vlm-server:/home/guokc/mypro/gnn-mount-sda1/CXLMemSim-riscv`.
- Consume parent QEMU, Linux and CXLMemSim artifacts by absolute path; do not patch those components.
- Target native 64-bit little-endian RISC-V guest processes and `/dev/dax0.0`.
- Configure `HF3FS_ENABLE_CXL=ON` and `HF3FS_ENABLE_RDMA=OFF`; no output binary may link `libibverbs`.
- Do not install packages, use `sudo`, or silently download a toolchain. Missing tools fail the preflight with an actionable path name.
- Do not use an x86 3FS process as a substitute for a failed RISC-V gate.
- G0-A must not link the current `common` target; G0-B must not run until the phase-1 source-closure handoff validates.
- G0-A requires a protocol-compatible RISC-V `fdbserver` and `libfdb_c`; a
  host-native server or mock transaction cannot satisfy the gate.
- G0-A and G0-B require lock-free 32-bit and 64-bit atomics in the target process; an
  emulated `libatomic` implementation cannot synchronize different guests.
- Do not create commits. Every task ends with an uncommitted review checkpoint until the user explicitly authorizes commits.
- Preserve unrelated dirty changes in the superproject and in every existing component.
- Unless a step explicitly names the superproject root, run its commands from the `components/3FS` checkout root.

---

## File map

**Create:**

- `deploy/cxl-riscv/preflight.py` — resolve and validate tools, artifacts and sysroot inputs.
- `deploy/cxl-riscv/toolchain-riscv64.cmake` — CMake cross-compilation contract.
- `deploy/cxl-riscv/smoke/CMakeLists.txt` — standalone G0-A DAX/FUSE/FDB build, intentionally independent of `common`.
- `deploy/cxl-riscv/build_riscv.py` — generate and run a deterministic CMake/Ninja command.
- `deploy/cxl-riscv/full-deps.json` — G0-B sysroot, external-project and Rust target contract.
- `deploy/cxl-riscv/kernel.fragment` — required guest kernel capabilities owned by 3FS.
- `deploy/cxl-riscv/guest_image.py` — validate a staged rootfs and build a writable ext4 image.
- `deploy/cxl-riscv/run_g0.py` — launch the DAX and process-start feasibility probes and write evidence JSON.
- `deploy/cxl-riscv/sync_to_vlm_server.py` — non-destructive working-tree synchronization.
- `deploy/cxl-riscv/fdb/fdb-riscv.lock.json` — selected upstream FDB source/API and direct-edit identities.
- `deploy/cxl-riscv/fdb/build_fdb_riscv.py` — explicit import or pinned-source RISC-V bundle builder.
- `deploy/cxl-riscv/fdb/verify_fdb_bundle.py` — ELF, dependency, version and provenance verifier.
- `src/tools/cxl_dax_smoke.cc` — direct DAX mapping and publication probe.
- `src/tools/fuse_mount_smoke.cc` — minimal RISC-V FUSE mount/read/unmount probe.
- `src/tools/fdb_client_smoke.cc` — minimal `libfdb_c` network and transaction probe.
- `tests/cxl_riscv/test_preflight.py` — preflight unit tests.
- `tests/cxl_riscv/test_fdb_bundle.py` — FDB lock, import/build and target-ELF tests.
- `tests/cxl_riscv/test_build_riscv.py` — cross-build command tests.
- `tests/cxl_riscv/test_cross_dependency_contract.py` — prevent host artifacts from entering a RISC-V link.
- `tests/cxl_riscv/test_guest_image.py` — kernel/rootfs/image contract tests.
- `tests/cxl_riscv/test_fuse_probe_contract.py` — FUSE source/runtime evidence contract.
- `tests/cxl_riscv/test_g0_evidence.py` — G0 result-schema and gate tests.

**Modify:**

- `CMakeLists.txt` — CXL/RDMA build options and RISC-V architecture checks.
- `cmake/Target.cmake` — directory-scoped source exclusion support.
- `cmake/ApacheArrow.cmake` — propagate the cross toolchain/sysroot and disable unsupported RISC-V SIMD.
- `cmake/Jemalloc.cmake` — out-of-tree RISC-V cross configure.
- `cmake/AddCrate.cmake` — build Rust archives for the RISC-V target triple.
- `src/common/CMakeLists.txt` — conditional ibverbs linkage and source selection.
- `src/tools/CMakeLists.txt` — smoke targets.
- `.gitignore` — ignore only generated G0 build/image/evidence directories.

Because `deploy/cxl-riscv` is an executable-script directory whose name contains
a hyphen, every Python unit test that imports one of its scripts begins with
this explicit import path setup (followed by imports of the modules used by that
test):

```python
DEPLOY_DIR = Path(__file__).parents[2] / "deploy" / "cxl-riscv"
sys.path.insert(0, str(DEPLOY_DIR))
import preflight
```

Do not spell this path as a Python package such as `deploy.cxl_riscv`; that
would name a different directory.

## Task 1: Add the deterministic environment preflight

**Files:**

- Create: `deploy/cxl-riscv/preflight.py`
- Create: `tests/cxl_riscv/test_preflight.py`

**Interfaces:**

- Produces: `Inputs.from_environment(env: Mapping[str, str]) -> Inputs`
- Produces: `validate(inputs: Inputs) -> list[str]`
- Produces: CLI output JSON with `schema_version`, `status`, `resolved`, and `errors`.
- Consumes later: `build_riscv.py`, `guest_image.py`, `run_g0.py` load the emitted JSON rather than resolving tools again.

- [x] **Step 1: Write tests for exact absolute-path and executable validation**

```python
class PreflightTest(unittest.TestCase):
    def test_rejects_missing_cxx_and_relative_sysroot(self):
        inputs = preflight.Inputs(
            project_root=Path("/work/3FS"),
            superproject_root=Path("/work/CXLMemSim-riscv"),
            qemu=Path("/artifacts/qemu-system-riscv64"),
            cxlmemsim_server=Path("/artifacts/cxlmemsim_server"),
            cxlmemsim_topology=Path("/artifacts/topology_simple.txt"),
            opensbi=Path("/artifacts/fw_dynamic.bin"),
            uboot=Path("/artifacts/u-boot.bin"),
            kernel=Path("/artifacts/Image"),
            kernel_config=Path("/artifacts/linux.config"),
            sysroot=Path("relative/sysroot"),
            rootfs=Path("/artifacts/riscv-rootfs"),
            c_compiler=Path("/tools/riscv64-linux-gnu-gcc"),
            cxx_compiler=Path("/tools/riscv64-linux-gnu-g++"),
            cmake=Path("/usr/bin/cmake"),
            ninja=Path("/usr/bin/ninja"),
            fdb_include=Path("/sysroot/usr/include"),
            fdb_client=Path("/sysroot/usr/lib/libfdb_c.so"),
            fdb_server=Path("/sysroot/usr/bin/fdbserver"),
            fuse_include=Path("/sysroot/usr/include/fuse3"),
            fuse_library=Path("/sysroot/usr/lib/libfuse3.so"),
        )
        errors = preflight.validate(inputs, is_executable=lambda p, mode: p.name != "riscv64-linux-gnu-g++")
        self.assertIn("HF3FS_RISCV_SYSROOT must be absolute", errors)
        self.assertIn("HF3FS_RISCV_CXX is not executable", errors)

    def test_accepts_complete_absolute_inputs(self):
        inputs = make_absolute_inputs()
        self.assertEqual(preflight.validate(inputs, is_executable=lambda _p, _mode: True), [])
```

- [x] **Step 2: Run the new test and verify the module is absent**

Run:

```bash
python3 -m unittest -v tests/cxl_riscv/test_preflight.py
```

Expected: FAIL with `ModuleNotFoundError: No module named 'preflight'`.

- [x] **Step 3: Implement the typed input contract and validator**

```python
@dataclasses.dataclass(frozen=True)
class Inputs:
    project_root: pathlib.Path
    superproject_root: pathlib.Path
    qemu: pathlib.Path
    cxlmemsim_server: pathlib.Path
    cxlmemsim_topology: pathlib.Path
    opensbi: pathlib.Path
    uboot: pathlib.Path
    kernel: pathlib.Path
    kernel_config: pathlib.Path
    sysroot: pathlib.Path
    rootfs: pathlib.Path
    c_compiler: pathlib.Path
    cxx_compiler: pathlib.Path
    cmake: pathlib.Path
    ninja: pathlib.Path
    fdb_include: pathlib.Path
    fdb_client: pathlib.Path
    fdb_server: pathlib.Path
    fuse_include: pathlib.Path
    fuse_library: pathlib.Path

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> "Inputs":
        names = {
            "project_root": "HF3FS_SOURCE_DIR",
            "superproject_root": "HF3FS_SUPERPROJECT_ROOT",
            "qemu": "HF3FS_QEMU",
            "cxlmemsim_server": "HF3FS_CXLMEMSIM_SERVER",
            "cxlmemsim_topology": "HF3FS_CXLMEMSIM_TOPOLOGY",
            "opensbi": "HF3FS_RISCV_OPENSBI",
            "uboot": "HF3FS_RISCV_UBOOT",
            "kernel": "HF3FS_RISCV_KERNEL",
            "kernel_config": "HF3FS_RISCV_KERNEL_CONFIG",
            "sysroot": "HF3FS_RISCV_SYSROOT",
            "rootfs": "HF3FS_RISCV_ROOTFS",
            "c_compiler": "HF3FS_RISCV_CC",
            "cxx_compiler": "HF3FS_RISCV_CXX",
            "cmake": "HF3FS_CMAKE",
            "ninja": "HF3FS_NINJA",
            "fdb_include": "HF3FS_RISCV_FDB_INCLUDE",
            "fdb_client": "HF3FS_RISCV_FDB_CLIENT",
            "fdb_server": "HF3FS_RISCV_FDBSERVER",
            "fuse_include": "HF3FS_RISCV_FUSE_INCLUDE",
            "fuse_library": "HF3FS_RISCV_FUSE_LIBRARY",
        }
        missing = [value for value in names.values() if not env.get(value)]
        if missing:
            raise ValueError("missing environment values: " + ", ".join(sorted(missing)))
        return cls(**{field: pathlib.Path(env[name]) for field, name in names.items()})

def validate(inputs: Inputs, is_executable=os.access) -> list[str]:
    errors: list[str] = []
    for field in dataclasses.fields(inputs):
        path = getattr(inputs, field.name)
        if not path.is_absolute():
            errors.append(f"{ENV_BY_FIELD[field.name]} must be absolute")
    for field in ("qemu", "cxlmemsim_server", "c_compiler", "cxx_compiler",
                  "cmake", "ninja", "fdb_server"):
        path = getattr(inputs, field)
        if path.is_absolute() and not is_executable(path, os.X_OK):
            errors.append(f"{ENV_BY_FIELD[field]} is not executable")
    return errors
```

Also require `sysroot`, `rootfs`, `fdb_include` and `fuse_include` to be
directories; require `kernel`, `kernel_config`, `opensbi`, `uboot`, `cxlmemsim_topology`,
`fdb_client`, `fdb_server` and `fuse_library` to be regular files; and verify
both libraries and `fdbserver` are RISC-V ELF. Hash QEMU, CXLMemSim server,
topology, OpenSBI, U-Boot, Linux image and its recorded config into the emitted
preflight record so no later runner resolves or silently substitutes one of
them. Resolve all symlink chains before classification and hashing. Require the
platform artifacts to remain beneath the exact configured superproject root;
require target FDB/FUSE libraries, `fdbserver`, their interpreter and recursive
target dependencies to remain inside the declared sysroot. Run
`fdbserver --version` under the target guest during G0 and reject a
protocol/API-incompatible server/client pair.

The CLI writes JSON even on failure and exits `2` when `errors` is non-empty.

- [x] **Step 4: Run the focused preflight tests**

Run:

```bash
python3 -m unittest -v tests/cxl_riscv/test_preflight.py
```

Expected: all tests PASS.

- [x] **Step 5: Capture the current remote failure evidence without changing the remote host**

Run from the superproject root after the 3FS working tree is staged remotely:

```bash
ssh -o BatchMode=yes vlm-server \
  'cd /home/guokc/mypro/gnn-mount-sda1/CXLMemSim-riscv && python3 components/3FS/deploy/cxl-riscv/preflight.py'
```

Expected before provisioning: exit `2`, with JSON naming the missing RISC-V C++ compiler, sysroot or FDB client. Do not install anything in this step.

- [x] **Step 6: Record the uncommitted checkpoint**

Run:

```bash
git diff --check && git status --short
```

Expected: only the preflight module, its test, and previously approved documentation are new or modified.

## Task 1A: Resolve or build the native RISC-V FoundationDB bundle

**Files:**

- Add: `third_party/foundationdb` — FoundationDB 7.3.63 gitlink at the locked peeled commit.
- Create: `deploy/cxl-riscv/fdb/fdb-riscv.lock.json`
- Create: `deploy/cxl-riscv/fdb/build_fdb_riscv.py`
- Create: `deploy/cxl-riscv/fdb/verify_fdb_bundle.py`
- Modify inside `third_party/foundationdb` only when required by a reproduced RISC-V compiler failure.
- Create: `tests/cxl_riscv/test_fdb_bundle.py`
- Modify: `deploy/cxl-riscv/preflight.py`

**Interfaces:**

- Produces: `verify_bundle(path, lock) -> list[str]` and a canonical
  `fdb-bundle-manifest.json` for `fdbserver`, `libfdb_c.so`, C headers,
  interpreter and complete target dependency closure.
- Produces: `build_fdb_riscv.py --import-bundle DIR` for a supplied native
  bundle, or `--source-dir third_party/foundationdb` for the experimental
  source-port path. The builder never clones or downloads source implicitly.
- Produces only beneath `out/cxl-riscv/fdb/<bundle-id>/`; it never installs a
  host package or edits anything outside the 3FS checkout.
- Gate: server and client library come from the same pinned source version,
  are RISC-V ELF, support C API 710 and complete a real guest transaction.

The audit basis is explicit: `src/fdb/FDB.h` selects API 710; the checked 3FS
development Dockerfiles install 7.3.63; the top-level
`set(FDB_VERSION 7.1.5-ibe)` has no consumer in the current CMake graph. The
official FoundationDB container packaging publishes amd64/aarch64 choices and
rejects other architectures, so no official RISC-V binary is assumed. Select
7.3.63 as the initial port candidate because it matches the checked 3FS
development image, not because upstream claims RISC-V support. Pin annotated
tag object `70a1f603842f8e7d4289cbd5e18441d74042a783` and peeled commit
`5140696da2df47c143ae74c0f4207b65d0d94876`; any version change requires a new
lock and a fresh G0-A result.

- [ ] **Step 1: Test lock and bundle rejection before implementing**

```python
class FdbBundleTest(unittest.TestCase):
    def test_rejects_mixed_server_and_client_source(self):
        bundle = valid_bundle()
        bundle["libfdb_c"]["source_commit"] = "0" * 40
        self.assertIn("FDB server/client source identity differs", verify(bundle))

    def test_rejects_host_or_wrong_api_artifact(self):
        bundle = valid_bundle()
        bundle["fdbserver"]["machine"] = "Advanced Micro Devices X86-64"
        bundle["api_versions"] = [720]
        errors = verify(bundle)
        self.assertIn("fdbserver is not RISC-V", errors)
        self.assertIn("FoundationDB C API 710 is unsupported", errors)

    def test_rejects_unrecorded_source_modification_or_dependency(self):
        bundle = valid_bundle()
        bundle["source_modifications"].append(
            {"path": "unknown.cpp", "sha256": "0" * 64}
        )
        bundle["fdbserver"]["needed"].append("/usr/lib/x86_64-linux-gnu/libstdc++.so.6")
        errors = verify(bundle)
        self.assertIn("FDB source modification is not lock-pinned", errors)
        self.assertIn("FDB target dependency escapes the RISC-V sysroot", errors)
```

Run `python3 -m unittest -v tests/cxl_riscv/test_fdb_bundle.py`.

Expected: FAIL because the verifier does not exist.

- [ ] **Step 2: Implement provenance-complete import mode first**

An externally supplied bundle is the lower-risk path, but it is trusted only
after verification. Require regular files, no symlink escape, hashes for all
contents, `readelf -h/-l/-d` RISC-V identity, target interpreter existence,
recursive NEEDED resolution inside the declared RISC-V sysroot, server/client
source version equality and API 710 headers. Copy verified bytes into a new
bundle directory and atomically write its manifest; never execute the supplied
binary on the x86 host.

Boot a disposable RISC-V guest to run `fdbserver --version`, load
`libfdb_c.so`, select API 710 and perform set/commit/readback through
`fdb_client_smoke`. Import succeeds only after this test; a filename or package
label is not version evidence.

- [ ] **Step 3: Add the pinned-source port experiment when import is unavailable**

Use the checked-in `third_party/foundationdb` gitlink as the only source. Require
its `HEAD`, annotated tag object and peeled commit to match the lock before
configuring. Require the complete changed-path set to equal
`source_modifications`, reject untracked paths, and verify each modified file's
post-edit SHA-256. Record the compiler/sysroot, CMake/Ninja, Mono and every
dependency hash. Generated build state stays under ignored
`out/cxl-riscv/fdb/`; there is no source-download fallback.

Separate build-machine tools from target artifacts. Run the C# actor compiler
and other generators on the x86 build host, but compile/link `fdbserver` and
`fdb_c` only with the RISC-V toolchain/sysroot. Disable Swift, Java, Python,
documentation, packaging and unrelated tests; build only the two required
targets and their libraries. Never allow a CMake search mode of `BOTH` for
target headers or libraries. If a configure probe must execute target code,
declare the exact QEMU-user emulator or preseed a measured cache entry; do not
silently accept a host result.

If upstream cannot separate host generators during cross-compilation, the
bounded fallback is a native build inside a disposable, enlarged RISC-V QEMU
build guest using the same pinned source and dependency manifests. Record this
as `build_mode=native-riscv-qemu` and keep it out of timed results. Do not
replace it with qemu-user execution of x86 FDB inside the test guest.

- [ ] **Step 4: Make every direct source edit auditable and minimal**

Do not create speculative edits. On each reproduced failure, first preserve the
compiler/linker log; then edit the corresponding tracked file directly inside
`third_party/foundationdb` and add its path plus full post-edit SHA-256 to the
lock. The builder requires exactly that dirty tracked-file set and rejects all
untracked or undeclared changes. Architecture changes may add RISC-V
selection/fences, but must not weaken transaction, checksum, TLS or protocol
behavior to make the smoke pass.

- [ ] **Step 5: Verify the final bundle in both build and guest environments**

Run:

```bash
python3 deploy/cxl-riscv/fdb/verify_fdb_bundle.py \
  --lock deploy/cxl-riscv/fdb/fdb-riscv.lock.json \
  --bundle out/cxl-riscv/fdb/selected/fdb-bundle-manifest.json
python3 -m unittest -v tests/cxl_riscv/test_fdb_bundle.py
```

Then feed the emitted include/library/server paths back into Task 1 preflight
and run the Task 6 guest transaction. Expected: exact RISC-V server/client
identity, API 710, no host dependency and committed readback.

- [ ] **Step 6: Stop cleanly if the port is not feasible**

If neither a verified imported bundle nor the pinned port produces both native
artifacts, write a failed `fdb-port-result.json` with the first unsupported
compiler/runtime boundary and stop G0-A. Do not begin phase-1 implementation,
run an x86 server, use a memory KV substitute or claim a partial migration.

- [ ] **Step 7: Record the uncommitted checkpoint**

Run `git diff --check && git status --short`. Only the lock, build/verifier,
tests, the FoundationDB gitlink and evidence-backed direct edits in that nested
checkout are source changes; all built FoundationDB bytes are ignored generated
output.

## Task 2: Make CMake and external projects express CXL-only RISC-V builds

**Files:**

- Modify: `CMakeLists.txt:78-110`
- Modify: `cmake/Target.cmake:13-38`
- Modify: `src/common/CMakeLists.txt:1-11`
- Modify: `tests/common/CMakeLists.txt`
- Modify: `cmake/ApacheArrow.cmake`
- Modify: `cmake/Jemalloc.cmake`
- Modify: `cmake/AddCrate.cmake`
- Create: `deploy/cxl-riscv/full-deps.json`
- Create: `tests/cxl_riscv/test_cross_dependency_contract.py`
- Create: `tests/cxl_riscv/test_cmake_contract.py`

**Interfaces:**

- Produces: CMake options `HF3FS_ENABLE_CXL`, `HF3FS_ENABLE_RDMA`, and `HF3FS_ENABLE_CXL_NETD`.
- Produces: directory variable `HF3FS_EXCLUDE_SOURCE_REGEX` consumed by
  `target_add_lib`, `target_add_shared_lib` and `target_add_test`.
- Produces: compile definition `HF3FS_ARCH_RISCV64=1` for RISC-V.
- Produces: target-triple-aware Arrow, jemalloc and Rust build commands; no
  ExternalProject/custom command may fall back to the host compiler.
- Consumes later: every CXL source uses `HF3FS_ENABLE_CXL`; IB source is absent when RDMA is disabled.

- [ ] **Step 1: Write a source-contract test for the new options and conditional ibverbs linkage**

```python
class CMakeContractTest(unittest.TestCase):
    def test_cxl_only_contract_is_declared(self):
        top = Path("CMakeLists.txt").read_text()
        common = Path("src/common/CMakeLists.txt").read_text()
        target = Path("cmake/Target.cmake").read_text()
        self.assertIn("option(HF3FS_ENABLE_CXL", top)
        self.assertIn("option(HF3FS_ENABLE_RDMA", top)
        self.assertIn('MATCHES "riscv64"', top)
        self.assertIn("if(HF3FS_ENABLE_RDMA)", common)
        self.assertIn("HF3FS_EXCLUDE_SOURCE_REGEX", target)
        self.assertEqual(target.count("foreach(source_regex IN LISTS HF3FS_EXCLUDE_SOURCE_REGEX)"), 3)
```

- [ ] **Step 2: Run the contract test and observe the missing options**

Run `python3 -m unittest -v tests/cxl_riscv/test_cmake_contract.py`.

Expected: FAIL because the option strings and source filter are absent.

- [ ] **Step 3: Add build options and the RISC-V architecture branch**

Add near the existing project options:

```cmake
option(HF3FS_ENABLE_CXL "Build the CXL shared-region transport" ON)
option(HF3FS_ENABLE_RDMA "Build the verbs/RDMA transport" ON)
option(HF3FS_ENABLE_CXL_NETD "Build the CXL TUN carrier" OFF)

if(NOT HF3FS_ENABLE_CXL AND NOT HF3FS_ENABLE_RDMA)
    message(FATAL_ERROR "At least one data-plane transport must be enabled")
endif()
```

Extend the architecture branch:

```cmake
elseif(CMAKE_SYSTEM_PROCESSOR MATCHES "riscv64")
    add_compile_definitions(HF3FS_ARCH_RISCV64=1)
    add_link_options(-latomic)
    message(STATUS "RISC-V 64 architecture detected")
```

- [ ] **Step 4: Add directory-scoped source filtering to library and test macros**

Immediately after each `file(GLOB_RECURSE ...)` in `target_add_lib`,
`target_add_shared_lib` and `target_add_test`, add:

```cmake
foreach(source_regex IN LISTS HF3FS_EXCLUDE_SOURCE_REGEX)
    list(FILTER FILES EXCLUDE REGEX "${source_regex}")
endforeach()
```

In `src/common/CMakeLists.txt`, construct transport libraries and exclusions:

```cmake
set(COMMON_TRANSPORT_LIBS)
set(HF3FS_EXCLUDE_SOURCE_REGEX)
if(HF3FS_ENABLE_RDMA)
    list(APPEND COMMON_TRANSPORT_LIBS ibverbs)
else()
    list(APPEND HF3FS_EXCLUDE_SOURCE_REGEX "^net/ib/" "^net/RDMAControl\\.cc$")
endif()
if(NOT HF3FS_ENABLE_CXL)
    list(APPEND HF3FS_EXCLUDE_SOURCE_REGEX "^net/cxl/")
endif()
```

Pass `${COMMON_TRANSPORT_LIBS}` to both common targets instead of unconditional
`ibverbs`.

In `tests/common/CMakeLists.txt`, exclude `^net/ib/` when RDMA is disabled.
After Task 6 of the phase-1 plan removes `TestRDMAControl.cc`, no second legacy
test exclusion is needed. This keeps CXL-only tests free of verbs headers while
the RDMA-enabled source-audit build still compiles the IB tests.

- [ ] **Step 5: Run the contract test and a host configure**

Run:

```bash
python3 -m unittest -v tests/cxl_riscv/test_cmake_contract.py
cmake -S . -B build/cxl-g0-host -G Ninja \
  -DSHUFFLE_METHOD=stdshuffle \
  -DHF3FS_ENABLE_CXL=ON \
  -DHF3FS_ENABLE_RDMA=ON
```

Expected: contract tests PASS and CMake generation succeeds after nested submodules and host dependencies are present.

- [ ] **Step 6: Add and test the full cross-dependency contract**

`full-deps.json` requires RISC-V sysroot artifacts for static Boost filesystem,
system and program_options; libuv; libnuma; libfuse3; `libfdb_c`; OpenSSL
ssl/crypto; libevent; gflags; glog; double-conversion; lz4; lzma; libdwarf;
libunwind; libaio; zlib; libatomic; the C/C++ runtimes; pthread, dl and rt. This
list is derived from the top-level CMake plus the repository's documented build
dependencies and is a minimum, not a host-library allowlist. It also requires
Rust target `riscv64gc-unknown-linux-gnu`, an offline Cargo registry/cache
matching `Cargo.lock`, and pinned Arrow/jemalloc source identities. Every
resolved library path must stay under the sysroot and report RISC-V ELF.

When `CMAKE_CROSSCOMPILING` is true:

- pass the main toolchain file, sysroot, C/C++ compiler and `ARROW_SIMD_LEVEL=NONE`
  to Arrow's configure command;
- use a pre-provisioned Arrow source tree and third-party archive cache whose
  commit/file hashes are in the dependency evidence; the cross branch has no
  `GIT_REPOSITORY`, `download_dependencies.sh` or network fallback;
- configure jemalloc out of tree with `--host=riscv64-linux-gnu`, explicit
  `CC`, `AR` and `RANLIB`;
- run Cargo with `--locked --offline --target
  riscv64gc-unknown-linux-gnu` and read archives from
  `target/riscv64gc-unknown-linux-gnu/release`;
- fail if `readelf -h` reports anything except RISC-V for a produced static or
  shared object.

Run:

```bash
python3 -m unittest -v tests/cxl_riscv/test_cross_dependency_contract.py
```

Expected: tests PASS and inspect exact argv arrays for all three external build
paths, reject a host-ELF dependency and reject any cross-build download token.
The native host branch remains byte-for-byte equivalent to its previous
commands.

- [ ] **Step 7: Record the uncommitted checkpoint**

Run `git diff --check && git status --short` and verify no file outside the listed CMake/test paths changed.

## Task 3: Add the cross-toolchain file and deterministic build driver

**Files:**

- Create: `deploy/cxl-riscv/toolchain-riscv64.cmake`
- Create: `deploy/cxl-riscv/smoke/CMakeLists.txt`
- Create: `deploy/cxl-riscv/build_riscv.py`
- Create: `tests/cxl_riscv/test_build_riscv.py`

**Interfaces:**

- Produces: `build_command(inputs: Inputs, build_dir: Path, jobs: int, profile: str, closure: Mapping[str, object] | None = None, cmake_options: Mapping[str, str] | None = None, extra_targets: Sequence[str] = ()) -> list[str]` for profiles `platform-smoke` and `full-3fs`.
- Produces: `build-manifest.json` inside the selected absolute build directory.
- Consumes: preflight JSON from Task 1 and CMake options from Task 2.
- G0-B `full-3fs` additionally consumes a passing phase-1 source-closure handoff JSON.
- G0-B consumes and validates `deploy/cxl-riscv/full-deps.json` before CMake configure.

The CLI option is named `--source-closure` and accepts schema
`hf3fs.cxl-source-closure.v1`; G0-B requires `gate=phase1`, while later phase-2
builds use the same driver with `gate=phase2`.

- [ ] **Step 1: Test the exact CMake argument contract**

```python
def test_full_build_command_is_cxl_only_and_uses_sysroot(self):
    command = build_riscv.build_command(
        make_inputs(), Path("/out/build"), 8, "full-3fs",
        closure=passing_phase1_closure(),
    )
    self.assertEqual(command[0], "/usr/bin/cmake")
    self.assertIn("-DHF3FS_ENABLE_CXL=ON", command)
    self.assertIn("-DHF3FS_ENABLE_RDMA=OFF", command)
    self.assertIn("-DCMAKE_TOOLCHAIN_FILE=/work/3FS/deploy/cxl-riscv/toolchain-riscv64.cmake", command)
    self.assertIn("-DHF3FS_RISCV_SYSROOT=/sysroot", command)

def test_platform_smoke_does_not_configure_main_project(self):
    command = build_riscv.build_command(
        make_inputs(), Path("/out/smoke"), 2, "platform-smoke"
    )
    self.assertIn("/work/3FS/deploy/cxl-riscv/smoke", command)
    self.assertNotIn("-DHF3FS_ENABLE_RDMA=OFF", command)

def test_full_profile_rejects_missing_phase1_closure(self):
    with self.assertRaisesRegex(ValueError, "phase-1 source closure"):
        build_riscv.build_command(make_inputs(), Path("/out/full"), 8, "full-3fs")
```

- [ ] **Step 2: Verify the test fails before the build driver exists**

Run `python3 -m unittest -v tests/cxl_riscv/test_build_riscv.py`.

Expected: FAIL with an import error.

- [ ] **Step 3: Add the cross-toolchain definition**

```cmake
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR riscv64)

if(NOT IS_ABSOLUTE "${HF3FS_RISCV_SYSROOT}")
    message(FATAL_ERROR "HF3FS_RISCV_SYSROOT must be absolute")
endif()

set(CMAKE_SYSROOT "${HF3FS_RISCV_SYSROOT}")
set(CMAKE_C_COMPILER "$ENV{HF3FS_RISCV_CC}")
set(CMAKE_CXX_COMPILER "$ENV{HF3FS_RISCV_CXX}")
set(CMAKE_FIND_ROOT_PATH "${HF3FS_RISCV_SYSROOT}")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
set(CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY)
```

- [ ] **Step 4: Implement the build driver without shell command construction**

`build_command` returns argv lists, uses `subprocess.run(..., check=True)` and
rejects non-positive jobs. For `platform-smoke`, configure
`deploy/cxl-riscv/smoke` and build only `cxl_dax_smoke`, `fuse_mount_smoke`
and `fdb_client_smoke`; that CMake file uses the standard C++ runtime, threads and
the preflight's absolute FoundationDB include/library paths and never calls
`add_subdirectory` on the main project.

The CLI accepts repeated `--cmake-option NAME=VALUE` and `--extra-target NAME`
only in `full-3fs`. Validate names against `[A-Z0-9_]+` and
`[A-Za-z0-9_.+-]+` respectively, reject attempts to override the toolchain,
sysroot, source directory, CXL or RDMA settings, and append targets as distinct
argv elements.

For `full-3fs`, verify the source-closure handoff hashes, configure the project
root with CXL on/RDMA off, validate every declared sysroot library and Rust
target, and invoke this second command:

```python
[
    str(inputs.cmake), "--build", str(build_dir),
    "--parallel", str(jobs),
    "--target", "mgmtd_main", "meta_main", "storage_main",
    "hf3fs_fuse_main", "cxl-fabricd", "cxl_dax_smoke",
    "fuse_mount_smoke", "fdb_client_smoke",
]
```

The manifest records the preflight JSON hash, exact argv arrays, compiler
`--version`, sysroot path, the content-complete source snapshot/remote receipt
hash (including untracked files), and output ELF paths.

- [ ] **Step 5: Run unit tests and a configure-only dry run**

Run:

```bash
python3 -m unittest -v tests/cxl_riscv/test_build_riscv.py
python3 deploy/cxl-riscv/build_riscv.py --preflight /tmp/hf3fs-preflight.json \
  --profile platform-smoke --build-dir /absolute/output/build --jobs 8 --print-only
```

Expected: tests PASS; print-only emits two JSON argv arrays and does not create a build directory.

- [ ] **Step 6: Record the uncommitted checkpoint**

Run `git diff --check && git status --short`.

## Task 4: Define the guest kernel and rootfs contract

**Files:**

- Create: `deploy/cxl-riscv/kernel.fragment`
- Create: `deploy/cxl-riscv/guest_image.py`
- Create: `tests/cxl_riscv/test_guest_image.py`
- Modify: `.gitignore`

**Interfaces:**

- Produces: `required_kernel_options() -> dict[str, str]`.
- Produces: `validate_rootfs(root: Path, profile: str) -> list[str]` for `platform-smoke` and `full-3fs`.
- Produces: an ext4 image and `guest-image-manifest.json` from an existing RISC-V rootfs directory.

- [ ] **Step 1: Test the exact kernel and rootfs requirements**

```python
def test_kernel_fragment_requires_dax_fuse_tun_and_storage():
    options = guest_image.required_kernel_options()
    self.assertEqual(options["CONFIG_CXL_MEM"], "y")
    self.assertEqual(options["CONFIG_CXL_REGION"], "y")
    self.assertEqual(options["CONFIG_DEV_DAX"], "y")
    self.assertEqual(options["CONFIG_DEV_DAX_CXL"], "y")
    self.assertEqual(options["CONFIG_DEV_DAX_KMEM"], "n")
    self.assertEqual(options["CONFIG_FUSE_FS"], "y")
    self.assertEqual(options["CONFIG_TUN"], "y")
    self.assertEqual(options["CONFIG_VIRTIO_BLK"], "y")
    self.assertEqual(options["CONFIG_EXT4_FS"], "y")

def test_rootfs_rejects_wrong_elf_machine():
    errors = guest_image.validate_elf_machine(
        {"/opt/3fs/bin/storage_main": "Advanced Micro Devices X86-64"}
    )
    self.assertEqual(errors, ["/opt/3fs/bin/storage_main is not RISC-V"])
```

- [ ] **Step 2: Run the tests and observe the missing module**

Run `python3 -m unittest -v tests/cxl_riscv/test_guest_image.py`.

Expected: FAIL with an import error.

- [ ] **Step 3: Add the kernel fragment**

```text
CONFIG_64BIT=y
CONFIG_RISCV=y
CONFIG_CXL_BUS=y
CONFIG_CXL_PCI=y
CONFIG_CXL_ACPI=y
CONFIG_CXL_MEM=y
CONFIG_CXL_PORT=y
CONFIG_CXL_REGION=y
CONFIG_CXL_CACHE=y
CONFIG_CXL_TYPE2_ACCEL=y
CONFIG_ZONE_DEVICE=y
CONFIG_DAX=y
CONFIG_FS_DAX=y
CONFIG_DEV_DAX=y
CONFIG_DEV_DAX_CXL=y
# CONFIG_DEV_DAX_KMEM is not set
CONFIG_FUSE_FS=y
CONFIG_TUN=y
CONFIG_NET=y
CONFIG_INET=y
CONFIG_VIRTIO_PCI=y
CONFIG_VIRTIO_BLK=y
CONFIG_VIRTIO_NET=y
CONFIG_EXT4_FS=y
CONFIG_DEVTMPFS=y
CONFIG_DEVTMPFS_MOUNT=y
CONFIG_PROC_FS=y
CONFIG_SYSFS=y
CONFIG_TMPFS=y
```

Treat this fragment as a validator for the prebuilt parent kernel, not
authorization to edit or rebuild Linux outside `components/3FS`. Parse the
kernel's recorded `.config` and fail G0 with the exact missing/mismatched key;
in particular, `CONFIG_DEV_DAX_KMEM` must remain disabled so the CXL region is
bound to device-dax.

- [ ] **Step 4: Implement rootfs validation and image creation**

The validator uses profile-specific RISC-V ELF requirements:

```python
PLATFORM_SMOKE_BINARIES = (
    "/opt/3fs/bin/cxl_dax_smoke",
    "/opt/3fs/bin/fuse_mount_smoke",
    "/opt/3fs/bin/fdb_client_smoke",
    "/opt/foundationdb/bin/fdbserver",
)
FULL_3FS_BINARIES = PLATFORM_SMOKE_BINARIES + (
    "/opt/3fs/bin/mgmtd_main",
    "/opt/3fs/bin/meta_main",
    "/opt/3fs/bin/storage_main",
    "/opt/3fs/bin/hf3fs_fuse_main",
    "/opt/3fs/bin/cxl-fabricd",
)
```

It runs `readelf -h` and rejects any machine other than `RISC-V`. It parses
`readelf -d` and rejects `libibverbs`. It installs the exact preflight-pinned
`fdbserver`, records its version and dependency closure, and never substitutes
a host binary. The `full-3fs` profile additionally requires a passing phase-1
source-closure handoff with matching binary hashes.
Image creation uses argv-form subprocess
calls with the already validated values:

```python
temporary = output.with_suffix(output.suffix + ".tmp")
commands = [
    (str(truncate), "-s", str(image_bytes), str(temporary)),
    (str(mkfs_ext4), "-F", "-d", str(rootfs), str(temporary)),
    (str(e2fsck), "-fn", str(temporary)),
]
for argv in commands:
    subprocess.run(argv, check=True)
temporary.replace(output)
```

The rename occurs only after all three commands succeed.
The script refuses an existing output unless `--replace-generated` is supplied
and the sibling manifest proves the file was generated by this script.

- [ ] **Step 5: Ignore only generated paths and run tests**

Add:

```gitignore
/build/cxl-riscv/
/out/cxl-riscv/
```

Run `python3 -m unittest -v tests/cxl_riscv/test_guest_image.py`.

Expected: all tests PASS.

- [ ] **Step 6: Record the uncommitted checkpoint**

Run `git diff --check && git status --short`.

## Task 5: Add DAX mapping and FUSE mount smoke binaries

**Files:**

- Create: `src/tools/cxl_dax_smoke.cc`
- Create: `src/tools/fuse_mount_smoke.cc`
- Modify: `deploy/cxl-riscv/smoke/CMakeLists.txt`
- Modify: `src/tools/CMakeLists.txt`
- Create: `tests/cxl_riscv/test_dax_smoke_cli.py`
- Create: `tests/cxl_riscv/test_fuse_probe_contract.py`

**Interfaces:**

- Produces: `cxl_dax_smoke --device PATH --offset N --length N --role writer|reader --generation N`.
- Produces: one JSON line with mapping, generation, checksum and status.
- Produces: `fuse_mount_smoke --mountpoint PATH`, which mounts a minimal
  in-process filesystem, validates one lookup/read and unmounts cleanly.

- [ ] **Step 1: Test CLI rejection and JSON schema**

```python
def test_rejects_unaligned_regular_file_range(self):
    result = run_smoke("--device", self.backing, "--offset", "1", "--length", "4096",
                       "--role", "writer", "--generation", "7")
    self.assertEqual(result.returncode, 2)
    self.assertIn("offset must satisfy mapping alignment", result.stderr)

def test_file_backed_writer_emits_checksum(self):
    result = run_smoke("--device", self.backing, "--offset", "0", "--length", "4096",
                       "--role", "writer", "--generation", "7")
    record = json.loads(result.stdout)
    self.assertEqual(record["status"], "passed")
    self.assertEqual(record["generation"], 7)
    self.assertEqual(record["bytes"], 4096)
    self.assertTrue(record["atomic_u32_lock_free"])
    self.assertTrue(record["atomic_u64_lock_free"])

def test_fuse_probe_requires_real_mount_and_read_markers(self):
    source = Path("src/tools/fuse_mount_smoke.cc").read_text()
    for symbol in ("fuse_session_mount", "fuse_session_loop", "fuse_session_unmount"):
        self.assertIn(symbol, source)
```

- [ ] **Step 2: Run the CLI test before adding the target**

Run:

```bash
python3 -m unittest -v \
  tests/cxl_riscv/test_dax_smoke_cli.py \
  tests/cxl_riscv/test_fuse_probe_contract.py
```

Expected: FAIL because the DAX executable and FUSE source do not exist.

- [ ] **Step 3: Implement bounded mmap and release/acquire publication**

For a regular-file host test, the binary validates `fstat` size, system page
alignment and overflow. For a character device, it resolves the devdax name
through sysfs, reads and records `/sys/class/dax/<name>/size` and `align`, and
requires the offset, mapping base and mapped length to satisfy the stricter of
the system page and device alignment. It never treats a devdax character
device's `st_size` as capacity. It maps only the checked range with
`MAP_SHARED` and uses this 64-byte header:

```cpp
struct alignas(64) SmokeHeader {
  uint64_t magic;
  uint64_t generation;
  uint64_t length;
  uint64_t checksum;
  uint32_t atomic32Probe;
  uint32_t reserved0;
  uint64_t reserved[2];
  uint64_t published;
};
static_assert(sizeof(SmokeHeader) == 64);
```

The writer fills a deterministic `byte(i, generation)` payload, writes the
header, then release-stores `published = generation`. The reader
acquire-loads `published`, validates every field and recomputes the checksum.
No virtual pointer is stored in the mapping.

Compile-time assert both `std::atomic_ref<uint32_t>::is_always_lock_free` and
`std::atomic_ref<uint64_t>::is_always_lock_free`, then call `is_lock_free()` on
the aligned mapped `atomic32Probe` and `published` words and emit both
`atomic_u32_lock_free=true` and `atomic_u64_lock_free=true`. A false value fails
both writer and reader before publication; `libatomic` fallback is not accepted
for inter-guest state.

Implement the FUSE probe with the low-level FUSE3 API. It exposes a root and a
read-only `sentinel` file containing `riscv-fuse-ok`, starts the session loop on
a joined thread, opens and verifies that file through the mounted path, then
unmounts and destroys the session. A passing JSON record requires the kernel
request handlers to have observed lookup, open and read; merely opening
`/dev/fuse` is not sufficient.

- [ ] **Step 4: Register standalone and full-project targets, then run host tests**

The standalone smoke CMake file uses `add_executable` directly and links only
the C++ runtime and `Threads::Threads` for DAX, and the preflight-validated
FUSE3 library for the FUSE probe; it does not link `common`. The host-only DAX
test sets `HF3FS_BUILD_FUSE_SMOKE=OFF`. Also add both sources to the full
project for G0-B:

```cmake
target_add_bin(cxl_dax_smoke "cxl_dax_smoke.cc" common)
target_add_bin(fuse_mount_smoke "fuse_mount_smoke.cc" common fuse3)
```

Run:

```bash
cmake -S deploy/cxl-riscv/smoke -B build/cxl-g0-smoke -G Ninja \
-DHF3FS_SMOKE_SOURCE_DIR="$PWD/src/tools" \
  -DHF3FS_BUILD_FDB_SMOKE=OFF -DHF3FS_BUILD_FUSE_SMOKE=OFF
cmake --build build/cxl-g0-smoke --target cxl_dax_smoke
HF3FS_DAX_SMOKE=build/cxl-g0-smoke/bin/cxl_dax_smoke \
  python3 -m unittest -v tests/cxl_riscv/test_dax_smoke_cli.py
python3 -m unittest -v tests/cxl_riscv/test_fuse_probe_contract.py
```

Expected: all tests PASS.

- [ ] **Step 5: Prove inter-guest DAX publication through CXLMemSim**

`run_g0.py` starts one strict coherence-v2 CXLMemSim instance and two
overlapping RISC-V QEMU guests with distinct host IDs and distinct sparse
Type-3 backing paths/inodes. Poison the two backing files differently first so
a shared host file cannot create a false pass. Through the serial consoles,
run writer on guest A and reader on guest B, then reverse the roles with a new
generation:

```text
/opt/3fs/bin/cxl_dax_smoke --device /dev/dax0.0 --offset 0 \
  --length RUNNER_SELECTED_DAX_ALIGNED_LENGTH --role writer --generation 1
/opt/3fs/bin/cxl_dax_smoke --device /dev/dax0.0 --offset 0 \
  --length RUNNER_SELECTED_DAX_ALIGNED_LENGTH --role reader --generation 1
/opt/3fs/bin/cxl_dax_smoke --device /dev/dax0.0 --offset 0 \
  --length RUNNER_SELECTED_DAX_ALIGNED_LENGTH --role writer --generation 2
/opt/3fs/bin/cxl_dax_smoke --device /dev/dax0.0 --offset 0 \
  --length RUNNER_SELECTED_DAX_ALIGNED_LENGTH --role reader --generation 2
/opt/3fs/bin/fuse_mount_smoke --mountpoint /tmp/hf3fs-g0-fuse
```

The runner assigns each command to the appropriate guest, checks both QEMU
lifetimes overlap, and correlates the DPA range with positive QEMU/CXLMemSim
GETS/GETX, invalidation/writeback or equivalent authoritative coherence-v2
events. Both readers must reproduce the peer's generation/checksum, and all
four records must say both `atomic_u32_lock_free=true` and
`atomic_u64_lock_free=true`. A same-inode mapping,
host-only memcpy, missing trace correlation or one-way-only success fails G0-A.
Before rendering these commands, the runner reads each guest's devdax sysfs
`size` and `align`, requires both guests to expose identical geometry, and
chooses a nonzero test length that is an exact multiple of that alignment and
fits the common DPA interval. The uppercase token above denotes the decimal
argv value substituted by the runner; it is not an ambient guest variable.
The FUSE record also has `lookup_seen`, `open_seen`, `read_seen` and `unmounted`
set to true.

Generate QEMU argv from a frozen 3FS-owned platform profile matching the
parent's proven RISC-V BI path: `sifive_u`, CXL FMW with restrictions `0x29`,
HDM passthrough, `x-256b-flit=on`, persistent Type-3 memory,
`coherence-v2=on`, `hdm-db=on`, a unique `coherence-v2-host-id`, 64-byte CPU
cache-block operations, and explicitly recorded cache/ways/timeout,
write-through and read-exclusive values. Generate the CXLMemSim argv with
coherence-v2, a trace path, SSD-stream authoritative backing and explicit
capacity/latency/page/chunk/cache/read-ahead/io_uring/O_DIRECT values. Record
the normalized argv and hashes of both endpoint backing files, the central SSD
backing file and topology. Missing, defaulted or unexpected CXL properties
fail the gate; do not silently inherit a parent script's changing defaults.

- [ ] **Step 6: Record the uncommitted checkpoint**

Run `git diff --check && git status --short`.

## Task 6: Prove the RISC-V FoundationDB client/server boundary

**Files:**

- Create: `src/tools/fdb_client_smoke.cc`
- Modify: `deploy/cxl-riscv/smoke/CMakeLists.txt`
- Modify: `src/tools/CMakeLists.txt`
- Create: `tests/cxl_riscv/test_fdb_probe_contract.py`

**Interfaces:**

- Produces: `fdb_client_smoke --cluster-file PATH --key KEY --value VALUE`.
- Produces: JSON fields `api_version`, `set_ok`, `get_ok`, `value_match`, and `status`.
- Consumes: FoundationDB C API 710 from the exact Task 1A-pinned server/client
  bundle; the unused `FDB_VERSION=7.1.5-ibe` CMake variable is not treated as
  artifact identity.
- Consumes: the preflight-pinned RISC-V `fdbserver`; the evidence records both
  server and client library versions and rejects protocol incompatibility.

- [ ] **Step 1: Add a source-contract test for a real set/get transaction**

```python
def test_probe_uses_network_thread_commit_and_readback(self):
    source = Path("src/tools/fdb_client_smoke.cc").read_text()
    for symbol in (
        "fdb_select_api_version", "fdb_setup_network", "fdb_run_network",
        "fdb_transaction_commit", "fdb_transaction_get",
    ):
        self.assertIn(symbol, source)
```

- [ ] **Step 2: Run the contract test before creating the source**

Run `python3 -m unittest -v tests/cxl_riscv/test_fdb_probe_contract.py`.

Expected: FAIL with `FileNotFoundError`.

- [ ] **Step 3: Implement the minimal transaction probe**

Use `FDB_API_VERSION` from the project headers, start the network thread, create
the database from the supplied cluster file, commit one key/value, read it in a
new transaction, compare bytes, destroy all FDB handles, stop the network and
join the thread. Every FDB future is checked with `fdb_future_block_until_ready`
and `fdb_future_get_error`; no error is converted into success.

The key is prefixed with `\xff\x02/hf3fs-cxl-g0/` plus a supplied run ID so the probe does not collide with 3FS metadata.

- [ ] **Step 4: Register standalone and full-project targets**

In the standalone project, define an imported library at the preflight's
absolute `HF3FS_RISCV_FDB_CLIENT` path, include headers from the validated
sysroot and link it plus `Threads::Threads`; never link `common` or the 3FS
`fdb` target. Also register the same source in the full project for G0-B:

```cmake
target_add_bin(fdb_client_smoke "fdb_client_smoke.cc" fdb)
```

Run the `platform-smoke` build command from Task 3 with a passing preflight.

Expected: the standalone target builds against the exact headers/library
recorded by preflight without configuring the 3FS common library.

- [ ] **Step 5: Cross-build and inspect the RISC-V result**

Run:

```bash
readelf -h build/cxl-riscv-smoke/bin/fdb_client_smoke | rg 'Machine:.*RISC-V'
readelf -d build/cxl-riscv-smoke/bin/fdb_client_smoke | rg 'libfdb_c'
! readelf -d build/cxl-riscv-smoke/bin/fdb_client_smoke | rg 'libibverbs'
```

Expected: RISC-V machine, a matching FDB client dependency, and no ibverbs dependency.

- [ ] **Step 6: Start the pinned server and run a real guest transaction**

In a fresh G0 guest, render a single-node cluster file in the run-owned writable
image, start `/opt/foundationdb/bin/fdbserver` bound to the declared guest TCP
address, wait for its availability marker, and then run:

Run:

```text
/opt/3fs/bin/fdb_client_smoke --cluster-file /opt/3fs/etc/fdb.cluster \
  --key g0 --value riscv-fdb-ok
```

Expected: JSON with `set_ok`, `get_ok`, `value_match`, and `status: "passed"` all true.

The runner records `fdbserver --version`, its RISC-V ELF/dependency hashes, the
cluster-file hash, bind/coordinator addresses and clean server shutdown. A
host-side server, memory KV substitute or pre-existing external database makes
the result invalid.

- [ ] **Step 7: Record the uncommitted checkpoint**

Run `git diff --check && git status --short`.

## Task 7: Add non-destructive remote synchronization and the G0 evidence gate

**Files:**

- Create: `deploy/cxl-riscv/sync_to_vlm_server.py`
- Create: `deploy/cxl-riscv/run_g0.py`
- Create: `tests/cxl_riscv/test_sync_to_vlm_server.py`
- Create: `tests/cxl_riscv/test_g0_evidence.py`

**Interfaces:**

- Produces: a content-complete source snapshot (tracked diff identity, tracked
  tombstones and hashes for every included file) and an identical remote working tree
  at the exact configured `components/3FS` path without copying local `.git`
  metadata.
- For `full-3fs`, produces separate canonical identities for the 3FS root and
  every initialized nested repository: gitlink, HEAD, tracked diff,
  tombstones and non-ignored untracked content. `platform-smoke` deliberately
  contains only the root identity.
- Produces paths such as `out/cxl-riscv/g0-a/20260904T120000Z-1234/result.json` and `out/cxl-riscv/g0-b/20260904T130000Z-1234/result.json`, with run directories generated from UTC time and the runner PID.
- Produces in each result a canonical `platform_contract` containing normalized
  QEMU/CXLMemSim device/server options, artifact hashes, topology hash and
  endpoint/central-backing identity policy; later runners consume its hash
  instead of reconstructing platform defaults.
- On a passing G0-B run, produces `out/cxl-riscv/g0-b/g0-b-handoff.json` containing the immutable result path and its SHA-256; it refuses to replace a different existing handoff implicitly.
- Produces: `validate_result(result: dict, profile: str) -> list[str]`.

- [ ] **Step 1: Test synchronization safety and the result gate**

```python
def test_snapshot_covers_untracked_content_and_tracked_deletion(self):
    snapshot = sync_to_vlm_server.build_snapshot(dirty_fixture())
    self.assertEqual(snapshot.base_head, BASE_HEAD)
    self.assertEqual(snapshot.files["src/new.cc"].sha256, NEW_CC_SHA256)
    self.assertIn("src/common/net/RDMAControl.cc", snapshot.tracked_tombstones)

def test_transfer_never_uses_broad_delete_or_copies_git(self):
    argv = sync_to_vlm_server.transfer_command(bundle(), "vlm-server", incoming_dir())
    self.assertNotIn("--delete", argv)
    self.assertNotIn(".git", " ".join(argv))

def test_result_rejects_x86_or_ibverbs(self):
    result = passing_result("full-3fs")
    result["binaries"]["storage_main"]["machine"] = "X86-64"
    result["binaries"]["storage_main"]["needed"].append("libibverbs.so.1")
    errors = run_g0.validate_result(result, "full-3fs")
    self.assertIn("storage_main is not RISC-V", errors)
    self.assertIn("storage_main links libibverbs.so.1", errors)

def test_platform_smoke_does_not_require_storage_binary(self):
    result = passing_result("platform-smoke")
    result["binaries"].pop("storage_main", None)
    self.assertEqual(run_g0.validate_result(result, "platform-smoke"), [])

def test_platform_smoke_requires_real_fuse_requests(self):
    result = passing_result("platform-smoke")
    result["fuse"]["read_seen"] = False
    self.assertIn(
        "FUSE lookup/open/read/unmount proof is incomplete",
        run_g0.validate_result(result, "platform-smoke"),
    )

def test_platform_smoke_requires_two_overlapping_distinct_guests(self):
    result = passing_result("platform-smoke")
    result["dax"]["guests"][1]["backing_inode"] = \
        result["dax"]["guests"][0]["backing_inode"]
    result["dax"]["qemu_lifetimes_overlap"] = False
    errors = run_g0.validate_result(result, "platform-smoke")
    self.assertIn("DAX guests do not have distinct backing inodes", errors)
    self.assertIn("DAX guest lifetimes did not overlap", errors)

def test_platform_smoke_requires_bidirectional_peer_publication(self):
    result = passing_result("platform-smoke")
    result["dax"]["directions"].pop()
    self.assertIn(
        "DAX peer publication is not bidirectional",
        run_g0.validate_result(result, "platform-smoke"),
    )

def test_platform_smoke_requires_correlated_coherence_trace(self):
    result = passing_result("platform-smoke")
    result["dax"]["coherence"]["events"] = []
    self.assertIn(
        "DAX range has no authoritative CXLMemSim coherence evidence",
        run_g0.validate_result(result, "platform-smoke"),
    )

def test_platform_smoke_requires_both_atomic_widths_lock_free(self):
    result = passing_result("platform-smoke")
    result["dax"]["directions"][0]["writer"]["atomic_u32_lock_free"] = False
    self.assertIn(
        "DAX 32/64-bit atomics are not lock-free",
        run_g0.validate_result(result, "platform-smoke"),
    )
```

The passing fixture's `dax` object is not a single-process smoke flag. It
contains exactly two guest records with unique endpoint/host IDs and
`[st_dev, st_ino]` backing identities, QEMU start/stop monotonic timestamps,
the common DPA interval, two transfer records (`A -> B` generation 1 and
`B -> A` generation 2), all four writer/reader 32/64-bit lock-free-atomic
results, and a
hash-addressed coherence-v2 trace slice whose decoded events overlap that DPA
interval. `validate_result()` independently rejects duplicate guest identity,
duplicate backing identity, non-overlapping lifetimes, loopback or missing
directions, generation/checksum mismatch, non-lock-free atomics, and an empty,
uncorrelated or out-of-range trace.

- [ ] **Step 2: Run tests before implementing the scripts**

Run `python3 -m unittest -v tests/cxl_riscv/test_sync_to_vlm_server.py tests/cxl_riscv/test_g0_evidence.py`.

Expected: FAIL with import errors.

- [ ] **Step 3: Implement safe remote staging**

The script first creates a canonical local snapshot containing the 3FS base
`HEAD`, the tracked binary-diff hash, every non-ignored working-tree file's
path/mode/type/SHA-256, tracked/untracked classification, and an explicit
tracked-tombstone list.
`git diff` and `git status` alone are not a source identity because neither
hashes untracked contents. Build/out paths, `.git` metadata and the contents of
gitlink submodules are excluded by type, not by an unchecked string glob.

That exclusion applies to the G0-A root snapshot. In `full-3fs` mode, enumerate
the exact gitlinks recursively and add one independently canonical nested-repo
record for each initialized checkout; never flatten its files into the parent
manifest. Require its HEAD to equal the recorded gitlink, then record the exact
post-edit diff, tombstones and untracked bytes.

It then performs a read-only remote check. If the exact remote 3FS directory is
absent, it clones `git@github.com:Yuer-yuan/3FS.git` without submodules and
checks out the snapshot's base commit. If it exists, it requires the resolved
Git top level and `HEAD` to equal those expected values and compares its state
with the previous sync receipt. Any unrelated remote edit, unowned untracked
path or base mismatch blocks synchronization; the script never resets it.

Compare the local manifest with the remote receipt and transfer every desired
file whose hash differs to a unique run-owned
`components/3FS/out/cxl-riscv/sync/<id>/incoming` directory. Verify every
received hash before applying anything. For each exact
path, allow replacement only when the current remote hash equals the prior
receipt or base blob; first preserve the old bytes in that run's `quarantine`,
then atomically rename the verified incoming file into place. For a tracked
tombstone, or a previously synced untracked file that disappeared locally,
verify the same ownership/hash condition and move that exact path to quarantine
instead of broadly deleting it. This desired-state delta works on repeated
syncs, including a file changed twice or reverted to its base contents; applying
a base-to-local patch on top of an earlier dirty sync would not.

Reject absolute paths, `..`, duplicate normalized paths, special files and any
destination with a symlink ancestor. Use `lstat`/directory-fd-relative
operations so a payload symlink cannot escape the checkout.

Finally recompute the scoped remote snapshot and require its canonical hash to
equal the local hash, write an atomic sync receipt, and print remote `git status
--short`. No command uses `--delete`, copies `.git`, initializes nested
submodules during G0-A, or mutates a path outside the exact checkout.

- [ ] **Step 4: Implement the G0 runner and evidence validator**

`run_g0.py` records profile-specific requirements:

```python
PLATFORM_SMOKE_BINARIES = (
    "cxl_dax_smoke", "fuse_mount_smoke", "fdb_client_smoke", "fdbserver",
)
FULL_3FS_BINARIES = PLATFORM_SMOKE_BINARIES + (
    "mgmtd_main", "meta_main", "storage_main", "hf3fs_fuse_main", "cxl-fabricd",
)
```

For every binary it records ELF machine, interpreter and NEEDED entries. It
boots the existing absolute QEMU artifact, verifies `/dev/dax0.0`, and runs the
two-overlapping-guest, distinct-backing, bidirectional DAX proof from Task 5,
including target lock-free atomics and a hash-addressed, DPA-correlated
CXLMemSim coherence-v2 trace slice. It mounts/reads/unmounts the FUSE probe,
starts the pinned guest `fdbserver`, and runs the FDB transaction probe for
both profiles. In `full-3fs` it
also validates the phase-1 closure handoff and starts each 3FS process with a
G0 configuration long enough to observe a ready marker or a precise dependency
failure. A passing result requires every profile-required binary to be RISC-V,
zero ibverbs dependencies, passing bidirectional inter-guest DAX evidence, a
compatible real FDB server transaction and clean runner process teardown.

For each run, copy the preflight's immutable base rootfs into a newly created
run staging directory, install only the profile-required binaries and their
manifested shared-library closure, then call `guest_image.py`. Never modify the
base rootfs or reuse a writable image from another session.

- [ ] **Step 5: Run unit tests**

Run:

```bash
python3 -m unittest -v \
  tests/cxl_riscv/test_g0_evidence.py \
  tests/cxl_riscv/test_preflight.py \
  tests/cxl_riscv/test_build_riscv.py \
  tests/cxl_riscv/test_cross_dependency_contract.py \
  tests/cxl_riscv/test_guest_image.py \
  tests/cxl_riscv/test_fuse_probe_contract.py
```

Expected: all tests PASS.

- [ ] **Step 6: Prove G0-A is independent of nested 3FS submodules**

Run the `platform-smoke` print-only build with all fourteen nested submodules
still uninitialized. Inspect both argv arrays and the standalone CMake inputs.

Expected: neither command names a nested submodule, the main 3FS CMake project
or `common`. Do not initialize submodules merely to make G0-A pass.

- [ ] **Step 7: Execute G0-A on the remote host before phase-1 implementation**

Run from the remote superproject root after preflight passes:

```bash
python3 components/3FS/deploy/cxl-riscv/build_riscv.py \
  --preflight components/3FS/out/cxl-riscv/preflight.json \
  --profile platform-smoke \
  --build-dir components/3FS/build/cxl-riscv-smoke --jobs 8
python3 components/3FS/deploy/cxl-riscv/run_g0.py \
  --preflight components/3FS/out/cxl-riscv/preflight.json \
  --profile platform-smoke \
  --build-manifest components/3FS/build/cxl-riscv-smoke/build-manifest.json \
  --output-root components/3FS/out/cxl-riscv/g0-a
```

Expected: the runner prints an absolute `result.json` path whose `status` is
`passed` and `gate` is `G0-A`. If any prerequisite is absent, preserve the
failed result and stop before phase-1 implementation.

- [ ] **Step 8: Execute G0-B after phase-1 Task 9 passes**

Before G0-B only, run inside the remote 3FS checkout:

```bash
git submodule update --init --recursive
./patches/apply.sh
```

Expected: all fourteen nested submodules populate at recorded revisions and the
project patches apply cleanly. Stop if an existing nested submodule has local
changes or a conflicting revision. Record each nested repository's gitlink
commit, tracked diff and untracked content manifest after patching; the G0-B
build manifest includes this recursive source identity. Require the remote root
plus nested identities to equal the local phase-1 source closure before
configuring; a top-level `git status` match is insufficient. Then run from the
remote superproject root:

```bash
python3 components/3FS/deploy/cxl-riscv/build_riscv.py \
  --preflight components/3FS/out/cxl-riscv/preflight.json \
  --profile full-3fs \
  --source-closure components/3FS/out/cxl-riscv/phase1/source-closure.json \
  --dependency-contract components/3FS/deploy/cxl-riscv/full-deps.json \
  --build-dir components/3FS/build/cxl-riscv-full --jobs 8
python3 components/3FS/deploy/cxl-riscv/run_g0.py \
  --preflight components/3FS/out/cxl-riscv/preflight.json \
  --profile full-3fs \
  --source-closure components/3FS/out/cxl-riscv/phase1/source-closure.json \
  --build-manifest components/3FS/build/cxl-riscv-full/build-manifest.json \
  --output-root components/3FS/out/cxl-riscv/g0-b
```

Expected: `gate` is `G0-B`; all four 3FS application processes plus
`cxl-fabricd` reach their declared start marker; all required ELF files are
RISC-V and have no verbs dependency. A
failure blocks phase-1 Task 11 multi-guest execution.

- [ ] **Step 9: Record the uncommitted G0 checkpoints**

Run:

```bash
git diff --check
git status --short
```

Expected: only G0 source, tests, configuration and approved documentation are modified; generated build/out paths are ignored; no commit exists.
