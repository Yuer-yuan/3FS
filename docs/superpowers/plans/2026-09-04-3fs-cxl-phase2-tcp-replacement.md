# 3FS CXL Phase 2 TCP Replacement Implementation Plan

> **Execution constraint:** Implement task-by-task in one agent. Do not spawn
> subagents unless the user separately requests them. Steps use checkbox
> (`- [ ]`) syntax for tracking.

**Goal:** Move Core RPC, CXL connection rendezvous and FoundationDB's network carrier off the ordinary TCP serving network so every post-readiness 3FS serving byte crosses the CXL shared region.

**Architecture:** Replace phase-1 TCP lane bootstrap with static, generation-scoped endpoint records in the CXL control region; route Core services directly through `CxlSocket`; and run one `cxl-netd` per participating guest so FoundationDB keeps its unmodified TCP/IP protocol while Linux sends those packets through a dedicated `cxl0` TUN device backed by separate CXL packet lanes.

**Tech Stack:** C++20, Folly coroutines/event loops, Linux TUN/TAP API,
mmap/eventfd/epoll, CMake/Ninja, GoogleTest, Python 3 `unittest`, RISC-V Linux
guests, the exact G0-pinned FoundationDB C API 710 server/client bundle, QEMU
and CXLMemSim coherence-v2.

**Spec:** `docs/superpowers/specs/2026-09-04-3fs-cxl-transport-design.md`

## Global Constraints

- Begin only after the phase-1 plan has passed its complete-cluster gate.
- Modify files only inside `components/3FS`; consume parent QEMU, Linux and CXLMemSim artifacts read-only by absolute path.
- Run remote integration only under `/home/guokc/mypro/gnn-mount-sda1/CXLMemSim-riscv` on `vlm-server`.
- Core RPC must use native `CxlSocket`; do not encapsulate Core RPC inside IP.
- FoundationDB keeps its C API, wire protocol, retry and worker-discovery behavior. Only its packet carrier changes.
- After every participant emits `CXL_READY`, no application serving connection
  may use a non-`cxl0` TCP interface. Normal phase-2 guest orchestration uses
  the QEMU serial console; ordinary guest NICs have no address or route and are
  administratively down before the barrier.
- Define `serving_tcp_bytes` as TCP payload observed on interfaces other than loopback and `cxl0`; TCP packets injected into `cxl0` are counted only as `fdb_cxl_packets` and `fdb_cxl_bytes`.
- Use fixed guest, endpoint and lane ownership from a checked manifest. Every
  DAX-mapping process has a unique endpoint ID and writes only its own state
  page and queue cursors; guest ID and `cxl0` address are separate identities.
- A stale epoch, manifest SHA-256, superblock CRC32C, endpoint generation, lane
  generation, nonce or capability mismatch fails closed.
- Give FDB packet traffic its own queues, arena budget and counters; it must not consume RPC or bulk queue capacity.
- Do not use firewall rules as the sole proof that ordinary TCP is unused. Require positive CXL evidence and zero forbidden-interface deltas.
- Missing RISC-V `fdbserver`, compatible `libfdb_c`, `/dev/net/tun`, TUN kernel support or route isolation blocks phase 2; it never enables a TCP fallback.
- Do not create commits. End every task with an uncommitted review checkpoint.
- Unless a step explicitly names the superproject root, run its commands from the `components/3FS` checkout root.

---

## File map

**Create in `src/common/net/cxl`:**

- `CxlRendezvous.h`, `CxlRendezvous.cc` — deterministic endpoint/lane discovery without TCP.

**Reuse/modify from phase 1 in `src/common/net/cxl`:**

- `CxlEndpointState.h`, `CxlEndpointState.cc` — owner-only lifecycle and
  heartbeat validation shared by both phases; phase 2 must not fork its ABI.

**Create `cxl-netd`:**

- `src/tools/cxl-netd/CMakeLists.txt`
- `src/tools/cxl-netd/CxlNetAbi.h`
- `src/tools/cxl-netd/CxlNetConfig.h`, `CxlNetConfig.cc`
- `src/tools/cxl-netd/TunDevice.h`, `TunDevice.cc`
- `src/tools/cxl-netd/CxlPacketPort.h`, `CxlPacketPort.cc`
- `src/tools/cxl-netd/CxlNetDaemon.h`, `CxlNetDaemon.cc`
- `src/tools/cxl-netd/main.cc`

**Create tests and deployment assets:**

- `tests/common/net/cxl/TestCxlRendezvous.cc`
- `tests/tools/cxl-netd/TestCxlNetAbi.cc`
- `tests/tools/cxl-netd/TestCxlPacketPort.cc`
- `tests/tools/cxl-netd/TestTunDevice.cc`
- `tests/tools/cxl-netd/CMakeLists.txt`
- `tests/cxl_riscv/test_phase2_manifest.py`
- `tests/cxl_riscv/test_phase2_network_gate.py`
- `tests/cxl_riscv/test_phase2_evidence.py`
- `tests/cxl_riscv/test_phase2_source_closure.py`
- `deploy/cxl-riscv/cxl_net.py`
- `deploy/cxl-riscv/phase2_manifest.py`
- `deploy/cxl-riscv/run_phase2.py`
- `deploy/cxl-riscv/config/phase2-cluster.json`
- `deploy/cxl-riscv/config/fdb.cluster.in`
- `deploy/cxl-riscv/config/cxl-netd.toml`

**Modify existing files:**

- `src/common/net/cxl/CxlAbi.h`, `CxlLayout.*`, `CxlSocket.*`, `CxlProgressEngine.*`, `CxlMetrics.*`
- `tests/common/net/cxl/TestCxlEndpointState.cc`
- `src/common/net/Listener.*`, `Transport.*`, `ServiceGroup.*`, `Server.*`
- `src/core/service/CoreService.*`
- `src/core/app/ServerLauncher.*`, `ServerAppConfig.*`
- `src/fdb/FDBContext.*`, `FDBConfig.h`
- `src/tools/CMakeLists.txt`, `tests/CMakeLists.txt`
- 3FS server/client configs under `configs/`
- G0 guest-image, preflight and runner files under `deploy/cxl-riscv/`

## Task 1: Freeze a static endpoint and lane manifest

**Files:**

- Create: `deploy/cxl-riscv/phase2_manifest.py`
- Create: `deploy/cxl-riscv/config/phase2-cluster.json`
- Create: `tests/cxl_riscv/test_phase2_manifest.py`
- Modify: `src/common/net/cxl/CxlLayout.h`
- Modify: `src/common/net/cxl/CxlLayout.cc`

**Interfaces:**

- Produces: `Phase2Manifest.load(path)`, `validate()`, `canonical_payload()`,
  `manifest_sha256()`, `superblock_crc32c()` and `write_binary(path)`.
- Produces: `derive_replication(base, factor)` and
  `derive_clients(base, count)` for deterministic RF1/RF2/RF3 and 1/2/4-rank
  manifests without renumbering base guests/endpoints.
- Produces: deterministic guest IDs, process-unique endpoint IDs, endpoint
  generation-incarnation schedules, directed RPC lanes with lane incarnations,
  FDB packet lanes, IP assignments and byte ranges.
- Consumes: the phase-1 `CxlLayout` range/alignment validator.
- Invariant: the same normalized JSON produces the same envelope bytes,
  manifest SHA-256 and rendered superblock CRC32C on every guest.

- [ ] **Step 1: Add import-safe manifest tests**

The test prepends the hyphenated deployment directory to `sys.path` and imports the module by filename:

```python
DEPLOY_DIR = Path(__file__).parents[2] / "deploy" / "cxl-riscv"
sys.path.insert(0, str(DEPLOY_DIR))
import phase2_manifest

class Phase2ManifestTest(unittest.TestCase):
    def test_rejects_duplicate_endpoint_and_overlapping_lane(self):
        raw = minimal_manifest()
        raw["endpoints"][1]["id"] = raw["endpoints"][0]["id"]
        raw["lanes"][1]["offset"] = raw["lanes"][0]["offset"]
        errors = phase2_manifest.Phase2Manifest.from_dict(raw).validate()
        self.assertIn("duplicate endpoint id 1", errors)
        self.assertIn("lane ranges overlap", errors)

    def test_requires_one_nonserving_fabric_authority(self):
        raw = minimal_manifest()
        raw["fabric_authority_endpoint"] = raw["endpoints"][1]["id"]
        self.assertIn(
            "fabric authority endpoint owns a serving lane",
            phase2_manifest.Phase2Manifest.from_dict(raw).validate(),
        )

    def test_normalization_is_deterministic(self):
        first = phase2_manifest.Phase2Manifest.from_dict(minimal_manifest())
        second = phase2_manifest.Phase2Manifest.from_dict(dict(reversed(list(minimal_manifest().items()))))
        self.assertEqual(first.to_binary(), second.to_binary())
        self.assertEqual(first.manifest_sha256(), second.manifest_sha256())
        self.assertEqual(first.superblock_crc32c(), second.superblock_crc32c())

    def test_crc32c_castagnoli_vector(self):
        self.assertEqual(phase2_manifest.crc32c(b"123456789"), 0xE3069283)

    def test_replication_derivation_preserves_base_ids(self):
        rf1 = load_checked_manifest()
        rf3 = phase2_manifest.derive_replication(rf1, 3)
        self.assertEqual(rf3.endpoint("mgmtd").id, rf1.endpoint("mgmtd").id)
        self.assertEqual(rf3.endpoint("storage-tail-2").id, 11)
        self.assertEqual(rf3.validate(), [])

    def test_client_derivation_uses_disjoint_stable_ids(self):
        base = load_checked_manifest()
        four = phase2_manifest.derive_clients(base, 4)
        self.assertEqual(four.endpoint("fuse-client-rank-0").id, 6)
        self.assertEqual(four.endpoint("fuse-client-rank-3").id, 1003)
        self.assertEqual(four.guest("client-rank-3").id, 103)
        self.assertEqual(four.validate(), [])
```

- [ ] **Step 2: Run the test and verify the module is absent**

Run:

```bash
python3 -m unittest -v tests/cxl_riscv/test_phase2_manifest.py
```

Expected: FAIL with `ModuleNotFoundError: No module named 'phase2_manifest'`.

- [ ] **Step 3: Implement canonical validation and binary output**

Reject non-power-of-two alignment, non-64-byte offsets, duplicate endpoint IDs, missing reverse lanes, overlapping ranges, endpoint-owned arena violations and capacity overflow. Canonicalize lists by numeric ID before serialization:

```python
def normalized(self) -> dict[str, object]:
    return {
        "schema_version": self.schema_version,
        "session_generation": self.session_generation,
        "region_bytes": self.region_bytes,
        "fabric_authority_endpoint": self.fabric_authority_endpoint,
        "guests": sorted((g.as_dict() for g in self.guests), key=lambda g: g["id"]),
        "endpoints": sorted((e.as_dict() for e in self.endpoints), key=lambda e: e["id"]),
        "lanes": sorted((lane.as_dict() for lane in self.lanes), key=lambda lane: lane["id"]),
    }

def canonical_payload(self) -> bytes:
    return (json.dumps(self.normalized(), separators=(",", ":"),
                       sort_keys=True) + "\n").encode("utf-8")

def to_binary(self) -> bytes:
    payload = self.canonical_payload()
    header = struct.pack(
        "<8sIIQQ32s", b"HF3FSP2M", self.schema_version, 64,
        len(payload), self.session_generation, hashlib.sha256(payload).digest(),
    )
    return header + payload

def manifest_sha256(self) -> bytes:
    return hashlib.sha256(self.to_binary()).digest()

def superblock_crc32c(self) -> int:
    return crc32c(render_superblock(self, self.manifest_sha256(), crc_value=0))
```

The binary envelope header is exactly 64 little-endian bytes: 8-byte magic,
`u32 schema_version`, `u32 header_bytes`, `u64 payload_bytes`, `u64
session_generation` and the 32-byte SHA-256 of the canonical JSON payload.
Reject a bad payload digest, noncanonical payload, length mismatch or trailing
bytes. The full envelope SHA-256 is embedded in the independently rendered
ABI-v1 4 KiB superblock; its CRC32C is computed with only the superblock CRC
field zero. This avoids a self-referential manifest hash and keeps the full
topology identity distinct from superblock corruption detection.

Use schema version `2` for phase 2. Guest identity controls VM placement and
the L3 address; endpoint identity controls ownership of shared-memory words.
Normal manifests declare incarnation `[1]`; a runner-generated fault overlay
may declare the exact bounded next endpoint/lane incarnations it will exercise.
Only a transition to the next declared 64-bit ordinal is accepted. The runner
chooses a fresh nonzero session generation with `getrandom`, rejects reuse in
its append-only ledger and creates fresh backing; an exhausted incarnation
schedule requires a new session rather than dynamic discovery.
The checked-in tiny topology is:

| Guest | Role / `cxl0` | DAX-mapping endpoint IDs |
| ---: | --- | --- |
| 1 | mgmtd / `10.203.0.1/24` | `1=mgmtd`, `2=mgmtd-netd`, `64=fabricd` |
| 2 | meta / `10.203.0.2/24` | `3=meta`, `4=meta-netd` |
| 3 | storage / no FDB route | `5=storage` |
| 4 | client / `10.203.0.4/24` | `6=fuse-client`, `7=admin`, `8=client-netd` |
| 5 | FDB / `10.203.0.5/24` | `9=fdb-netd`; `fdbserver` itself does not map DAX |

Endpoint 7 is reserved so an admin process never shares the FUSE client's
state page or arena. The four `*-netd` endpoints receive a full set of
manifest-declared directed packet lanes. Because mgmtd configuration loading,
Meta and admin tooling can each use `libfdb_c`, all four corresponding guests
participate in the CXL-backed subnet. Reject duplicate guest IDs, endpoint IDs,
endpoint ownership within a guest, and duplicate `cxl0` addresses.

Endpoint 64 is the sole `fabric_authority_endpoint`. It owns the global
superblock lifecycle and a state page but no serving, packet or bulk lane and
no bulk arena. `cxl-fabricd` starts in guest 1 before mgmtd/netd/FDB, which
breaks the otherwise circular dependency between mgmtd's FDB access and
`cxl0` attachment. Reject a missing authority, a second authority, or any lane
assigned to endpoint 64.

RF2 deterministically adds guest `6` with endpoint `10=storage-successor-1`;
RF3 adds guest `7` with endpoint `11=storage-tail-2`. These storage-only guests
have no `cxl0` address or netd. Derivation preserves IDs/ranges from the base
manifest, appends aligned arenas and creates exactly the Core, storage-chain,
forwarding and resync lanes required by the declared service graph. It fails if
the frozen region has insufficient space. Each derived JSON and binary manifest
is written inside its run bundle and hashed; it is never edited in place after
readiness.

Client-rank expansion uses a disjoint namespace: extra rank `r` (`1..3`) owns
guest `100+r` and FUSE endpoint `1000+r`; rank 0 retains guest 4 and endpoint
6. Extra IO500 clients do not run admin tools or link `libfdb_c`, so they do
not receive a netd endpoint or `cxl0` address. Enforce that dependency closure
rather than assuming it. Deriving replication then clients, or clients then
replication, must produce byte-identical normalized output. This keeps RF and
client-count changes orthogonal.

In phase-2 IO500 comparison runs, one rank executes in each independent client
guest. A comparison-only `mpi0` virtio network matches the unmodified LegoFS
MPI/Hydra control path; it is absent in the normal functional profile and has
no route to Core or FDB addresses. Classify it as
`benchmark_control_tcp_bytes`. Only manifest-pinned MPI/Hydra process IDs and
ports may bind or send on it; any 3FS/FDB use is a forbidden fallback.

Implement `crc32c` in the module with the reflected Castagnoli polynomial
`0x82F63B78`; do not substitute `zlib.crc32`, which implements a different
polynomial. The C++ ABI test must produce the same value for the canonical test
vector and checked-in manifest.

- [ ] **Step 4: Make C++ layout validation consume the binary manifest**

Add:

```cpp
struct ManifestIdentity {
  uint32_t schemaVersion;
  uint32_t superblockCrc32c;
  uint64_t sessionGeneration;
  std::array<std::byte, 32> manifestSha256;
};

Result<CxlLayout> CxlLayout::fromManifest(std::span<const std::byte> bytes,
                                         uint64_t mappedBytes);
```

The C++ parser must reject a phase-1 schema, trailing bytes, integer overflow and any range beyond `mappedBytes` before creating a pointer.

- [ ] **Step 5: Run deterministic round-trip and C++ ABI tests**

Run:

```bash
python3 -m unittest -v tests/cxl_riscv/test_phase2_manifest.py
cmake --build build/cxl-g0-host --target test_common
build/cxl-g0-host/tests/test_common --gtest_filter='TestCxlLayout.*:TestCxlAbi.*'
```

Expected: all tests PASS and two serializations of the checked-in topology have identical SHA-256 hashes.

- [ ] **Step 6: Review checkpoint**

Run `git diff --check` and inspect `git status --short`. Confirm every changed path begins with `components/3FS/` from the superproject root and leave the changes uncommitted.

## Task 2: Reuse endpoint state and implement generation-scoped static CXL rendezvous

**Files:**

- Modify: `src/common/net/cxl/CxlEndpointState.h`
- Modify: `src/common/net/cxl/CxlEndpointState.cc`
- Create: `src/common/net/cxl/CxlRendezvous.h`
- Create: `src/common/net/cxl/CxlRendezvous.cc`
- Modify: `tests/common/net/cxl/TestCxlEndpointState.cc`
- Create: `tests/common/net/cxl/TestCxlRendezvous.cc`
- Modify: `src/common/net/cxl/CxlAbi.h`
- Modify: `src/common/net/cxl/CxlSocket.cc`
- Modify: `src/common/net/cxl/CxlMetrics.h`

**Interfaces:**

- Consumes: phase-1 `EndpointLifecycle {Free, Starting, Ready, Draining,
  Retired, Faulted}`, `CxlEndpointRecord` and `CxlEndpointState` methods.
- Produces: `CxlRendezvous::start`, `acceptReady`, `connect`, `drain`.
- Consumes: phase-1 `CxlLaneOwnerRecord`, with one requester and one acceptor
  record per manifest lane.
- Removes runtime dependency on `CxlConnectService` from phase-2 builds.

- [ ] **Step 1: Write lifecycle and ownership tests**

```cpp
TEST(TestCxlEndpointState, RejectsWriteToPeerOwnedPage) {
  auto region = TestRegion::create(1_MB);
  CxlEndpointState local(region, EndpointId{4}, EndpointId{4});
  CxlEndpointState peer(region, EndpointId{5}, EndpointId{4});
  EXPECT_TRUE(local.publish(EndpointLifecycle::Starting));
  EXPECT_EQ(peer.publish(EndpointLifecycle::Ready).error().code(), StatusCode::kInvalidArg);
}

TEST(TestCxlRendezvous, StaleGenerationCannotBecomeReady) {
  auto pair = RendezvousFixture::readyPair();
  pair.manifest().lane(7).peerGeneration++;
  auto result = pair.client().connect(ServiceId{CoreService::kServiceId}, 50_ms);
  EXPECT_EQ(result.error().code(), RPCCode::kStaleGeneration);
  EXPECT_EQ(pair.metrics().generationMismatch(), 1);
}
```

- [ ] **Step 2: Run and observe missing rendezvous types**

Run:

```bash
cmake --build build/cxl-g0-host --target test_common
build/cxl-g0-host/tests/test_common --gtest_filter='TestCxlEndpointState.*:TestCxlRendezvous.*'
```

Expected: compilation FAIL because `CxlRendezvous` does not exist; the reused
phase-1 endpoint-state tests still compile.

- [ ] **Step 3: Reuse and enforce the phase-1 endpoint record**

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

This is the exact phase-1 ABI, including its 64-bit endpoint generation. Do
not introduce a phase-2-only layout or narrow the generation in the JSON/
binary manifest.

The one owner keeps the next sequence locally: relaxed-store an odd
`recordSequence`, execute a sequentially consistent fence, update the aligned
32/64-bit body words and CRC with relaxed lock-free atomic stores, then
release-store the next even sequence.
Never use `fetch_add` or another shared RMW. A reader acquire-loads the first
sequence, retries if it is odd, reads body words with relaxed atomics, executes
an acquire fence, and accepts only when a second acquire-load returns the same
even value with a matching CRC. Compute the CRC over the complete 64-byte
encoding with the CRC field zero and `recordSequence` set to the final even
value that will be published.
Add a stress test that updates heartbeat and lifecycle concurrently with
snapshots and never accepts a torn record; comparing CRC twice without the
sequence protocol is not sufficient.

- [ ] **Step 4: Implement deterministic lane matching**

`connect(service, timeout)` finds exactly one outgoing lane whose local
endpoint, remote endpoint, service and `ServicePlane` match the manifest. It
waits for both endpoint records to carry the expected session and endpoint
generations, and for both lane-owner records to carry stable matching session,
lane generation, nonce, capabilities and their respective endpoint generations,
then constructs `CxlSocket`. Zero or
multiple matches are configuration errors. The resulting `Transport` takes its
plane from the validated lane, never from `TransportKind::CXL`.

`acceptReady()` scans only manifest-declared incoming lanes and returns each generation once. It never scans unassigned region bytes.

Within a live session, a lane generation never resets, including after an
endpoint-process restart, because frame entries do not carry endpoint
generation. Rendezvous accepts only the next manifest-declared incarnation
after both stable owner records establish the previous high-water value; a
torn or corrupt high-water record faults the session and requires fresh
backing rather than guessing or restarting the lane at generation one.

- [ ] **Step 5: Add restart, corruption and delayed-peer cases**

Cover:

```text
peer absent -> timeout without slot reclamation
peer generation changed -> old lane Faulted
manifest SHA-256 or superblock CRC changed -> process refuses readiness
record checksum bad -> cxl_crc_error increments
same generation seen twice -> second accept suppressed
Ready -> Draining -> Retired -> next generation accepted
```

- [ ] **Step 6: Run host fault tests under sanitizers**

Run:

```bash
cmake -S . -B build/cxl-p2-asan -G Ninja \
  -DHF3FS_ENABLE_CXL=ON -DHF3FS_ENABLE_RDMA=OFF \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo -DSANITIZER=ASAN
cmake --build build/cxl-p2-asan --target test_common
ASAN_OPTIONS=detect_leaks=1 build/cxl-p2-asan/tests/test_common \
  --gtest_filter='TestCxlEndpointState.*:TestCxlRendezvous.*'
```

Expected: PASS with no sanitizer diagnostics.

- [ ] **Step 7: Review checkpoint**

Run `git diff --check`; confirm no cross-endpoint atomic RMW was added and leave changes uncommitted.

## Task 3: Cut Core and phase-1 bootstrap over to static CXL

**Files:**

- Modify: `src/common/net/Listener.h`
- Modify: `src/common/net/Listener.cc`
- Modify: `src/common/net/Transport.h`
- Modify: `src/common/net/Transport.cc`
- Modify: `src/common/net/ServiceGroup.h`
- Modify: `src/common/net/ServiceGroup.cc`
- Modify: `src/common/net/Server.cc`
- Modify: `src/core/service/CoreService.h`
- Modify: `src/core/service/CoreService.cc`
- Modify: `src/core/app/ServerLauncher.cc`
- Create: `configs/cxl/phase2-common.toml`
- Modify: `configs/hf3fs_client_agent.toml`
- Modify: `configs/meta_main.toml`
- Modify: `configs/mgmtd_main.toml`
- Modify: `configs/monitor_collector_main.toml`
- Modify: `configs/storage_main.toml`
- Create: `tests/common/net/cxl/TestCxlCore.cc`
- Create: `tests/cxl_riscv/test_phase2_source_closure.py`

**Interfaces:**

- Produces: `Listener::startCxl(CxlRendezvous&)` for manifest-declared incoming lanes.
- Produces: `Transport::create(Address::CXL, IOWorker&, CxlRendezvous&)` with no TCP dial.
- Consumes: the phase-1 `ServicePlane` API and static rendezvous.
- Gate: phase-2 config contains no serving `network_type = 'TCP'` and disables `CxlConnectService`.

- [ ] **Step 1: Add a Core-over-CXL test with TCP disabled**

```cpp
TEST(TestCxlCore, EchoAndGetConfigNeedNoTcpSocket) {
  StaticCxlCluster cluster({.denyTcpSocket = true});
  cluster.startCore();
  EXPECT_EQ(cluster.client().echo("phase2").value(), "phase2");
  EXPECT_FALSE(cluster.client().getConfig().value().empty());
  EXPECT_EQ(cluster.socketAudit().inetStreamOpenAttempts(), 0);
}
```

- [ ] **Step 2: Run the test and confirm phase-1 code dials TCP**

Run:

```bash
cmake --build build/cxl-g0-host --target test_common
build/cxl-g0-host/tests/test_common --gtest_filter='TestCxlCore.*'
```

Expected: FAIL because the current CXL path needs the TCP bootstrap service.

- [ ] **Step 3: Add static-rendezvous listener and connector branches**

For `Address::CXL`, `Listener::setup()` validates the local endpoint/service assignment and creates no `ServerSocket`. `Listener::start()` registers `acceptReady()` with the local progress engine. `Transport::connect()` calls `CxlRendezvous::connect()` and never constructs a TCP address.

Keep phase-1 bootstrap source buildable behind:

```cmake
option(HF3FS_CXL_TCP_BOOTSTRAP "Enable phase-1 CXL TCP bootstrap" OFF)
```

Reject `HF3FS_CXL_TCP_BOOTSTRAP=ON` in the phase-2 runner rather than silently selecting it.

- [ ] **Step 4: Move Core transport to CXL while retaining the Control plane**

Register `CoreService` in the Control table with a manifest-declared CXL
listener and set the connected transport's plane from that lane. Former RDMA
services remain in the Data table. Add a dispatch test proving that a Core CXL
lane cannot call a Data service ID and a Data CXL lane cannot call a
Control-only Core method. Remove assumptions that Core peer credentials are
available from a TCP socket; calls that require Unix peer credentials remain
local-only and return an explicit unsupported error on CXL.

- [ ] **Step 5: Generate phase-2 configs from one checked topology**

`run_phase2.py` writes generated TOML into its run directory. For example, the
mgmtd Core group receives:

```toml
network_type = 'CXL'
service_plane = 'Control'
cxl_endpoint_id = 1
cxl_manifest = '/etc/hf3fs/phase2.manifest'
cxl_tcp_bootstrap = false
```

Use `service_plane = 'Control'` only for Core groups and `Data` for the former
RDMA groups. Other processes receive their own manifest value rather than the
example ID `1`. The endpoint ID differs per DAX-mapping process, including `cxl-netd` and an
admin process, and is taken from `phase2-cluster.json`; it is never inferred
from guest boot order. The guest ID and CXL subnet address are configured
separately and cannot be substituted for the endpoint ID.

- [ ] **Step 6: Add source and config closure checks**

Run:

```bash
python3 -m unittest -v tests/cxl_riscv/test_phase2_source_closure.py
rg -n 'CxlConnectService|acceptTCP|ServerSocket' \
  src/common/net/cxl src/core
```

Expected: the closure test passes and parses its exact
`PHASE2_CONFIG_INPUTS` list, rejecting `network_type = 'TCP'`, a plane mismatch
or `cxl_tcp_bootstrap = true` only in those inputs; phase-1 configs are not
mistaken for phase-2 inputs. The search finds only guarded phase-1
compatibility code, never the Core phase-2 path. The test owns the allowlist
and fails on any new match.

- [ ] **Step 7: Run common Core and RPC regressions**

Run:

```bash
cmake --build build/cxl-g0-host --target test_common
build/cxl-g0-host/tests/test_common --gtest_filter='TestCxlCore.*:TestCxlRendezvous.*:TestEcho.*'
ctest --test-dir build/cxl-g0-host --output-on-failure -R 'core|net'
```

Expected: PASS.

- [ ] **Step 8: Review checkpoint**

Run `git diff --check` and leave changes uncommitted.

## Task 4: Implement the CXL packet ABI and TUN daemon

**Files:**

- Create: `src/tools/cxl-netd/CMakeLists.txt`
- Create: `src/tools/cxl-netd/CxlNetAbi.h`
- Create: `src/tools/cxl-netd/CxlNetConfig.h`
- Create: `src/tools/cxl-netd/CxlNetConfig.cc`
- Create: `src/tools/cxl-netd/TunDevice.h`
- Create: `src/tools/cxl-netd/TunDevice.cc`
- Create: `src/tools/cxl-netd/CxlPacketPort.h`
- Create: `src/tools/cxl-netd/CxlPacketPort.cc`
- Create: `src/tools/cxl-netd/CxlNetDaemon.h`
- Create: `src/tools/cxl-netd/CxlNetDaemon.cc`
- Create: `src/tools/cxl-netd/main.cc`
- Create: `tests/tools/cxl-netd/TestCxlNetAbi.cc`
- Create: `tests/tools/cxl-netd/TestCxlPacketPort.cc`
- Create: `tests/tools/cxl-netd/TestTunDevice.cc`
- Create: `tests/tools/cxl-netd/CMakeLists.txt`
- Modify: `src/tools/CMakeLists.txt`
- Modify: `tests/CMakeLists.txt`

**Interfaces:**

- Produces: `TunDevice::open("cxl0")`, non-blocking `readPacket` and `writePacket`.
- Produces: `CxlPacketPort::send(std::span<const std::byte>)` and `receive()`.
- Produces: `cxl-netd --config /etc/hf3fs/cxl-netd.toml`.
- Uses dedicated `TrafficClass::FdbPacket` lanes and a maximum IP packet size of 65,535 bytes.

- [ ] **Step 1: Write ABI, MTU and packet-integrity tests**

```cpp
TEST(TestCxlNetAbi, HeaderIsOneCacheLine) {
  static_assert(sizeof(CxlNetFrame) == 64);
  EXPECT_EQ(CxlNetFrame::kMagic, 0x4E4C5843U);
}

TEST(TestCxlPacketPort, PreservesPacketBoundariesAndChecksum) {
  PacketPortPair pair({.cellBytes = 4096, .cellCount = 32});
  auto packet = deterministicPacket(65535);
  ASSERT_TRUE(pair.left().send(packet));
  EXPECT_EQ(pair.right().receive().value(), packet);

  ASSERT_TRUE(pair.left().send(packet));
  pair.corruptNextReadablePayloadByte(17);
  EXPECT_EQ(pair.right().receive().error().code(), StatusCode::kDataCorruption);
}

TEST(TestCxlPacketPort, QueueFullNeverPublishesAPacketPrefix) {
  PacketPortPair pair({.cellBytes = 4096, .cellCount = 32});
  auto packet = deterministicPacket(65535);
  pair.leaveProducerSlots(requiredFragments(packet) - 1);
  auto beforeCursor = pair.left().publishedCursor();
  auto beforeSlots = pair.snapshotAllSlots();
  ASSERT_ERROR(StatusCode::kQueueFull, pair.left().send(packet));
  EXPECT_EQ(pair.left().publishedCursor(), beforeCursor);
  EXPECT_EQ(pair.snapshotAllSlots(), beforeSlots);
}
```

Add negative cases for a stale session/lane generation, wrong endpoint pair,
nonzero flags/reserved field, zero/oversized fragment length, last fragment ending
short or long, duplicate/gapped/reordered offset, mixed packet sequence/length/
CRC across fragments, and a payload cell that does not correspond to its ring
slot. No invalid prefix may reach TUN.
Inject producer death before each fragment write, before the batch cursor
store and immediately after it. The receiver must observe either no packet or
one complete checksum-valid packet, never a prefix. Do not pretend the cursor
store and a separate process-local metric update are crash-atomic: death after
cursor publication may leave the protocol outcome complete but the producer
counter snapshot incomplete. Such a fault result records
`counter_snapshot_complete=false` and is never a measured sample.

- [ ] **Step 2: Run tests and observe missing target**

Run:

```bash
cmake --build build/cxl-g0-host --target test_cxl_netd
```

Expected: FAIL because `test_cxl_netd` does not exist.

- [ ] **Step 3: Define the fixed packet frame**

```cpp
struct alignas(64) CxlNetFrame {
  static constexpr uint32_t kMagic = 0x4E4C5843U;
  le32 magic;
  le16 abiVersion;
  le16 flags;
  le32 sourceEndpoint;       // source cxl-netd endpoint
  le32 destinationEndpoint;  // destination cxl-netd endpoint
  le32 packetLength;
  le32 fragmentOffset;
  le32 fragmentLength;
  le32 packetCrc32c;
  le32 headerCrc32c;
  le32 reserved0;
  le64 packetSequence;
  le64 sessionGeneration;
  le64 laneGeneration;
};
static_assert(sizeof(CxlNetFrame) == 64);
```

Fragment only at the CXL cell layer; reassemble a complete IP packet before writing once to TUN. Enforce an in-order packet sequence per SPSC lane and bound incomplete-packet memory by the manifest's FDB arena budget.

Before writing the first fragment, the sole producer computes the exact
fragment count and verifies that many ring slots are free. Because no other
producer can consume that credit, queue-full rejection leaves the producer
cursor and every slot unchanged. Once admitted, write every fragment payload
and header without changing the shared producer cursor, execute one release
fence, then publish the complete batch with one release-store that advances
the cursor by the exact fragment count. A crash before that store exposes no
fragment; a crash after it exposes the complete packet. A new generation
ignores any uncommitted slot bytes. Reject a configured MTU/cell/depth
combination in which one maximum packet can never fit.

Each fragment's payload cell is the immutable slot corresponding to its ring
sequence; no arbitrary payload offset is accepted. Require matching source,
destination, session and lane generations, one monotonically increasing
`packetSequence` per packet, `fragmentOffset` equal to the next expected byte,
and `fragmentLength` equal to the bounded bytes actually present in that cell.
The first offset is zero and the last fragment must end exactly at
`packetLength`. Reject a zero-length/oversized fragment, mixed packet metadata,
duplicate/reordered offsets and stale lane generations before copying to the
reassembly buffer. ABI v1 requires `flags == 0`. Packet sequence and absolute
ring cursors never wrap within a lane generation; drain and renew the lane
before either checked increment would overflow.

`headerCrc32c` is Castagnoli CRC32C over the 64-byte encoded header with that
field zeroed. `packetCrc32c` is over the complete original IP packet and is
repeated in every fragment; validate header CRC before accepting a fragment and
packet CRC only after exact, gap-free reassembly. `reserved0` must be zero.

The daemon routes destination `cxl0` IP addresses to the unique netd endpoint
declared for that guest; it never sends a packet to the 3FS application
endpoint that happens to reside in the same guest.

In the source CMake file, put every daemon implementation except `main.cc`
into `cxl-netd-lib`; link both the `cxl-netd` executable and
`test_cxl_netd` against that library. Add `tests/tools/cxl-netd` from the top
test CMake file so the target named in this plan exists.

- [ ] **Step 4: Wrap `/dev/net/tun` with an injectable fd**

Production uses `IFF_TUN | IFF_NO_PI`, `O_NONBLOCK | O_CLOEXEC` and interface name `cxl0`. Unit tests pass one end of `socketpair(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0, fds)` so they need neither root nor a TUN device.

Reject Ethernet frames, IPv6 when the config declares IPv4-only, packets above 65,535 bytes and a runtime MTU that differs from the manifest.

- [ ] **Step 5: Implement the daemon event loop and shutdown**

The epoll loop watches the TUN fd, packet-port eventfd and signal fd. It applies
bounded per-wakeup budgets in both directions, then rearms after a second
cursor check. If CXL lacks whole-packet credit after a TUN read, retain that
packet in a bounded pending-TX queue and disable further TUN reads at the high
water mark until credit returns. If a TUN write returns `EAGAIN`, retain the
complete reassembled packet, arm write readiness and keep it at the queue head.
Retry only after write readiness, dequeue only after exactly one full-packet
write succeeds, and treat an impossible short TUN write as fatal; never discard,
duplicate or interleave it with the next packet. Queue bounds and high/low water
marks come from the manifest. On shutdown it stops new TUN reads, drains both
pending queues and published packets, emits final counters and retires its
lanes. Timeout with retained packets is a failed clean shutdown, not success.

- [ ] **Step 6: Add mandatory counters**

Expose:

```text
fdb_cxl_packets_tx
fdb_cxl_packets_rx
fdb_cxl_bytes_tx
fdb_cxl_bytes_rx
fdb_cxl_fragments_tx
fdb_cxl_fragments_rx
fdb_cxl_crc_error
fdb_cxl_drop_invalid
fdb_cxl_queue_full
fdb_cxl_pending_tx_peak_packets
fdb_cxl_pending_tun_peak_packets
fdb_cxl_partial_publish
fdb_cxl_reassembly_peak_bytes
fdb_cxl_tun_write_error
```

`fdb_cxl_packets`, `fdb_cxl_bytes` and `fdb_cxl_packet_fragments` in run
evidence are the canonical sums of successful producer-side TX publications
across directed packet lanes. The corresponding consumer RX totals must match
after a clean drain, but are reconciliation counters and are not added again.
A clean measured run requires `counter_snapshot_complete=true` for every
producer and exact TX/RX reconciliation. A crash-injection run may instead
prove the cursor-level zero-or-complete invariant while explicitly carrying an
incomplete counter snapshot; missing metrics are never coerced to zero. A Linux
TCP retransmission produces a new TUN packet and is intentionally counted as
real CXL-carrier traffic; only the sender/receiver observation of the same
packet is de-duplicated.
`fdb_cxl_partial_publish` must remain zero. Also record TUN kernel drop/error
deltas; an unexplained drop makes the run invalid even if TCP later
retransmits successfully.

- [ ] **Step 7: Run unit tests and leak checks**

Run:

```bash
cmake --build build/cxl-p2-asan --target cxl-netd test_cxl_netd
ASAN_OPTIONS=detect_leaks=1 build/cxl-p2-asan/tests/test_cxl_netd
```

Expected: PASS with zero leaks, zero unexpected packet drops and byte-identical packets.

- [ ] **Step 8: Review checkpoint**

Run `git diff --check`; confirm all packet-lane writes obey the phase-1 owner/cursor rules and leave changes uncommitted.

## Task 5: Provision isolated `cxl0` routing and prove FoundationDB compatibility

**Files:**

- Create: `deploy/cxl-riscv/cxl_net.py`
- Create: `deploy/cxl-riscv/config/fdb.cluster.in`
- Create: `deploy/cxl-riscv/config/cxl-netd.toml`
- Create: `tests/cxl_riscv/test_phase2_network_gate.py`
- Modify: `deploy/cxl-riscv/preflight.py`
- Modify: `deploy/cxl-riscv/guest_image.py`
- Modify: `deploy/cxl-riscv/kernel.fragment`
- Modify: `src/fdb/FDBConfig.h`
- Modify: `src/fdb/FDBContext.cc`

**Interfaces:**

- Produces: `cxl_net.plan(manifest) -> list[Command]` and `verify(snapshot) -> list[str]`.
- Requires: absolute `HF3FS_RISCV_FDBSERVER` and exact-compatible `HF3FS_RISCV_FDB_CLIENT` artifacts.
- Produces: FDB cluster file whose coordinator addresses are in `10.203.0.0/24` and whose checksum is recorded.
- FDB source changes are diagnostic only; they do not replace or proxy the FDB protocol.

- [ ] **Step 1: Add network-plan tests without executing privileged commands**

```python
class Phase2NetworkGateTest(unittest.TestCase):
    def test_fdb_route_is_exclusive_to_cxl0(self):
        plan = cxl_net.plan(load_tiny_manifest())
        rendered = [command.argv for command in plan]
        self.assertIn(("ip", "link", "set", "cxl0", "up"), rendered)
        self.assertIn(("ip", "route", "replace", "10.203.0.0/24", "dev", "cxl0"), rendered)

    def test_rejects_coordinator_on_eth0(self):
        errors = cxl_net.verify(snapshot_with_route("10.203.0.5", "eth0"))
        self.assertIn("FDB coordinator route does not use cxl0", errors)

    def test_rejects_live_ordinary_guest_interface(self):
        errors = cxl_net.verify(snapshot_with_up_interface("eth0"))
        self.assertIn("ordinary guest interface eth0 is not isolated", errors)

```

- [ ] **Step 2: Run the test and observe the missing network module**

Run:

```bash
python3 -m unittest -v tests/cxl_riscv/test_phase2_network_gate.py
```

Expected: FAIL with an import error for `cxl_net`.

- [ ] **Step 3: Extend the G0 preflight with hard G5 network requirements**

Revalidate the G0-pinned `fdbserver`/`libfdb_c` hashes, RISC-V ELF machine,
dynamic interpreter compatibility and FDB version rather than resolving new
artifacts. Add these kernel requirements:

```text
CONFIG_TUN=y
CONFIG_NET=y
CONFIG_INET=y
CONFIG_DEV_DAX=y
```

Require `/dev/net/tun` in the staged rootfs and `CAP_NET_ADMIN` for `cxl-netd`, supplied either by the guest init process or a file capability. Do not call `sudo` from the runner.

Also require RISC-V guest executables for `ip` and `ss` (or a BusyBox build
whose applet list proves both commands), plus readable `/proc/net/dev`,
`/proc/net/snmp` and per-process `/proc/PID/fd`. Record their ELF identity or
BusyBox binary hash in the guest manifest.

- [ ] **Step 4: Generate and verify the isolated route plan**

Use argv arrays rather than a shell:

```python
commands = [
    Command(("ip", "addr", "replace", local_cidr, "dev", "cxl0")),
    Command(("ip", "link", "set", "dev", "cxl0", "mtu", str(mtu))),
    Command(("ip", "link", "set", "cxl0", "up")),
    Command(("ip", "route", "replace", subnet, "dev", "cxl0")),
]
```

After application, parse `ip -j address show dev cxl0` and `ip -j route get 10.203.0.5`. Fail unless source, destination, MTU and device exactly match the manifest.

The runner controls guests through their serial consoles. Before publishing
`CXL_READY`, enumerate every interface other than `lo` and `cxl0`, remove its
global addresses and routes, and set it administratively down using argv-form
`ip` commands. Then require `ip -j address`/`ip -j route` evidence showing no
ordinary default or serving route. Do this only inside run-owned QEMU guests;
never alter a host interface. Keeping a QEMU NIC device attached for topology
parity does not make it an allowed carrier.

Start the FDB process only after every required packet lane and route is ready.
Bind its listen/public address to `10.203.0.5` and render the coordinator as
`10.203.0.5:4500` in `fdb.cluster`; reject a coordinator or advertised worker
address outside `10.203.0.0/24`. This ensures FDB's dynamically discovered
worker endpoints remain on the TUN-carried subnet.

The normal functional profile keeps every other interface down. Task 7's
comparison-only IO500 profile may separately provision `mpi0`; it never adds
an FDB/Core route and is outside this `cxl0` plan.

- [ ] **Step 5: Make FDB emit its selected network evidence**

At `FDBContext` startup, record the cluster-file SHA-256 and configured coordinator addresses. After the first successful transaction, emit an `fdb_network_ready` evidence event. Do not invoke socket syscalls or alter FDB addresses inside the wrapper.

- [ ] **Step 6: Boot only FDB and its client over `cxl0`**

On `vlm-server`, run from the approved superproject root:

```bash
python3 components/3FS/deploy/cxl-riscv/run_phase2.py \
  --manifest components/3FS/deploy/cxl-riscv/config/phase2-cluster.json \
  --stage fdb-smoke \
  --output components/3FS/out/cxl-riscv/phase2/runs/fdb-smoke
```

Expected evidence:

```json
{
  "status": "passed",
  "fdb_transaction_committed": true,
  "fdb_route_device": "cxl0",
  "fdb_cxl_bytes": 1,
  "forbidden_fallbacks": 0
}
```

The actual `fdb_cxl_bytes` must be greater than zero; `1` in this expected fragment denotes the minimum accepted value.

- [ ] **Step 7: Exercise FDB restart and retransmission**

Stop the FDB guest after a committed transaction, start it with a new endpoint generation, and rerun the same transaction identity. Require the client to reconnect through `cxl0`, stale packet frames to be rejected, and FDB's own retry behavior to recover without ordinary TCP traffic.

- [ ] **Step 8: Review checkpoint**

Run `git diff --check`; preserve the evidence directory, do not edit FoundationDB source, and leave 3FS changes uncommitted.

## Task 6: Add a post-readiness serving-network gate

**Files:**

- Create: `tests/cxl_riscv/test_phase2_evidence.py`
- Modify: `deploy/cxl-riscv/run_phase2.py`
- Modify: `src/common/net/cxl/CxlMetrics.h`
- Modify: `src/common/net/cxl/CxlMetrics.cc`
- Modify: `src/common/net/tcp/TcpSocket.cc`

**Interfaces:**

- Produces: per-process `CXL_READY` timestamps and network counter snapshots.
- Produces: `validate_phase2_evidence(run_dir) -> list[str]`.
- Produces: `out/cxl-riscv/phase2/source-closure.json` with schema
  `hf3fs.cxl-source-closure.v1`, `gate=phase2` and the current content-complete
  source/config hashes, including untracked files and tracked tombstones.
- Gate: `serving_tcp_bytes == 0`, `rdma_open_attempts == 0`, `fdb_cxl_bytes > 0`, `forbidden_fallbacks == 0` after the readiness barrier; a multi-rank IO500 comparison additionally records independently attributed `benchmark_control_tcp_bytes` on its manifest-bound `mpi0` network.

- [ ] **Step 1: Write evidence validator tests**

```python
class Phase2EvidenceTest(unittest.TestCase):
    def test_rejects_tcp_delta_after_ready(self):
        evidence = valid_evidence()
        evidence["network"]["serving_tcp_bytes"] = 64
        self.assertIn("serving_tcp_bytes must be zero", validate(evidence))

    def test_rejects_zero_fdb_cxl_bytes(self):
        evidence = valid_evidence()
        evidence["cxl"]["fdb_cxl_bytes"] = 0
        self.assertIn("fdb_cxl_bytes must be positive", validate(evidence))

    def test_rejects_3fs_socket_on_comparison_mpi_network(self):
        evidence = valid_multirank_evidence()
        evidence["network"]["mpi0_sockets"].append(
            {"owner": "meta_main", "bytes": 64}
        )
        self.assertIn("3FS/FDB process used mpi0", validate(evidence))
```

- [ ] **Step 2: Capture counters at the readiness barrier**

Each guest reports `/proc/net/dev`, `/proc/net/snmp`, `ss -Htnp`, route JSON and 3FS CXL counters before readiness. The controller waits for every declared endpoint and `cxl-netd`, then writes a monotonic `CXL_READY` barrier into the run manifest. It captures the same sources after workload completion and computes deltas.

- [ ] **Step 3: Define interface classification explicitly**

Classify:

```text
lo       ignored local traffic
cxl0     allowed FDB TCP/IP carried by CXL; counted in fdb_cxl counters
mpi0     comparison-only MPI/Hydra control, with pinned owners/ports/routes;
         absent and down in normal phase-2 runs
eth*, en*, tap*, usernet* forbidden for application serving after CXL_READY
```

A listening socket is not itself a failure, but every INET listener owned by a
3FS or FDB process must bind loopback or a manifest `cxl0` address. A live
ordinary guest interface other than comparison-owned `mpi0`, ordinary serving
route, forbidden bind, or positive `mpi0` payload delta attributed to a
3FS/FDB process is a failure. Only pinned MPI/Hydra owners and ports may use
`mpi0`. Correlate socket inodes with `/proc/PID/fd` and process IDs captured by
the runner. The combination of route isolation, socket ownership, 3FS
instrumentation and positive CXL counters is the proof; interface counters
alone are not treated as attribution.

Count an FDB packet once at the local netd producer and keep RX only as a
reconciliation count. Count `mpi0` bytes independently from interface and
socket-owner deltas. TCP retransmissions are real carried packets and remain
in their originating class. Require classified bytes plus explicit protocol
overhead to reconcile with totals; unknown application traffic fails closed.

- [ ] **Step 4: Instrument 3FS TCP sends as a second independent check**

`TcpSocket` increments `serving_tcp_bytes` only when a 3FS request or response is written after the local readiness barrier. Bootstrap and orchestration processes do not share this counter. Any increment also increments `forbidden_fallbacks` in a phase-2 build.

- [ ] **Step 5: Prove the validator rejects injected ordinary TCP**

Add a runner-only fault mode that temporarily configures a run-owned guest's
ordinary NIC and sends a tagged 64-byte application probe over it. The phase-2
evidence test must reject that run for both lost isolation and payload. The
runner tears down the injected address, then a fresh session without the fault
must pass. No host interface is changed.

- [ ] **Step 6: Run validator tests**

Run:

```bash
python3 -m unittest -v \
  tests/cxl_riscv/test_phase2_network_gate.py \
  tests/cxl_riscv/test_phase2_evidence.py
python3 tests/cxl_riscv/test_phase2_source_closure.py \
  --write-evidence out/cxl-riscv/phase2/source-closure.json
```

Expected: PASS, including negative fixtures.

The phase-2 source identity uses the same canonical snapshot algorithm as the
G0 remote synchronizer and must equal its final remote receipt. Reject a
closure based only on `git diff`/`git status`, because this uncommitted workflow
contains newly created, untracked transport sources.

- [ ] **Step 7: Review checkpoint**

Run `git diff --check`; verify that no firewall setup is needed to make a passing fixture pass and leave changes uncommitted.

## Task 7: Run the complete phase-2 RISC-V cluster and fault matrix

**Files:**

- Create: `deploy/cxl-riscv/run_phase2.py`
- Modify: `deploy/cxl-riscv/guest_image.py`
- Modify: `deploy/cxl-riscv/config/phase2-cluster.json`
- Modify: `tests/cxl_riscv/test_phase2_evidence.py`

**Interfaces:**

- Produces: `run.json`, `manifest.bin`, per-guest logs/counters, route snapshots, process exit records, filesystem validation and CXLMemSim evidence beneath `components/3FS/out/cxl-riscv/phase2/runs/`.
- Accepts an `io500` stage with absolute config, `--client-ranks 1|2|4`,
  immutable resource envelope, platform contract/backing policy, seed and
  measurement marker, plus `--cache-state cold|preconditioned` and
  `--warmup-iterations` and `--result-class qualification|measured`. It derives the static manifest before launch and
  records exact IO500/MPI argv, hashes and realized measurement boundaries.
- Consumes: G0 guest images, phase-1 3FS binaries, phase-2 `cxl-netd`, RISC-V FDB artifacts and parent platform binaries.
- Pass condition: every required phase-2 evidence invariant holds in one full client-visible filesystem run.

- [ ] **Step 1: Unit-test command generation and output confinement**

Assert that every runner-generated image, log and evidence path resolves beneath
`components/3FS/out/cxl-riscv/phase2/runs`, all build paths resolve beneath
`components/3FS/build`, all parent artifacts are opened read-only, all guest
endpoint IDs come from the manifest and no command contains an RDMA device or
TCP bootstrap flag.

For an IO500 command fixture, also assert one rank per independently derived
client guest, exact aggregate vCPU/RAM/storage/shared-fabric totals from the
resource envelope, and a run-owned `mpi0` path whose QEMU socket/multicast,
MTU, rank placement and Hydra argv match the comparison manifest. Assert that
no Core/FDB/3FS address or process can route or bind through `mpi0`. Assert the
rendered command contains the exact cache state, warm-up count and measurement
marker selected by the immutable experiment profile.

The runtime order is fixed: start `cxl-fabricd` and observe the exact
session/layout READY record; start all manifest netds and configure `cxl0`;
start `fdbserver` and prove its endpoint; then start mgmtd, meta, storage and
clients. Shutdown first tells applications to stop new submissions, asks
fabricd to publish `DRAINING`, and waits while live endpoints drain and
acknowledge. It then stops application/netd processes in reverse dependency
order and accepts fabricd's `RETIRED` before the authority exits. Authority
exit at any earlier point fails the run and permits only a fresh-session
restart.

After readiness, a `preconditioned` run executes its declared unmeasured
workloads in a run-owned namespace while retaining the same QEMU guests,
CXLMemSim process, CXL session and storage backing. Every warm-up must validate,
drain all RPC/TUN queues and remove only its run-owned namespace. The runner
then snapshots CXL/TUN/network/simulator/process counters, publishes the common
manifest-bound measurement marker to all participants, runs the measurement
and captures matching end snapshots. Guest boot IDs, CXL session generation
and endpoint generations prove cohort identity. A warm-up failure invalidates
the slot before measurement.

A `cold` run requires zero warm-ups, a fresh session and never-before-used
backing files created before launch. Reject inconsistent cache-state/count
pairs, and never aggregate cold with preconditioned evidence.

- [ ] **Step 2: Build the phase-2 RISC-V artifacts**

On `vlm-server`, from the approved superproject root:

```bash
python3 components/3FS/deploy/cxl-riscv/build_riscv.py \
  --preflight components/3FS/out/cxl-riscv/preflight.json \
  --profile full-3fs \
  --source-closure components/3FS/out/cxl-riscv/phase2/source-closure.json \
  --dependency-contract components/3FS/deploy/cxl-riscv/full-deps.json \
  --build-dir components/3FS/build/cxl-riscv-phase2 \
  --cmake-option HF3FS_ENABLE_CXL_NETD=ON \
  --cmake-option HF3FS_CXL_TCP_BOOTSTRAP=OFF \
  --extra-target cxl-netd
```

Expected: `mgmtd_main`, `meta_main`, `storage_main`, `hf3fs_fuse_main`, `cxl-netd` and the smoke clients are RISC-V ELF binaries and none has a `NEEDED` entry for `libibverbs`.

- [ ] **Step 3: Run the readiness and Core smoke**

```bash
python3 components/3FS/deploy/cxl-riscv/run_phase2.py \
  --manifest components/3FS/deploy/cxl-riscv/config/phase2-cluster.json \
  --stage core-smoke \
  --output components/3FS/out/cxl-riscv/phase2/runs/core-smoke
```

Expected: Echo, GetConfig and one management discovery request pass over CXL; `serving_tcp_bytes`, `rdma_open_attempts` and `forbidden_fallbacks` are zero.

- [ ] **Step 4: Run a client-visible filesystem smoke**

```bash
python3 components/3FS/deploy/cxl-riscv/run_phase2.py \
  --manifest components/3FS/deploy/cxl-riscv/config/phase2-cluster.json \
  --stage filesystem-smoke \
  --output components/3FS/out/cxl-riscv/phase2/runs/filesystem-smoke
```

The stage creates, writes, fsyncs, reads, hashes, renames and removes deterministic files through the FUSE mount. It performs metadata mutations that require FoundationDB and at least one storage read/write.

Expected: filesystem validation PASS, positive CXL RPC/bulk and FDB byte counts, zero forbidden paths and no pending request at shutdown.

- [ ] **Step 5: Execute the phase-2 fault matrix**

Run one case per fresh session epoch:

```text
restart Core peer before publish
restart Core peer after the request range is cursor-published but before its
  end reaches the trustworthy peer-delivered watermark
restart cxl-netd during an FDB transaction
restart fdbserver and reconnect through cxl0
corrupt one packet checksum
delay one packet consumer until queue-full backpressure
change one endpoint generation while an old frame remains
inject ordinary serving TCP as a negative control
```

Every non-negative case must either complete or return its specified
`CompletionDisposition::RejectedBeforeExecute`,
`CompletionDisposition::OutcomeUnknown` or
`CompletionDisposition::LaneRetired` together with the normal status, without
reuse corruption. Expectations are based on the phase-1 request byte range and
peer-delivered watermark: `LaneRetired` is used only when that snapshot is
untrustworthy, not for every forced close. The negative control must be
rejected by the evidence gate.

- [ ] **Step 6: Exercise the comparison-facing IO500 entry point in print-only mode**

Unit tests create temporary IO500 and resource-envelope inputs, then render:

```text
run_phase2.py --stage io500 --client-ranks 4
  --io500-config <absolute-path> --resource-envelope <absolute-path>
  --platform-contract <absolute-path> --endpoint-backing-policy all-distinct
  --cache-state preconditioned --warmup-iterations 1
  --result-class measured
  --seed 20260904 --measurement-marker dry-run --output <run-owned-path>
  --print-only
```

Require four client guests/ranks, deterministic disjoint endpoint IDs, the
same normalized manifest regardless of whether RF or client derivation is
applied first, a benchmark-only `mpi0` on client guests, and no launched
process. The real comparison plan supplies the checked-in IO500 configs and
frozen envelope later.

Normal runs require the G0-B `all-distinct` platform contract. Comparison may
use `all-shared` only to match a realized LegoFS policy, with all other
normalized fields byte-identical to G0-B and a fresh bidirectional DAX
checksum/trace smoke before IO500. Reject `mixed` backing identities.

- [ ] **Step 7: Verify the final evidence bundle**

Run:

```bash
python3 -m unittest -v tests/cxl_riscv/test_phase2_evidence.py
python3 components/3FS/deploy/cxl-riscv/run_phase2.py \
  --validate-only components/3FS/out/cxl-riscv/phase2/runs/filesystem-smoke/run.json
```

Expected:

```text
status=passed
filesystem_valid=true
cxl_rpc_bytes>0
cxl_bulk_read_bytes+cxl_bulk_write_bytes>0
fdb_cxl_bytes>0
benchmark_control_tcp_bytes>0 when client_ranks>1
io500_clean_valid=true in a measured IO500 stage
qualification_semantics_passed=true and performance omitted in a
  qualification-only IO500 stage
actual resource totals == frozen resource envelope
serving_tcp_bytes=0
rdma_open_attempts=0
forbidden_fallbacks=0
pending_requests=0
```

- [ ] **Step 8: Final source and dependency audit**

Run from the approved superproject root:

```bash
rg -n 'Address::TCP|TcpSocket|CxlConnectService' \
  components/3FS/src/core components/3FS/src/mgmtd components/3FS/src/meta \
  components/3FS/src/storage components/3FS/src/client
python3 -m unittest -v components/3FS/tests/cxl_riscv/test_phase2_source_closure.py
! find components/3FS/build/cxl-riscv-phase2 -type f -perm -111 -print0 | \
  xargs -0 -r riscv64-linux-gnu-readelf -d | rg 'libibverbs|librdmacm'
git -C components/3FS diff --check
git -C components/3FS status --short
```

Expected: no unguarded serving TCP references, no RDMA dynamic dependency and no whitespace errors. All edits remain inside `components/3FS` and remain uncommitted.

## Phase 2 completion gate

Phase 2 is complete only when all of the following are true:

- static rendezvous passes restart, corruption, ownership and generation tests;
- Core smoke succeeds with TCP socket creation denied;
- a real compatible RISC-V `fdbserver` and `libfdb_c` commit a transaction over `cxl0`;
- a FUSE filesystem smoke exercises Core, metadata, FoundationDB and storage through CXL;
- the post-readiness gate reports zero ordinary serving TCP, zero RDMA opens and zero fallback;
- every required CXL path has positive guest-visible byte evidence;
- parent QEMU/Linux/CXLMemSim and LegoFS sources are unchanged;
- `git diff --check` passes and no commit has been created.
