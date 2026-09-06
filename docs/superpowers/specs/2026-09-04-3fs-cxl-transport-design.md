# 3FS CXL Transport Design

Date: 2026-09-04
Status: Phase-1 migration and functional qualification passed; 10c1s IO500 standard measurement remains open
Scope owner: `components/3FS`

Current user scope: finish phase-1 migration and measure the 10c1s standard
IO500 test, close failures with timing evidence and root causes, then stop. Interpret 10c1s as ten client guests and one server guest using the
existing RISC-V QEMU/CXLMemSim platform. Machine-local FoundationDB remains on
TCP and is excluded from migration. Do not start phase 2 or the later
cross-system performance comparison as part of this task. This instruction
supersedes the FoundationDB cutover described in the earlier phase-2 design.

Acceptance evidence (2026-09-06 Asia/Shanghai): source closure 029/build 027
passes G0-B and the ten-client/one-server RISC-V guest qualification in
`out/cxl-riscv/phase1/10c1s/20260905T203556.712060Z-2074312/result.json`.
All ten independent FUSE clients pass 2 MiB direct writes with fsync, direct
readback/byte comparison/SHA-256, and another client's read/hash check.
The final result is `passed`, with `HF3FS_CXL_10C1S_OK`, no validation errors,
clean application retirement and all owned platform processes stopped.
The [fixture plan](../plans/2026-09-05-3fs-cxl-test-fixtures.md#accepted-phase-1-and-10c1s-result)
records exact transport counters, source/build/G0 identities, native/formal
coverage, retained failures and limitations. This is functional qualification
on the existing QEMU/CXLMemSim platform, not physical CXL or a standard IO500
measurement. The earlier completion claim was premature. Remaining work follows
the [IO500 standard plan](../plans/2026-09-06-3fs-cxl-io500-standard.md):
300-second stonewall, all 13 phases, one rank per client guest, official verifier,
and guest/host timing closure. The functional result remains unchanged. Core/bootstrap and machine-local FDB remain TCP.
Phase 2 has not started; no commits or platform changes were made.

## 1. Decisions and constraints

This design records the following approved decisions:

- 3FS clients and services run as native RISC-V processes inside QEMU guests.
- Guests access CXL through `/dev/dax0.0` using the existing
  `cxlmemsim-riscv + QEMU` functional platform.
- Source changes stay under `components/3FS`; LegoFS and the other top-level
  components are reference inputs and are not modified.
- The current task replaces every original 3FS RDMA path with CXL while
  retaining existing TCP paths. Machine-local FoundationDB stays on TCP.
  A later Core/TCP migration requires a separate task; it is outside this run.
- No RDMA device is available or required for correctness testing.
- The RISC-V build defaults to CXL enabled and RDMA disabled and does not link
  `libibverbs`.
- CXL failures must fail closed. A CXL configuration must never silently fall
  back to RDMA or TCP.
- Existing RDMA source may remain behind a disabled build option for audit and
  source comparison, but it is absent from the RISC-V runtime image.
- Do not commit or push. All implementation and evidence changes remain
  uncommitted until the user gives a separate explicit instruction.

## 2. Audit summary

### 2.1 3FS has two distinct RDMA roles

The current implementation does not have a single replaceable RDMA call site.
It uses RDMA in two layers:

1. `IBSocket` implements the `Socket` byte-stream contract with SEND/RECV and
   feeds the generic framed RPC path in `Transport`.
2. Storage requests serialize `RDMARemoteBuf` descriptors. Storage servers use
   one-sided RDMA READ to pull writes and RDMA WRITE to push reads.

Replacing only `IBSocket` would leave the storage data path dependent on
registered RDMA memory.

### 2.2 Replication carries RDMA state across hops

The storage write path receives client data into a server-owned registered
buffer and passes a descriptor for that buffer to the next replica through
`ReliableForwarding`. Resync uses the same general mechanism. The CXL design
therefore covers all of these paths:

- client to storage head;
- storage head to successor;
- additional successors;
- storage resync;
- batch-read responses from storage to client.

### 2.3 RDMA types leak through otherwise generic layers

The main coupling points are:

- `common/net/Transport.*`: construction, connection, shutdown and
  `ibSocket()` access;
- `common/net/IOWorker.*` and `Listener.*`: RDMA-specific timeouts, event
  handling and TCP bootstrap;
- `common/net/TransportPool.*`: connections are keyed only by address, while a
  CXL carrier must retain an explicit Control/Data service plane;
- `common/serde/Services.h` and `Processor.h`: the service plane is represented
  by a boolean `isRDMA`;
- `common/net/RDMAControl.*`, `Client.h`, `RequestOptions.h`, `Waiter.h` and
  `common/serde/ClientContext.*`: delayed bulk-transfer admission is named and
  wired as RDMA even though its ordering role is transport-independent;
- `common/serde/MessagePacket.h` and `CallContext.*`: the control flag and
  `RDMATransmission` directly name RDMA/verbs;
- `client/storage/StorageClient.*`: public `IOBuffer` is an `RDMABuf`;
- FUSE buffer pools and admin commands (`FillZero`, `QueryChunk`, `ReadBench`
  and `FileWrapper`) allocate `RDMABuf` directly;
- `fbs/storage/Common.h`: read and update payloads embed
  `RDMARemoteBuf`;
- `storage/service/StorageOperator.*`, `ReliableForwarding.*`, `BufferPool.*`
  and sync code: direct `IBSocket`, RDMA buffer and device references;
- application, launcher, FUSE and admin entry points: unconditional
  `IBManager` configuration/start/stop;
- `StatusCodeDetails.h` and Meta retry classification: IB/RDMA-specific error
  symbols escape the backend;
- `common/CMakeLists.txt`: `ibverbs` is linked unconditionally.

### 2.4 Existing TCP and FoundationDB boundaries

Mgmtd, Meta and Storage each expose a primary service group that defaults to
RDMA and a Core group that uses TCP. FoundationDB traffic does not pass through
the 3FS `Transport` abstraction: `FDBContext` delegates network setup and the
network thread to the external `libfdb_c` implementation. Moving FDB traffic
to CXL without changing FoundationDB requires a transparent network carrier.

### 2.5 Current remote feasibility state

The remote experiment host already contains built QEMU/CXLMemSim artifacts,
but the complete 3FS RISC-V userspace is not yet present:

- the remote superproject does not currently contain `components/3FS`;
- the local 3FS checkout has fourteen uninitialized nested submodules;
- the normal remote PATH currently has a RISC-V C compiler and linker but no
  `riscv64-linux-gnu-g++` or RISC-V pkg-config wrapper;
- FoundationDB client/server packages were not found in the remote package
  query;
- 3FS selects C API version 710 in `src/fdb/FDB.h`; its current development
  images install FoundationDB 7.3.63, while the otherwise unused top-level
  `FDB_VERSION` variable says `7.1.5-ibe`. G0 must freeze one matching
  server/client artifact identity instead of inferring compatibility from that
  stale variable;
- the current Linux config fragment enables DAX, devdax, networking, ext4 and
  virtio-blk, but does not advertise FUSE or TUN in that fragment.

These are build and guest-image gates, not changes to the selected CXL
hardware model.

## 3. Goals and non-goals

### 3.1 Goals

- Preserve 3FS RPC framing, serde dispatch, retry identities, storage checksums
  and chain-replication semantics.
- Replace all application-visible RDMA operations with coherent load/store
  access to a shared CXL region.
- Integrate with the existing event-driven `Transport` and `IOWorker` design.
- Support small RPCs and the existing 512 MiB maximum framed message.
- Define explicit buffer ownership, generation, timeout and crash-recovery
  rules.
- Allow correctness testing on a file-backed mmap before running multi-guest
  DAX tests.
- Produce fail-closed evidence proving which traffic used CXL.
- Compare 3FS-CXL and LegoFS-CXL under the same QEMU/CXLMemSim configuration
  without presenting functional-simulator wall-clock results as physical CXL
  performance.

### 3.2 Non-goals

- Implement CXL wire protocol, electrical timing or a physical-device driver.
- Treat the CXL transport region as the durable 3FS storage medium.
- Reimplement FoundationDB transaction semantics.
- Modify LegoFS, QEMU, Linux or CXLMemSim source during the initial work.
- Claim compatibility between CXL and an existing live RDMA cluster.
- Use an RDMA run as a prerequisite for migration validation.

## 4. Architecture

The selected approach is a transport-neutral refactor followed by a native
CXL backend. A compatibility shim that pretends CXL objects are RDMA objects is
rejected because it would preserve incorrect address/rkey semantics. A second,
parallel RPC stack is rejected because it would duplicate serde, retries,
timeouts and service dispatch.

```text
3FS serde / storage / replication
              |
     +--------+---------+
     |                  |
MessageTransport   BulkTransfer
     |                  |
 CxlSocket         CxlBulkTransfer
     +--------+---------+
              |
        CxlFabric/Region
              |
        /dev/dax0.0
              |
       QEMU + CXLMemSim
```

### 4.1 Common types

The refactor introduces explicit concepts instead of RDMA predicates:

```cpp
enum class TransportKind { TCP, RDMA, CXL };
enum class ServicePlane { Control, Data };

struct ServiceEndpoint {
  Address address;
  ServicePlane plane;
};
```

The common interfaces are:

- `TransportRuntime`: process-scoped startup/shutdown for the compiled data
  plane backends;
- `CxlFabric`: the process owner of one validated mapping, local endpoint,
  progress engine, lane authority and bulk arena;
- `MessageTransport`: non-blocking framed byte-stream transport;
- `SharedBuffer`: a local buffer with optional remotely visible storage;
- `RemoteBufferHandle`: a validated, transport-tagged remote reference;
- `CxlAllocationRecord`: an owner-written export-directory entry indexed by a
  remote handle, so exported subranges retain an unambiguous allocation base,
  length and generation;
- `RemoteExportLease`: request-scoped ownership that prevents an exported
  allocation from being reclaimed while a peer may still access it;
- `BulkTransfer`: `pull`, `push` and batched transfer operations;
- `BulkTransmission`: the transport-neutral replacement for
  `RDMATransmission`.
- `BulkControl`: the transport-neutral admission/ack service that preserves
  the current delayed-transfer limiter and request-identity ordering.
- `PublicationSnapshot`: an optional byte-stream progress view containing the
  lane generation and accepted, cursor-published and peer-delivered byte
  watermarks; TCP and RDMA may report it as unavailable.
- `CompletionDisposition`: `Completed`, `RejectedBeforeExecute`,
  `OutcomeUnknown` or `LaneRetired`, used independently of the transport's
  status code.

`Address::CXL` is appended to the existing address enum to preserve the
numeric values of existing address types. The serialized address remains eight
bytes.

### 4.2 Service planes

The existing two service tables are retained conceptually but indexed by
`ServicePlane`, not `bool isRDMA`. Plane is an explicit listener/connection/lane
property; it is not derived from `TransportKind`, because CXL carries Data
services in phase 1 and both Core Control and Data services in phase 2.

Each service registers in exactly the planes it declares. A Data service is
not automatically copied into the Control table. The CXL bootstrap service is
Control-only, and its TCP connection cannot dispatch Data service IDs. The
client connection key includes both address and plane, and every CXL connect
request or static lane declares its plane. In phase 1:

- CXL replaces RDMA in the Data plane;
- existing TCP Core services remain in the Control plane.

In phase 2, Core's CXL lanes remain in the Control plane while the former RDMA
services remain in the Data plane. This prevents adding a large service table
per address type, keeps dispatch independent of carrier and closes an implicit
TCP fallback path.

### 4.3 Build modes

The build exposes:

```text
HF3FS_ENABLE_CXL=ON
HF3FS_ENABLE_RDMA=OFF
HF3FS_ENABLE_CXL_NETD=OFF   # enabled in phase 2
```

A CXL-only build neither finds nor links `ibverbs`. RDMA-specific source and
tests are excluded. A source-audit build may enable both backends on a machine
that has the dependencies, but no runtime fallback exists between them.

`TransportRuntime` replaces unconditional `IBManager` startup in server,
FUSE and admin entry points. It initializes `CxlFabric` before any client or
server is constructed and shuts it down only after transports drain. The local
launcher config therefore contains the DAX path/range, participant identity,
generation and layout identity needed before remote configuration can be
fetched. One process maps the region once and shares that mapping/progress
engine across all of its I/O workers.

## 5. Migration phases

### 5.1 Phase 1: replace RDMA and retain TCP

All service groups and clients that currently select RDMA switch to CXL:

- Mgmtd;
- MetaSerde;
- StorageSerde;
- FUSE, meta and storage clients;
- admin clients that currently select RDMA;
- storage bulk read/write;
- chain forwarding and resync.

TCP remains for:

- Core services;
- FoundationDB;
- CXL lane bootstrap.

The bootstrap socket carries only connection metadata. Once a CXL lane reaches
`READY`, no serving RPC or payload on that connection may use its bootstrap
socket.

### 5.2 Phase 2: move the remaining TCP serving traffic

Phase 2 performs three changes:

1. Core services move directly to `CxlSocket`.
2. Static CXL rendezvous replaces TCP lane bootstrap.
3. A CXL virtual network device carries FoundationDB's unmodified TCP/IP
   protocol between guests.

QEMU user networking may remain for experiment orchestration, console access
and evidence collection, but it is outside the application serving path.
Monitoring/ClickHouse traffic is disabled during comparative benchmarks.

## 6. Shared-region ABI

### 6.1 Addressing

Every participating process maps its guest's `/dev/dax0.0`. Virtual addresses
may differ. Every process that writes shared control state has a unique
participant/endpoint ID even when several processes run in one guest. Guest
identity and `cxl0` IP assignment are separate from this writer identity.
Every shared reference uses a logical region and offset; virtual pointers are
never serialized.

```text
region identity + DAX offset + length + generations
```

The runner emits a layout manifest consumed by every participant. Region sizes
and offsets are not hard-coded in the C++ implementation.

### 6.2 Region layout

```text
+-----------------------------+
| 4 KiB superblock            |
+-----------------------------+
| endpoint directory          |
+-----------------------------+
| lane directory              |
+-----------------------------+
| owner-separated cursor pages|
+-----------------------------+
| SQE/CQE rings               |
+-----------------------------+
| RPC payload cells           |
+-----------------------------+
| bulk allocation records     |
+-----------------------------+
| endpoint-owned bulk arenas  |
+-----------------------------+
| evidence and counters       |
+-----------------------------+
```

The ABI-v1 superblock is exactly 4 KiB. Its first 128 bytes encode:

- `HF3FSCXL` magic, ABI major/minor and superblock size;
- total region size and every subregion range;
- cache-line size and little-endian marker;
- session generation;
- endpoint and lane counts;
- the binary SHA-256 of the canonical layout manifest and superblock CRC32C;
- the offset of an authority-owned 64-byte lifecycle record.

The header is followed by exactly eight 24-byte range entries in numeric kind
order: endpoint directory, lane directory, cursor pages, frame rings, RPC
payload cells, bulk allocation records, bulk arenas, and evidence/counters.
Each entry contains `u16 kind`, `u16 flags`, zero `u32 reserved`, `u64 offset`
and `u64 length`. ABI v1 requires flags zero. The remaining 3,776 bytes are
zero. CRC32C covers the complete 4 KiB little-endian encoding with the CRC
field zero. Unknown kinds, count/order changes or any nonzero reserved byte are
rejected rather than inferred.

An attacher first copies the fixed 4 KiB from offset zero and validates its
magic, expected manifest/session, CRC and every checked range. It must not
dereference `lifecycle_record_offset` or another encoded offset before that
validation. Only then does it acquire-read the lifecycle record and require a
matching stable `READY` generation; a concurrent partial format therefore
causes a bounded retry, never an unchecked pointer access.

All offsets, lengths, additions, alignments and region relationships are
validated before a process publishes readiness.

The immutable manifest names exactly one live `cxl-fabricd` process endpoint
as the fabric authority for the session. It is colocated with the mgmtd guest
for filesystem scenarios (or the server guest for the isolated Echo case),
has no Core/FDB dependency and owns no serving lane. Only that endpoint may
format the superblock and transition the separate lifecycle record:

```text
uninitialized -> INIT -> READY -> DRAINING -> RETIRED
```

On a fresh runner-owned backing set, the authority maps DAX, verifies that no
different live session exists, writes the canonical immutable layout and its
CRC, initializes only authority-owned global words, then release-publishes
`READY`. Every other process uses attach-only mode and acquire-waits for the
exact session/layout identity; it never formats or repairs shared state. Each
endpoint separately initializes only its own cursor, state, allocator and
counter pages before declaring endpoint readiness. Ring/payload bytes may be
poisoned because no consumer trusts them before a matching published cursor,
generation and CRC.

The authority remains alive and is the sole writer of the global lifecycle.
It enters `DRAINING` only after new submissions are disabled, and `RETIRED`
only after all declared endpoints have drained/acknowledged. Initial phase
implementations do not elect a replacement authority: authority death faults
the whole session, preserves evidence and requires a fresh epoch plus fresh
run-owned backing files. This keeps initialization and retirement free of
cross-endpoint RMW or ambiguous takeover.

The mutable lifecycle record is cache-line-separated from the immutable
superblock bytes:

```text
u64  record_sequence
u64  session_generation
u64  authority_generation
u64  heartbeat
u32  authority_endpoint
u32  superblock_crc32c
u32  lifecycle
u32  record_crc32c
u8   reserved[16]
```

It uses the same owner-written odd/even snapshot protocol as endpoint records.
The immutable superblock CRC excludes this record and never changes after
publication; the lifecycle record has its own CRC with `record_crc32c=0` and
reserved bytes zero. Attach requires both the immutable layout identity and a
stable `READY` lifecycle snapshot. This prevents heartbeat or drain updates
from invalidating the layout and prevents immutable/mutable false sharing.

Every manifest-declared process, including `cxl-fabricd`, also owns one
cache-line-separated endpoint record from phase 1 onward:

```text
u64  record_sequence
u64  session_generation
u32  endpoint_id
u32  lifecycle
u64  endpoint_generation
u64  capability_bits
u64  heartbeat
u32  record_crc32c
u32  reserved0
u64  reserved1
```

`endpoint_generation` is 64-bit and is the same identity carried as
`owner_generation` in `RemoteBufferHandle`. Endpoint lifecycle is `FREE ->
STARTING -> READY -> DRAINING -> RETIRED`, with `FAULTED` reachable from any
live state. Only the named endpoint process writes its record. It uses the
same final-even sequence/CRC protocol, increments heartbeat from a local
counter without shared RMW, and sets reserved fields to zero. Phase 1 uses the
record for membership, failure detection and authority drain acknowledgements;
phase 2 reuses it for static rendezvous rather than defining a new ABI.

### 6.3 Lane organization

Each logical connection owns one duplex SPSC lane:

```text
requester                         acceptor
SQ producer -------------------> SQ consumer
CQ consumer <------------------- CQ producer
```

The `Transport` MPSC write queue remains the multi-caller boundary. One
`IOWorker` exclusively calls a given `CxlSocket`'s `send`/`flush`, and one read
task exclusively calls its `recv`; the progress engine observes cursors and
signals local eventfds but never produces or consumes lane bytes. Construction
records this worker ownership and debug/test builds reject a cross-worker call.

Defaults align with the LegoFS transport where practical:

- ring depth: 64;
- SQE/CQE size: 64 bytes;
- cache line: 64 bytes;
- each independently written cursor on a separate 4 KiB page;
- RPC payload cell: 64 KiB, configurable through the manifest.

Every cursor has one writer. Cross-endpoint locks and atomic read-modify-write
operations are not required on the hot path.

Each direction also has a consumer-owned 64-byte delivered record on a
separate cache line in that consumer's cursor page:

```text
u64  record_sequence
u64  session_generation
u64  lane_generation
u64  delivered_offset
u64  delivered_offset_complement
u32  crc32c
u32  reserved0
u64  reserved1
u64  reserved2
```

The consumer publishes it with the owner-written odd/even sequence protocol
after copying a contiguous stream prefix to the caller and before `recv()`
returns. The complement must equal `~delivered_offset`, the CRC covers the
complete final-even 64-byte encoding with `crc32c=0`, and reserved fields are
zero. The record may advance within a ring cell; it never controls slot reuse.
The producer reads it only for request publication/failure classification. Its
offset starts at zero, is monotonic within the lane generation, cannot exceed
the published stream offset, and has exactly one writer. A torn, stale-generation
or corrupt record is untrustworthy rather than evidence that a request was not
delivered.

Each lane also has two owner-separated 64-byte state records, one written by
the requester and one by the acceptor:

```text
u64  record_sequence
u64  session_generation
u64  lane_generation
u128 connection_nonce
u64  capability_bits
u32  lifecycle
u32  crc32c
u64  endpoint_generation
```

The immutable lane slot supplies lane ID, allocation authority and every byte
range. Phase 1 binds that slot to endpoint/service/plane identities in the
validated bootstrap result; phase 2 supplies the binding in its immutable
manifest. Each owner record uses the sequence/CRC snapshot protocol defined
below. Readiness requires each side's endpoint generation to match its stable
endpoint record and both records to match that binding exactly; neither side
writes its peer's record.

### 6.4 Frame entry

SQ and CQ use the same fixed-width, little-endian 64-byte frame entry:

```text
u64  absolute_sequence
u64  session_generation
u64  lane_generation
u64  stream_offset
u64  payload_offset
u32  payload_length
u32  flags             # DATA, ERROR, RETIRE
u32  crc32c
u32  reserved0
u64  reserved1
```

The queue is full when `producer - consumer == depth`. The producer must apply
backpressure and must not overwrite a slot. A cell cannot be reused until the
consumer advances past it.

`crc32c` is Castagnoli CRC32C over the 64-byte little-endian entry with its CRC
field encoded as zero, followed by exactly `payload_length` stream bytes.
This protects both descriptor and cell contents; the existing whole-message
checksum remains an independent end-to-end check after `Transport` reconstructs
the message. Reserved fields must be zero.

Absolute cursors do not wrap within one lane generation. Values with
`consumer > producer` or `producer - consumer > depth` fault the lane. Before
an increment could overflow `uint64_t`, submission stops, the lane drains, a
fresh nonzero lane incarnation is allocated, and that incarnation's absolute
cursors start at zero. Ring-index wrap is tested separately from
absolute-cursor exhaustion.

### 6.5 Byte-stream segmentation

`Socket::send()` receives arbitrary iovec batches, and `Transport::writeAll()`
may retry an arbitrary suffix. A socket therefore cannot assume that one call
contains one RPC or even one complete `MessageHeader`. `CxlSocket` preserves
the same ordered byte-stream contract as `TcpSocket` and `IBSocket`; it does
not duplicate an RPC UUID or message boundary in `CxlFrameEntry`.

The sole producer coalesces accepted bytes into a 64 KiB default cell. Before
accepting the first byte of a staged cell, it reserves one currently free ring
slot locally; a later `flush()` can therefore never discover that the cell has
no credit. A full cell is published immediately, and `flush()` publishes a
non-empty reserved cell. `send()` returns only bytes copied into such reserved
or already published cells. The receiver concatenates published cells and may
return a partial cell from `recv()` while preserving the unread suffix.

`stream_offset` is the absolute offset of the first payload byte within one
lane generation and direction. It starts at zero, advances exactly by
`payload_length`, and never wraps within that generation. A missing,
duplicated, reordered, overlapping or gapped stream segment faults the lane.
The queue `absolute_sequence` independently protects slot order. For a `DATA`
entry, `payload_offset` must equal the immutable layout's cell offset for
`absolute_sequence % depth`, not merely point somewhere inside the lane.
Reject a segment before publication if advancing either the stream offset or
the absolute cursor would overflow. Control flags are mutually exclusive,
carry zero payload and use the current stream offset.

The existing `Transport` layer reconstructs messages, applies the
`MessageHeader` checksum and enforces the 512 MiB maximum. The existing 64-bit
RPC UUID remains in serialized `MessagePacket`; higher-layer bulk control and
lease reconciliation continue to bind to that UUID without exposing it to
`CxlSocket`.

`CxlSocket::publicationSnapshot()` exposes process-local `accepted_offset` and
`published_offset` plus the acquire-loaded peer `delivered_offset`, all bound
to one lane generation. A trustworthy snapshot satisfies
`peer_delivered_offset <= published_offset <= accepted_offset`, and all three
offsets are monotonic and non-wrapping. `Transport` maps each ordered
`WriteItem` byte range
to these offsets and retains a small publication receipt after the serialized
buffer leaves the active write list. A request item whose end is still above
`peer_delivered_offset` remains recoverable for retry after a trustworthy lane
retirement; its buffer is not destroyed merely because `send()` copied or
cursor-published it. A final response or peer delivery through the range end
permits the serialized request buffer to be released, while its request
identity and any export leases follow the longer reconciliation lifetime.
Non-request response buffers may be released after publication. Frames may
coalesce bytes from adjacent RPCs because classification uses byte ranges, not
frame boundaries.

### 6.6 Remote buffer handle

`RemoteBufferHandle` is a fixed 64-byte value with natural 64-bit alignment.
It is copied into RPC values and coroutine frames, so it must not inherit the
cache-line alignment required by shared fabric records. Natural alignment
preserves the 64-byte wire size and all field offsets, with this logical content:

```text
u16 abi_version
u8  transport_kind
u8  permissions
u32 owner_endpoint
u32 arena_id
u32 allocation_slot
u64 session_generation
u64 owner_generation
u64 allocation_generation
u64 offset
u64 length
u32 checksum
u32 reserved0
```

Before access, the receiver verifies:

1. addition and range calculations do not overflow;
2. `allocation_slot` selects a valid owner/arena export-directory record;
3. the requested subrange is inside that record's allocation base/length, the
   declared arena and the mapped region;
4. session, endpoint and allocation generations match;
5. the operation permission is present in both the handle and active record;
6. the allocation has not entered retirement or been reclaimed;
7. reserved fields and descriptor checksum are valid.

The export directory contains one cache-line-separated record per allocation
slot:

```text
u64  record_sequence
u64  session_generation
u64  owner_generation
u64  allocation_generation
u64  allocation_base_offset
u64  allocation_length
u32  owner_endpoint
u32  arena_id
u32  state_and_permissions  # FREE, EXPORTED, RETIRING; READ/WRITE bits
u32  record_crc32c
```

Only the arena owner writes a record, using the same final-even sequence/CRC
protocol as endpoint records. A handle's `allocation_slot` is the array index;
the record supplies the allocation base even when the handle describes a
subrange. `record_crc32c` covers all final-even 64 bytes with itself zeroed.

Each endpoint is the only allocator for its own bulk arena. Remote endpoints
may access a valid handle but never manipulate that allocator.

Storage disk-I/O buffers use ordinary aligned process memory, including buffers
registered with AIO/io_uring. They must not come from the CXL DAX arena: CPU
access to a shared mapping does not establish that a disk device can DMA to it.
Bulk reads copy into these local buffers, and disk reads are pushed from them.
Replication and resynchronization export a separate read-only arena copy,
pinned by the existing request lease. Reusing a local disk buffer cannot change
the exported bytes or shorten the remote allocation lifetime.

The handle checksum is Castagnoli CRC32C over the complete 64-byte encoded
handle with `checksum` set to zero. Its reserved field must be zero on send and
receive.

Exporting a handle also creates a `RemoteExportLease` that pins its allocation.
Before accepting any request byte, the transport binds that lease to the
request UUID, session, lane generation and assigned stream range. It retains
the lease until a final bulk acknowledgement, or until reconciliation plus a
session/owner-endpoint generation transition and membership evidence prove
that no old holder can issue an access. Lane retirement alone is insufficient
because the handle intentionally contains no lane generation.
A caller-visible timeout after publication does not release the lease. The
allocation generation may advance only after every local view and export lease
has been released; this closes the validate-then-reuse race at the remote
reader.

## 7. Publication, visibility and durability

The producer publishes a frame in this order:

```text
write payload
write SQE/CQE body
release fence
release-store producer cursor
```

The consumer follows:

```text
acquire-load producer cursor
validate the complete entry and payload CRC
copy a contiguous prefix to the recv caller
publish the delivered record before recv returns
release-store consumer cursor after the complete cell is returned
```

A partial `recv()` retains validated per-cell state and does not reread or
expose an unvalidated suffix. Advancing the delivered watermark before return
may conservatively classify a crash in that narrow interval as an unknown
outcome; it can never incorrectly classify an executed request as rejected.

The implementation uses aligned `std::atomic_ref<uint64_t>` cursor accesses
and aligned 32/64-bit atomic accesses for owner records. Both host and RISC-V
builds require `std::atomic_ref<uint32_t>::is_always_lock_free` and
`std::atomic_ref<uint64_t>::is_always_lock_free`; G0 also proves both
`is_lock_free()` results at runtime from DAX-backed storage. A library-emulated
atomic is process-local and is therefore invalid for inter-guest
synchronization. Single-writer ownership avoids depending on cross-endpoint
atomic RMW.

Endpoint lifecycle, heartbeat and lane-owner records use an owner-written
sequence word. The owner sequence is advanced from a process-local counter: it
relaxed-stores an odd value (never an RMW), executes a sequentially consistent
fence so later body stores cannot pass it, updates aligned body words with
relaxed atomic stores, then release-stores the next even value. The
128-bit nonce is two 64-bit words. A reader acquire-loads the sequence, rejects
odd values, reads body words with relaxed atomic loads, executes an acquire
fence, and accepts only when a second acquire-load returns the same even value
and the CRC matches. All 32/64-bit atomic accesses must be lock-free. CRC alone
is not a concurrent snapshot protocol.

An odd or changing lifecycle sequence means publication is incomplete, not
that a stable record failed its CRC. The authority monitor waits for another
observation without refreshing its liveness deadline. Only a validated, changed
heartbeat extends that deadline; a permanently unfinished publication still
expires. A stable record with invalid CRC or identity immediately faults the
observer. Incomplete snapshots cannot prove request rejection or permit buffer
reuse.

Every owner-record CRC is computed over the complete encoded cache line with
the CRC field zero and `record_sequence` set to the final even value that will
be release-published. Thus sequence corruption is covered as well as body
corruption; readers never validate the transient odd encoding.

The transport does not use `msync`, `fsync` or Zicbom cache clean to publish
queue data. Visibility comes from the guest-visible CXL coherence path modeled
by QEMU and CXLMemSim. The CXL region is ephemeral transport memory, not the
durable 3FS data store. 3FS durability remains defined by local storage commit
and chain-replication commit.

Restart creates a new session or endpoint generation. Old frames and buffer
handles are rejected instead of recovered as live state.

The runner obtains each fresh nonzero 64-bit session generation from
`getrandom`, rejects reuse in its append-only run ledger and always pairs it
with newly created backing. Within that session, endpoint and lane generations
are nonzero 64-bit incarnation ordinals starting at one and advancing exactly
by one under their declared owner; checked overflow forces a new session.
Phase 2 predeclares every bounded restart incarnation used by a fault scenario.
An undeclared jump, rollback or exhausted schedule faults the session rather
than inventing a generation through discovery.

A lane generation never resets while its session generation is live, including
across an endpoint-process restart, because frames do not carry endpoint
generation. Phase 1 advances from the last stable shared lane-owner high-water
record; if that record is torn or corrupt, it faults the session instead of
guessing. Each lane-owner record also carries its writer's endpoint generation,
closing readiness against a restarted process paired with old lane state.

## 8. Bulk data ownership

### 8.1 Write path

```text
client buffer
  -> client-owned CXL window
  -> storage-owned stable buffer and local storage
  -> head-owned CXL forwarding window
  -> successor-owned stable buffer
```

The correctness baseline retains the current per-hop stable-copy behavior. It
does not let every replica consume the original client window. This preserves
retry and mutation assumptions. Sharing one client window across the entire
chain is a later optimization with separate tests and results.

### 8.2 Read path

```text
storage media
  -> storage DRAM buffer
  -> client-owned CXL response window
  -> client buffer
```

Two client APIs are supported:

- compatibility: existing `registerIOBuffer()` creates and manages a CXL
  shadow for arbitrary process memory;
- native: `allocateIOBuffer()` allocates directly from the endpoint bulk arena.

The initial implementation does not assume that devdax pages can be passed
directly to every configured io_uring/O_DIRECT path. Direct media I/O into a
CXL window is enabled only after an explicit kernel and alignment test. IO500
performance comparisons use the native buffer path once its correctness gate
passes.

## 9. Connections and progress

### 9.1 Phase-1 TCP bootstrap

`CxlConnectService` follows the current high-level RDMA bootstrap pattern:

1. requester sends endpoint ID, process generation, ABI and target service;
2. acceptor allocates a duplex lane from its partition;
3. both validate session generation, manifest SHA-256 and superblock CRC32C;
4. acceptor returns lane offsets, lane generation and a connection nonce;
5. both publish their owner-specific connection state;
6. `CxlSocket` becomes ready and the bootstrap socket leaves the serving path.

An `Address::CXL` may retain an IP and port for phase-1 bootstrap discovery.
Those fields do not imply that serving data uses TCP.

### 9.2 Phase-2 static rendezvous

The runner gives every process a fixed endpoint ID and a deterministic lane
manifest. A lane has owner-separated requester and acceptor state pages with
this effective lifecycle:

```text
FREE -> CONNECTING -> READY -> DRAINING -> RETIRED
                  \-> FAULTED ----------/
```

Neither side writes the other side's state. Both generations, nonce and
capabilities must match before readiness. Servers scan only the incoming lanes
assigned to them. Lane reuse always advances to the next manifest-declared
generation; the initial implementation requires an endpoint incarnation change
or a fresh session rather than reconnecting a failed static lane ad hoc.

### 9.3 epoll integration

The existing `Socket` contract requires an fd, while shared memory has no
remote notification fd. Each process therefore owns a `CxlProgressEngine`:

```text
shared SQ/CQ -> progress polling -> local eventfd -> epoll/IOWorker/Transport
```

`CxlSocket::fd()` returns a local eventfd. The progress engine writes that fd
when receive data or transmit credit becomes available. `poll()` drains the
eventfd and rechecks shared cursors. Arming interest includes a second cursor
check to close the lost-wakeup race.

Polling is configurable as bounded spin, yield and short sleep. Every result
records time spent in each mode. Different systems must use a comparable wait
policy during performance runs.

Traffic classes have separate lanes and budgets so FoundationDB or bulk
control traffic cannot starve latency-sensitive RPCs.

## 10. Timeout and failure semantics

Timeout behavior depends on the serialized request byte range `[begin, end)`
and a trustworthy `PublicationSnapshot` for the same lane generation:

| State | Required behavior |
| --- | --- |
| `published_offset < end` | The full request is not visible. Retain its buffer/lease, stop the lane, and classify it `RejectedBeforeExecute` only after retirement prevents publishing the suffix. |
| `published_offset >= end`, `peer_delivered_offset < end` | The full request is visible but has not been delivered to peer `Transport`. Keep its slots and leases through drain; after an authoritative retirement it is `RejectedBeforeExecute`. |
| `peer_delivered_offset >= end`, no final response | The peer may have dispatched the request. Return `OutcomeUnknown` and reconcile with the original request identity. |
| Snapshot generation or integrity untrustworthy | Return `LaneRetired`, treat it as unknown for application retry policy, and reconcile; never infer rejection from corrupt progress. |

`accepted_offset` is local staging progress, not publication evidence. A
positive `send()` result alone can never move a request out of the first row.
The producer retains each completed request `WriteItem` buffer until its range
end is peer-delivered, a final response arrives, or it is safely recovered
after lane retirement.

The transport preserves RPC UUIDs, storage `MessageTag`, UpdateChannel and
`ReliableUpdate` identities. A timeout never creates a new logical write
identity merely to free a slot.

The common completion dispositions are:

- `Completed`;
- `RejectedBeforeExecute`;
- `OutcomeUnknown`;
- `LaneRetired`.

`LaneRetired` is reserved for a retirement in which trustworthy request-range
classification is unavailable, such as cursor or generation corruption. It is
not a synonym for every normal lane close. For business-level retry safety it
is at least as conservative as `OutcomeUnknown`; the separate `Status` records
the transport failure cause. A normal, trustworthy retirement resolves each
request to `RejectedBeforeExecute` or `OutcomeUnknown` using the table above.

For normal close, new submission stops, queues drain, both owners mark closed,
the lane authority retires the lane, and a future user receives a new
generation.

Each endpoint increments a counter in its own heartbeat page. A peer detects
staleness based on how long its local clock has observed no counter progress,
not on shared wall-clock timestamps. A single RPC timeout is insufficient to
declare failure. Lane reclamation requires both membership/failure evidence and
a generation transition.

## 11. FoundationDB over CXL

FoundationDB retains its protocol and client retry behavior. In phase 2 one
`cxl-netd` process per relevant guest creates a `cxl0` TUN interface:

```text
libfdb_c -> Linux TCP/IP -> cxl0 -> CXL lane
                                      |
FDB guest <- Linux TCP/IP <- cxl0 <---+
```

The experiment assigns an isolated IP subnet to `cxl0` and routes FDB
coordinator and worker addresses through it. This handles FoundationDB's
dynamic worker address discovery more reliably than a single-port proxy.

Normal phase-2 guest orchestration uses the QEMU serial consoles. Before the
readiness barrier, every ordinary guest NIC is stripped of global addresses and
routes and set down; only loopback and `cxl0` remain usable by application
processes. Route/socket snapshots and byte counters independently verify the
isolation. This does not affect CXLMemSim's host-side simulator TCP adapter,
which is outside the guest network stack.

The comparison-only IO500 profile may additionally create a run-owned `mpi0`
virtio network matching the unmodified LegoFS MPI/Hydra control path. It has no
route to any 3FS or FDB serving address; only the recorded MPI/Hydra process
identities and ports may bind it. Core, FDB, 3FS clients and daemons remain
forbidden from using that interface, and any such socket or byte is a failed
run. This benchmark-control exception is not present in the normal phase-2
functional gate and is not a TCP serving fallback.

The FDB lane class has independent queue capacity and evidence counters. The
phase-2 gate requires a RISC-V `fdbserver`, a compatible RISC-V `libfdb_c`, TUN
kernel support, and positive FDB CXL byte counts. Failure of that gate blocks
phase 2; it does not authorize TCP fallback.

## 12. Source layout and change map

New CXL code is placed under:

```text
src/common/net/TransportRuntime.{h,cc}
src/common/net/cxl/
  CxlAbi.h
  CxlFabric.{h,cc}
  CxlRegion.{h,cc}
  CxlLayout.{h,cc}
  CxlEndpointState.{h,cc}
  CxlLane.{h,cc}
  CxlSocket.{h,cc}
  CxlProgressEngine.{h,cc}
  CxlConnectService.{h,cc}
  CxlBuffer.{h,cc}
  CxlBulkTransfer.{h,cc}
  CxlMetrics.{h,cc}
src/tools/cxl-fabricd/
  CMakeLists.txt
  main.cc
```

The region layer has two implementations with identical validation:

- `FileRegion` uses memfd or a file-backed mmap for host tests;
- `DaxRegion` maps `/dev/dax0.0` in the RISC-V guest.

The main existing change areas are:

- `common/utils/Address.h`;
- application/launcher lifecycle code that currently starts `IBManager`;
- `common/net/Socket.h`, `Transport.*`, `TransportPool.*`, `IOWorker.*`, `Listener.*`,
  `Client.h`, `RequestOptions.h`, `Waiter.h` and `RDMAControl.*`;
- `common/serde/Services.h`, `MessagePacket.h`, `ClientContext.*` and
  `CallContext.*`;
- `common/net/Processor.*`;
- `client/storage/StorageClient.*`, `StorageClientImpl.*`;
- FUSE clients/config and RDMA-buffer-using admin commands;
- `fbs/storage/Common.h`;
- `storage/service/StorageOperator.*`, `ReliableForwarding.*`,
  `BufferPool.*`;
- storage AIO, sync and resync call sites;
- server and client default configs;
- top-level and common CMake files.

Phase 2 additionally introduces:

```text
src/tools/cxl-netd/
deploy/cxl-riscv/
```

After phase 1, business-layer source outside `common/net/ib` must not refer to
`IBSocket`, `RDMABuf`, `RDMARemoteBuf`, verbs opcodes, IB devices or legacy
IB/RDMA error symbols. Stable numeric error aliases may retain those legacy
declarations in `StatusCode` compatibility code, but all new call sites use
transport-neutral names.

## 13. RISC-V build and deployment

All integration support remains inside 3FS:

```text
third_party/foundationdb/  # pinned gitlink plus audited direct RISC-V edits
deploy/cxl-riscv/
  preflight.py
  toolchain-riscv64.cmake
  full-deps.json
  kernel.fragment
  build_riscv.py
  guest_image.py
  sync_to_vlm_server.py
  run_g0.py
  run_phase1.py
  run_phase2.py
  fdb/
    fdb-riscv.lock.json
    build_fdb_riscv.py
    verify_fdb_bundle.py
```

The runner may consume existing parent-repository QEMU, Linux and CXLMemSim
artifacts by absolute path. It does not patch their source. It creates writable
guest images for FoundationDB and 3FS storage plus a root filesystem containing
the required RISC-V shared libraries.

The feasibility work has two ordered checkpoints to avoid a circular
dependency with the RDMA-neutralization work:

- **G0-A, before transport refactoring:** use a standalone, 3FS-owned smoke
  CMake project to prove the RISC-V C++ compiler/sysroot, guest DAX mapping,
  lock-free 32-bit and 64-bit atomics, FUSE/kernel contract and a transaction between the
  matching RISC-V `libfdb_c` and `fdbserver`. It does not link the current
  RDMA-coupled `common` library or use a host-native FDB substitute.
- **G0-B, after the phase-1 source/build closure:** cross-build and start
  `mgmtd_main`, `meta_main`, `storage_main` and `hf3fs_fuse_main` with CXL on,
  RDMA off and no `libibverbs` dependency.

G0-A must pass before implementing the transport. G0-B must pass before the
first multi-guest 3FS integration run.

There is no assumed official RISC-V FoundationDB binary. The current official
container packaging selects amd64 or arm64 and rejects other architectures,
while the official project recommends a dependency-heavy source build when no
binary package exists. G0 first accepts a provenance-complete compatible
RISC-V bundle if one is supplied. Otherwise a 3FS-owned, pinned source-port
experiment builds host generators separately from RISC-V targets directly from
the `third_party/foundationdb` gitlink. The lock records its exact annotated tag,
peeled commit, complete set of directly edited tracked paths and each resulting
file SHA-256; an extra, missing, untracked or differently hashed edit fails
closed. This experiment is a feasibility gate, not an assertion that upstream
supports RISC-V. Failure to produce both native RISC-V artifacts blocks phase 1
and phase 2.

Phase 1 starts the pinned RISC-V `fdbserver` over its retained guest TCP network
and proves that actual Core, bootstrap and FDB traffic are the only TCP serving
classes. Phase 2 reuses the same FDB artifacts and additionally requires
`/dev/net/tun`, the isolated `cxl0` route and a successful FDB transaction over
that route.

Benchmark configs reduce production defaults such as the multi-gigabyte
storage RDMA buffer pool and 32-thread service pools to values appropriate for
the declared guest memory. Reductions are recorded and applied equally across
repeated runs.

## 14. Validation strategy

No validation stage requires an RDMA device. Existing RDMA behavioral tests
are generalized into transport tests and run against CXL.

| Layer | Environment | Required coverage |
| --- | --- | --- |
| ABI | x86 file/memfd mapping | Layout validation, overflow, alignment, ring wrap, absolute-cursor exhaustion, byte-stream segmentation, delivered-record integrity, CRC and generation rejection. |
| Concurrency model | Formal `specs/CXLTransport` model | No overwrite, duplicate consumption, premature reuse or stuck drain under valid scheduling. |
| RPC | x86 two-process CXL backend | Echo, concurrency, compression, partial sends, large messages, short timeout, restart and connection loss. |
| CXL smoke | Two RISC-V guests | DAX mapping, bidirectional SQ/CQ, coherent visibility and correlated QEMU/CXLMemSim evidence. |
| Storage | Client and storage guests | Batch reads/writes, checksum, inline and bulk paths, native and shadow buffers. |
| Replication | Client plus multiple storage guests | Forwarding, RF=2/3, resync, successor restart and unknown-outcome reconciliation. |
| Phase 1 | Complete 3FS guest cluster | Every former RDMA path uses CXL; Core/FDB TCP remains functional; no RDMA dependency or access. |
| Phase 2 | Complete 3FS plus FDB guest | Core and FDB use CXL; no serving TCP after readiness. |
| Filesystem | FUSE and IO500 | Easy, metadata, random-4K, tiny and standard validation. |

Required negative tests include malformed descriptors, stale generations,
queue exhaustion, delayed consumers, producer death at every publication
boundary, responder death after execution, endpoint restart and corrupted
payload/entry/delivered-record checksums.

## 15. Evidence and observability

At minimum the implementation exports:

```text
cxl_rpc_requests
cxl_rpc_responses
cxl_rpc_bytes
cxl_stream_frames
cxl_peer_delivered_bytes
cxl_bulk_read_bytes
cxl_bulk_write_bytes
cxl_queue_full
cxl_queue_max_occupancy
cxl_poll_spin_ns
cxl_poll_yield_count
cxl_poll_sleep_count
cxl_generation_mismatch
cxl_crc_error
cxl_lane_connect
cxl_lane_retire
cxl_lane_forced_retire
cxl_outcome_unknown
rdma_open_attempts
serving_tcp_bytes
fdb_cxl_packets
fdb_cxl_bytes
fdb_cxl_packet_fragments
forbidden_fallbacks
```

Phase 1 requires `rdma_open_attempts == 0`. Phase 2 requires, after
`CXL_READY`, `serving_tcp_bytes == 0`, `fdb_cxl_bytes > 0` for an FDB-active
workload, and `forbidden_fallbacks == 0`.

Here `serving_tcp_bytes` means application TCP payload observed on interfaces
other than loopback and `cxl0`. FoundationDB still speaks TCP/IP, but packets
injected into `cxl0` are accounted as `fdb_cxl_packets`/`fdb_cxl_bytes` because
their inter-guest carrier is the CXL region rather than an Ethernet or QEMU
user-network socket.

Global FDB CXL packet/byte values count successful producer-side publications
once. Receiver totals must reconcile after lane drain but are not summed into
the global value. TCP retransmissions that reach TUN are distinct carried
packets and remain counted; this avoids both hiding retransmission cost and
double-counting the sender/receiver observation of one CXL transfer.

The CXLMemSim TCP MESI adapter is labeled simulator-internal traffic. It is not
counted as a 3FS application TCP path.

## 16. LegoFS comparison

The measured variants are:

- `3FS-CXL-P1`: former RDMA paths use CXL; Core and FDB retain TCP;
- `3FS-CXL-P2`: all 3FS serving traffic uses CXL; FDB uses `cxl0`;
- `LegoFS-CXL`: the existing shared-region serving implementation.

No measured 3FS-RDMA baseline is reported without hardware. Published RDMA
results, if cited, are contextual only.

The architectural comparison is:

| Dimension | 3FS-CXL | LegoFS-CXL |
| --- | --- | --- |
| Existing semantics retained | 3FS serde, UUID/retry, storage checksum and chain replication | LegoFS serving-transport, authority and persistence profiles |
| Small RPC/control | `CxlSocket` byte stream feeding existing 3FS framing on explicit Control/Data lanes | Existing LegoFS shared-region request lanes |
| Bulk payload | endpoint-owned arenas and validated 64-byte handles; stable copy per replication hop | Existing staged/direct/blob path selected by its recorded profile |
| Metadata dependency | Meta plus FoundationDB; phase 2 carries FDB TCP/IP through `cxl0` | no FoundationDB guest |
| Scaling semantics | RF1 is the direct baseline; RF2/RF3 are separate chain-replication results | single-server baseline; multi-server placement is a separate class |
| Failure boundary | request publication disposition, lane generation and 3FS reconciliation | LegoFS generation/lease/authority recovery evidence |
| Resource accounting | all 3FS, FDB and `cxl-netd` guests/processes | all LegoFS server/client guests/processes |

This table is not a claim that similarly named internal operations have equal
completion semantics.

### 16.1 Transport-isolated diagnostics

Use identical QEMU/CXLMemSim settings for 1-client/1-server and
2-client/1-server runs. Cover 64 B, 1 KiB, 8 KiB, 64 KiB, 1 MiB and 4 MiB
requests, queue depths 1/8/32/64, and 4 KiB through 4 MiB bulk transfers.

Report latency distributions, throughput, useful/CXL byte ratio, frames per
operation, occupancy, queue-full time, poller CPU, memory-copy
bytes and coherence events.

The existing 3FS and LegoFS transport probes expose different operation and
completion contracts. Their raw panels are therefore diagnostic by default,
not a source of cross-system speedup ratios. A direct transport ratio is emitted
only if the comparison validator proves the same guest platform, payload,
concurrency, start/stop boundary, checksum and visibility/durability class. If
the unmodified LegoFS runner cannot express a matching case, the report says
`not directly comparable` and the direct cross-system comparison remains the
RF1 IO500 result below.

### 16.2 Full-system comparison

Run matching IO500 easy-smoke, metadata-smoke, random-4K and tiny configurations
as functional qualification only. The audited LegoFS runner treats those as
semantic-smoke stages whose verifier expects an invalid-size result, so they
must not contribute throughput statistics. Run clean-valid `scc` and
`standard` configurations for the measured filesystem comparison. The
principal cross-system comparison uses 3FS replication factor one. RF=2 and
RF=3 are additional 3FS scaling results and are not presented as equivalent to
a single-server LegoFS run.

If 3FS requires an additional FDB guest, its vCPU, memory and runtime cost are
included in total system resources. Client-visible throughput is reported
alongside total all-guest resource use and its per-role breakdown.

The principal RF1 full-system group uses an equal total resource envelope:
client/rank count, aggregate all-guest vCPU, aggregate all-guest RAM,
writable storage capacity and exposed CXL capacity are equal. Each system may
partition that envelope into its native internal roles, so 3FS's
mgmtd/meta/FDB guests need not match LegoFS's guest count; every role is still
charged to the aggregate. If either system cannot run inside the frozen
envelope, report feasibility/resource-normalized results separately and emit
no cross-system throughput ratio. Phase 1 versus phase 2 uses the same 3FS
internal placement.

### 16.3 Semantic normalization

Results distinguish:

- visibility complete;
- locally durable;
- replicated durable.

Only matching durability levels are compared directly. LegoFS CXL persistence
and 3FS disk/replica commit are not assumed equivalent merely because both
operations returned success.

### 16.4 Experimental controls

Every comparison fixes and records:

- host kernel, CPU topology, QEMU accelerator/TCG thread mode, CPU/NUMA
  affinity, backing mount/device class and a frozen host-load/noise policy;
- QEMU, Linux and CXLMemSim commits;
- the normalized per-role QEMU CXL device contract: CPU/cache-block settings,
  FMW size and restrictions, HDM passthrough, 256-byte flit mode, HDM-DB,
  persistent Type-3 mode, unique coherence host IDs, cache capacity/ways,
  timeout, write-through/read-exclusive policy and endpoint-backing inode
  topology;
- the normalized CXLMemSim contract: coherence-v2 transport/topology,
  authoritative capacity, latency, trace, SSD-stream backing, page/I/O-chunk,
  cache, read-ahead, io_uring and O_DIRECT settings;
- logical fabric capacity, per-guest aperture size and 64-byte line size (the
  shared fabric capacity is counted once, while aperture count is reported
  separately);
- client count plus aggregate vCPU and guest memory across every client,
  server and infrastructure guest, with per-guest placement recorded;
- link, media, request and BI model parameters;
- benchmark-control network model, rank-to-guest placement, MTU and MPI/Hydra
  argv (when a multi-rank filesystem comparison is made);
- writable storage image type and size;
- IO500 configuration, seeds and rank count;
- polling policy and queue parameters.

Every preconditioned configuration performs its declared warm-up inside the
same live guest cohort as its measurement. The principal cross-system IO500
profile is cold-start with fresh backing and zero warm-ups: the audited
read-only LegoFS runner accepts one stage and shuts down its guest cohort when
that invocation returns, so a separate invocation is not a valid warm-cache
preconditioner. No cross-system warm-cache ratio is emitted unless a later
unmodified runner exposes and proves a retain-cohort hook.

`cold-start` means new writable CXL/storage backings and new guest/simulator
processes. It does not claim privileged eviction of the host kernel page cache;
the residual host state is disclosed and mitigated by rotated complete blocks.

Each reported principal configuration requires at least five complete valid
interleaved pairing blocks. A failed slot invalidates its block for paired
statistics and causes a full replacement block to be appended, subject to a
frozen attempt bound. Variant order rotates to reduce host drift. Reports use
median and relative MAD and never combine cold-start and warm-cache results.
Measured variant slots execute serially; overlapping runner-owned lifetimes or
a failed predeclared host-noise gate invalidate the affected block.

The correctness gates use distinct poisoned endpoint backing inodes so that
host page-cache sharing cannot manufacture peer visibility. The read-only
LegoFS runner may expose a different backing-inode policy in a measured
profile. The comparison adapter records that policy from the realized QEMU
argv and configures the 3FS measured profile identically; otherwise the runs
remain separate and no cross-system ratio is emitted. A matched shared-inode
measurement does not replace the earlier distinct-inode correctness proof.

The most recently inspected remote LegoFS result,
`target/results/legofs-io500/placement-route-restore-v118-10c1s-tiny-r2/result.json`,
is failed because the MPI tiny stage timed out; it also reports an invalid
filesystem and no guest-visible CXL evidence. It cannot serve as a baseline.
Failed evidence remains preserved while a new valid baseline is obtained.

### 16.5 Valid-run gate

A run enters statistics only if:

- its result class is `measured` and its workload has the clean-valid verdict
  required by that profile; `qualification-only` semantic-smoke outcomes never
  enter performance statistics;
- application and filesystem validation pass;
- every required guest reports CXL readiness and guest-visible CXL evidence;
- no checksum, generation, range or pending-request error remains;
- no forbidden fallback occurs;
- the phase-specific RDMA/TCP/FDB counters pass;
- CXLMemSim reports no protocol, delivery or server-copy failure;
- cache state, warm-up count, fresh-backing/cohort identity and measurement
  boundaries match the immutable experiment profile;
- runner-owned processes exit and evidence collection completes.

Every result is labeled:

```text
functional QEMU/TCG + CXLMemSim evidence
not physical CXL performance
```

## 17. Gates and implementation order

```text
G0-A  Standalone RISC-V C++/FDB/FUSE/DAX feasibility
 |
G1    Transport-neutral interfaces, CXL ABI, host tests and formal model
 |
G0-B  Complete CXL-only RISC-V 3FS build/start closure
 |
G2    CxlSocket, TCP bootstrap and two-guest Echo/large-message tests
 |
G3  CXL bulk buffers, storage read/write, forwarding and resync
 |
P1  Complete cluster with every former RDMA path on CXL
 |
G4  Static rendezvous and Core cutover
 |
G5  cxl-netd, RISC-V FoundationDB and FDB-over-CXL proof
 |
P2  No 3FS serving TCP after CXL readiness
 |
G6  Transport microbenchmarks and five-run IO500 comparison
```

A failed gate is repaired at that layer. It does not authorize a mock service,
host-native substitute, or TCP/RDMA fallback to make a later gate appear to
pass.

## 18. Principal risks

- FoundationDB publishes no assumed RISC-V binary and its source build uses
  host-side generators plus a large native dependency closure; producing and
  validating both RISC-V artifacts is the first hard feasibility risk.
- Full 3FS RISC-V builds may require substantial sysroot and dependency work.
- The current guest kernel may require FUSE and TUN configuration overlays.
- Full IO500 under QEMU TCG may be slow enough to require carefully bounded
  smoke configurations before standard runs.
- Polling can dominate guest CPU and must be accounted for rather than hidden.
- DAX-backed pages may not be accepted directly by every media I/O path; the
  baseline retains explicit copies until proven otherwise.
- Stale shared-memory state can create false success unless every object is
  generation-scoped and fail-closed.
- LegoFS and 3FS have materially different metadata and durability
  architectures; system-level results cannot be labeled as transport-only.

These risks are addressed by the ordered gates, explicit evidence counters and
two-level comparison methodology above.
