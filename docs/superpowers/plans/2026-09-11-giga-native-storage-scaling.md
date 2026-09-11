# Giga Native 3FS-CXL Storage Server Scaling Implementation Plan

> **For agentic workers:** 按以下任务在当前会话内顺序执行，每步保存验收结果。用户已批准本计划并授权实施。当前会话内顺序执行，不提交、不推送、不委派子代理。

**Goal:** 在 giga 单机上支持 1/2/4 个独立 storage server，保持 RF=1、现有 CXL copy-like 路径和文件布局，证明每个 server 实际存储并处理自己的数据。

**Architecture:** 复用现有 `storage_main`、管理面和 manifest 布局算法，在 native 部署层增加节点描述、配置渲染、target 放置和逐节点验收。每个实验从独立空目录启动；这是多实例部署扩展，不包含已有集群的在线扩容、数据迁移或自动再均衡。

**Tech Stack:** Python 3.11+、现有 3FS C++ 二进制、FoundationDB 7.3.63、FUSE、共享 CXL transport region、现有有界 IO500 配置。

**Spec:** 本文“设计合同”“节点和资源”“验收合同”是本计划的设计依据。当前源版本为 `6197e74dce6a38ee691b23a7281c7b8ae6c6fc72`；第三方依赖的现有本地兼容性提交保持原状。

## 设计合同

1. 原有 `1c1s / 2c1s / 3c1s` 继续有效；扩展为客户端数 `{1,2,3}` × storage 数 `{1,2,4}`。首轮不扩客户端上限。
2. `S` 专指 `storage_main` 实例数。FDB、fabric authority、mgmtd、meta 各保留一个，纳入服务端资源统计。
3. 所有新实验保持 RF=1，并按用户补充要求显式使用新引擎：创建 target 时传 `--use-new-chunk-engine`，逐个验证实际 target.toml 中 `only_chunk_engine=true`。不将旧引擎或引擎未知的历史结果当成可比基线。使用正常 target、chain table 和 3FS 路由。不能用 RF=2/3 冒充 server 扩展，也不增加副本写入。
4. 保持全局 **4 个 target / 4 条单副本 chain**。1S 每节点 4 个，2S 每节点 2 个，4S 每节点 1 个。增加 server 不同时增加 target 数量。
5. 保持 chunk size=524288 B、stripe size=1、chain table ID=1。单文件不因此自动跨 server 条带化；使用多个文件验证跨 server 放置。
6. CXL region=1 GiB、memory node=1、lane count=256、queue depth=8、cell bytes=65536、allocation slots per endpoint=256。保留 `adaptive` polling 默认值和已有所有显式 polling 选项。
7. 保持 RPC/serde、UUID、重试、pending-publication、checksum、buffer 生命周期、storage commit、flush/fsync 和 chain replication 实现。保持当前 worker、buffer pool、超时、FUSE cache 和 GC 配置；不借扩展调优这些参数。
8. 数据请求继续走 CXL；Core/bootstrap 和 FDB 保持当前允许的控制路径。TCP data-plane、bootstrap serving fallback、RDMA open attempts 必须仍为零。
9. 保持当前 `/tmp` 数据后端和 `fallocate` 能力检查；CXL region 是 transport region。此次不把 tmpfs 更换为 CXL 持久存储，也不声称验证断电持久性。
10. 不修改 `src/**`、第三方仓库、公共 RISC-V 部署流程、论文、主仓库 IO500 脚本、IO500 profile 或默认 LegoFS 快路径。若部署验收暴露生产代码问题，先单独记录复现和机制影响，不能通过改协议或放宽验收使这次扩展“通过”。

## 已核对的实现事实

- `deploy/giga-native/topology.py:topology()` 当前只变化客户端数，storage 固定 CPU 22/23。
- `deploy/giga-native/cluster.py:_native_manifest()` 只声明 storage-0；启动、heartbeat 和 target 创建固定 node ID=10000。
- `deploy/cxl-riscv/make_phase1_manifest.py:select_roles()` 按 RF 动态补入 storage roles；但也支持 scenario 的显式 `base_roles`，因此 native 调用方可声明多个 storage，复用原有 manifest 校验与布局算法。
- `deploy/cxl-riscv/full_stack.py:render_config()` 生成现有基线模板，包括独立 Core listener 的端口 0。native 扩展保留该函数，只在自身生成的副本上修改节点身份、服务端口、路径和 target 列表。
- `src/common/app/TwoPhaseApplication.h:configPushable()` 在显式 `--cfg` 时返回 false。现有 native 启动正使用该模式，故可保留独立本地配置，不需要增加按节点配置推送或模板引擎。
- `InitCluster.cc` 确认最后一个初始化参数是 stripe size，当前值为 1。
- 已运行现有本地测试：giga-native 37 项、manifest 7 项、full-stack 21 项，合计 **65 项通过**。这是改动前的基线，不是多 server 已通过的证明。
- 已只读核对 giga 的 CPU/core/SMT/LLC 映射，下表 CPU 均为不同物理核；node1 无 CPU。尚未执行新拓扑。

## 节点和资源

### 固定身份

| 角色 | Node ID | CXL endpoint | Data 地址/端口 | CPU |
|---|---:|---:|---|---|
| FDB | 不适用 | 无 | TCP 4500 | 18 |
| fabric authority | 不适用 | 1 | CXL 12499 | 19 |
| mgmtd | 1 | 2 | CXL 12501 | 20 |
| meta | 50 | 3 | CXL 12502 | 21 |
| storage-0 | 10000 | 4 | CXL 12503 | 22,23 |
| storage-1 | 10001 | 5 | CXL 12504 | 16,17 |
| storage-2 | 10002 | 6 | CXL 12505 | 10,11 |
| storage-3 | 10003 | 9 | CXL 12506 | 4,5 |
| admin | 不适用 | 7 | CXL 12507 | 沿用现有策略 |
| client-0/1/2 | 沿用现有身份 | 16/17/18 | CXL 12516/12517/12518 | 1/7/13 |

所有 CXL 地址为 `CXL://127.0.0.1:<port>`。endpoint 7 保留给 admin，8 保留给原有 monitor 角色；第四个 storage 使用 9，不能按 `4 + index` 分配。Core listener 继续使用端口 0，并从路由记录保存实际监听地址。

每 storage 2 个物理核，控制服务共 4 核，所以 1/2/4S 的长期运行服务端核预算为 **6/8/12**。保存每个进程所有线程的实际 affinity、CPU time、RSS，以及短时 admin 进程的资源记录。per-storage worker 和 buffer 配额保持原值，聚合配额随 server 数增加，必须一起报告。

storage-0/1/2/3 的 LLC 分别为 3/2/1/0；后三者会与相应 client 共享 LLC。不能继续把多 server 描述成“全部服务端位于 LLC3”，也不能把这条曲线解释为固定总资源或纯进程分片收益。固定总核数、不同 LLC 放置和跨物理机扩展留作后续独立实验。

### 固定 target 和文件布局

令 `i` 为 0..3，原有 target/chain ID 均保持 `1000000000 + (i + 1) * 1000 + 1`，即 `1000001001` 到 `1000004001`。

```python
owner_index = i % storage_count
owner_node_id = 10000 + owner_index
disk_index = i // storage_count
```

`create-target` 已分别接收 node ID、disk index、target ID、chain ID，target ID 不需要随拥有节点重新编号。

| S | storage-0 | storage-1 | storage-2 | storage-3 |
|---:|---|---|---|---|
| 1 | T1,T2,T3,T4 | — | — | — |
| 2 | T1,T3 | T2,T4 | — | — |
| 4 | T1 | T2 | T3 | T4 |

1S 保留当前 `storage/data1..data4` 路径形式。多 S 使用 `storage/node-10000/data1..dataK` 等独立目录，K=4/S，避免相互打开同一后端。四条 chain 的内容和顺序保持原样，每条只含一个 target。

新集群独立创建，因此不存在运行中 target 搬迁。创建新文件时由原有 3FS 路由选择 chain。不得修改文件路由算法、在正式 IO500 测量里硬编码 file→server，也不得为获得加速比自动把 stripe size 改为 4。

## 改动边界与接口

以下接口已实现；远端验收进度见文末。

| 文件 | 责任 |
|---|---|
| 修改 `deploy/giga-native/topology.py` | 解析九种拓扑；定义 `StorageNode`；固定节点身份和 CPU；扩展 host 验证 |
| 新增 `deploy/giga-native/storage_layout.py` | 四个 target 的放置、native 多 storage manifest、独立 storage 配置渲染 |
| 修改 `deploy/giga-native/cluster.py` | 按节点启动/等待/配置/创建 target；保存部署来源和每节点证据；CLI 与验收 |
| 新增 `deploy/giga-native/storage_smoke.py` | 通过现有 `execute_cluster()` 执行有界多文件、跨客户端验证 |
| 修改 `tests/giga_native/test_topology.py`、`test_cluster.py` | 旧入口兼容、资源校验、部分启动失败、节点验收 |
| 新增 `tests/giga_native/test_storage_layout.py`、`test_storage_smoke.py` | 放置、配置差异合同、错误布局及读回/节点证据失败 |
| 新增 `deploy/giga-native/README.md` | 拓扑含义、命令、资源和限制 |

预期接口：

```python
# topology.py
@dataclass(frozen=True)
class StorageNode:
    index: int
    node_id: int
    endpoint: int
    port: int
    cpus: tuple[int, ...]

# Topology 新增 storage_nodes: tuple[StorageNode, ...]
# 新增只读 storage_count 属性，等于 len(storage_nodes)。
# 原有 client_cpus/client_l3/ranks/region_bytes 不变。
# server_cpus 和 ports 中的首节点仍使用原有 "storage" 键；
# 多 S 额外使用 "storage-1"、"storage-2"、"storage-3"。
def topology(name: str) -> Topology: ...
def validate_host_topology(sysfs_root: Path = Path('/sys'),
                           *, selected: Topology | None = None) -> dict: ...

# storage_layout.py
@dataclass(frozen=True)
class TargetPlacement:
    target_id: int
    chain_id: int
    node_id: int
    disk_index: int

def target_placements(storage_count: int) -> tuple[TargetPlacement, ...]: ...
def native_manifest(clients: int, storage_count: int,
                    session: int, region_bytes: int) -> dict: ...
def render_storage_config(base: Path, destination: Path, node: StorageNode,
                          target_paths: tuple[Path, ...], logs: Path) -> dict: ...

# cluster.py 保持已有调用方兼容，storage_count 为新增 keyword-only 参数。
def _native_manifest(clients: int, session: int, region_bytes: int,
                     *, storage_count: int = 1) -> dict: ...

# storage_smoke.py
def run(cluster: NativeCluster) -> dict: ...
```

接口签名中的省略号仅表示签名，不是允许跳过实现；下面任务逐项规定实现和测试。

## 实施任务

### Task 1：拓扑与四个 target 的放置

**Files:** `topology.py`、`storage_layout.py`、`test_topology.py`、`test_storage_layout.py`。

**Consumes:** 上述节点表、固定 target ID 与放置公式。

**Produces:** `StorageNode`、`Topology.storage_nodes/storage_count`、`target_placements()`。

- [x] 先补充行为测试：旧 1S 对象的 CPU、端口和客户端字段保持原值；九种组合有效；拒绝 0S/3S/8S、0C/4C、格式错误；所有 endpoint/port/物理核不重复。

```python
for clients in (1, 2, 3):
    for servers in (1, 2, 4):
        value = topology.topology(f'{clients}c{servers}s')
        assert value.ranks == clients
        assert value.storage_count == servers
        assert [n.node_id for n in value.storage_nodes] == list(range(10000, 10000 + servers))
        rows = storage_layout.target_placements(servers)
        assert len(rows) == 4
        assert len({r.target_id for r in rows}) == 4
        assert all(r.target_id == r.chain_id for r in rows)
        assert {r.node_id for r in rows} == {n.node_id for n in value.storage_nodes}
```

- [x] 运行 `python3 -m unittest discover -s components/3FS/tests/giga_native -p 'test_storage_layout.py'`，确认新接口测试先失败。
- [x] 按固定表构建 `storage_nodes`；用 `re.fullmatch(r'([123])c([124])s', name)` 解析；按上述公式生成四个 placement。host 校验接受所选拓扑，检查 online、物理核唯一性、逐 CPU 的 LLC 和 node1，不能直接去掉原有拓扑校验。
- [x] 同时通过新增测试和原 `test_topology.py`，确认默认 1S 结果没有被静默改成新资源策略。

### Task 2：独立配置与 manifest，保持共享生成器不变

**Files:** `storage_layout.py`、`cluster.py`、`test_storage_layout.py`、`test_cluster.py`。

**Consumes:** Task 1 的 `StorageNode` 与 placement；当前 `full_stack.render_config()` 生成结果。

**Produces:** 每 storage 独立 app/launcher/server 配置；共同 manifest；逐文件配置哈希与允许字段差异记录。

- [x] 保存当前 1/2/3C1S 渲染 fixture，固定 session=7、region=1 GiB，作为完整配置回归输入。fixture 排除 token 内容的展示，但比较时不擅自改 token。
- [x] 新增碰撞负例：第四个 storage 使用 endpoint 7、重复 node ID、重复 data port、同一 target 路径、缺失路由、Core listener 被改为固定 data port，都必须失败。
- [x] 1S 走原 manifest 路径，要求相同输入得到相同完整 manifest 和 digest。多 S 只在内存复制的 native topology 中添加一个 `native-storage` scenario：`base_roles` 为原控制角色加全部 storage；`storage_roles=False` 表示不再按 RF 动态追加；`client_roles=True`。调用原 `build_manifest(..., replication_factor=1)`，不修改公共 `phase1_topology.json` 或生成器。
- [x] 给多 S 声明 endpoint 4/5/6/9 和端口 12503..12506，保留所有旧角色编号。由原布局算法检查 ranges、对齐、bulk 空间和唯一性，不在生成后补丁式追加 routes/participants 或重写 digest。
- [x] 从当前基线配置生成每 storage 副本，继续传入独立的 `--app_cfg`、`--launcher_cfg`、`--cfg`。严格限定修改字段：node ID、endpoint、第一组 data listener 端口、配置/日志路径、target_paths。用锚定字段且要求精确匹配一次的替换，并用 `tomllib` 解析最终文件；不能全局替换数字或字符串后直接使用。
- [x] 将新旧 TOML 展平为字段路径比较，除上述清单外必须相同，包括第二个 Core listener、worker、buffer、polling、重试、fsync/GC/cache 配置。1S 所有现有配置语义和文件布局不变。
- [x] 运行 giga-native、manifest、full-stack 三组测试。公共 RF=2/3 和原 RISC-V 渲染行为必须继续通过。

### Task 3：逐节点启动、readiness、target 创建与生命周期

**Files:** `cluster.py`、`test_cluster.py`；仅在必要的证据字段扩展处使用现有 `runtime.py` API，不重写进程管理。

**Consumes:** 拓扑、配置、共同 manifest、四个 placement。

**Produces:** 正常启动且可审计的 1/2/4S 集群，失败时只处理本次拥有的进程和目录。

- [x] 增加模拟验收测试：第二个 storage 启动失败、少一个 heartbeat、重复 target 行、节点归属错误、一个节点未参与、端口仍占用，均不能标记通过。
- [x] 沿用 FDB → fabric → mgmtd/meta 的顺序，再逐个启动 storage；第一实例保留 `storage` receipt 名称，其余为 `storage-1..3`。每个保存 PID/start_ticks/可执行文件/argv/config/log/CPU；继续用 `OwnedProcess` 的身份校验。
- [x] readiness 同时核对全部预期 STORAGE node ID、类型、heartbeat、CXL Data 地址和独立 Core 地址。不能仅计数或等待 10000；不能只凭 `Start server finished`。
- [x] 对每个节点执行现有 `get-config --node-id <id> --output-file <owned-path>`，核对实际 data port 和 target_paths，避免“配置文件不同，实际启动相同”。
- [x] 初始化依旧为 `init-cluster ... 1 524288 1`。根据 placement 调用现有 `create-target`，沿用四条 RF1 chain 和 chain table。上传后 `list-targets`、`list-chains`、`dump-chain-table` 三份证据必须组成完整双射：四个 target 各一个拥有节点、四条 chain 各一个 target、table 恰好列出四条 chain。
- [x] 用结构化的 ID/状态集合校验替换只数四次 `SERVING-UPTODATE` 的薄弱判断。拒绝 OFFLINE、缺失、重复、额外成员及 RF>1。
- [x] 失败走现有逆序 teardown，已启动的 storage 都纳入；FUSE、storage、meta/mgmtd 退出后再关闭 fabric/FDB。检查全部本次进程、mount 和端口；没有有效 owner receipt 的进程/路径不能操作。保留原始 first_failure，不由后续 cleanup 错误覆盖。
- [x] result JSON 加入 `storage_count`、`replication_factor=1`、逐节点 CPU/LLC/路径/endpoint、placement、Core 实际配置和部署源文件哈希。保留 binary build manifest 原始身份，额外记录新部署源，不能把旧二进制重新标成由新源构建。

### Task 4：多文件与跨客户端 smoke，证明真实 server 参与

**Files:** `storage_smoke.py`、`test_storage_smoke.py`、`cluster.py` 中的验收扩展。

**Consumes:** `execute_cluster()` 与 Task 3 的已启动集群/placement。

**Produces:** 有界、可失败的内容和路由验证；逐 storage 的参与证据。

- [x] 增加错误注入测试：预期四节点却只命中三节点、错误 chain owner、错误内容哈希、缺失 endpoint、重复/错 session 的 transport record、非零 fallback；全部必须失败。
- [x] smoke 使用 **2 个独立 FUSE client**，全局 64 个文件，每文件 4 MiB，总 256 MiB。文件 `i` 由 client `i % 2` 写入，按文件编号生成确定性非零内容；逐文件 fsync、close，写入阶段全部结束后由另一 client 首次打开并读回，比较完整 SHA-256。每文件 120 秒、每阶段 600 秒界限固定；不通过延长超时掩盖问题。
- [x] 保持正常默认布局。写完后通过 `stat --display-layout --display-chain-list --display-chunks` 和 chain/target 表验证四个 chain 都实际拥有文件，记录每节点的文件数和字节数。若未覆盖，不改路由、不筛选保留“漂亮”的文件；这次 smoke 失败并保存分布。
- [x] 两个 mount 是独立 FUSE 实例，读者此前不读取对应文件，减少本进程读缓存代替服务端读回的可能。对每个 storage 至少一个实际 chunk 再执行现有 `query-chunk --read`，保存 backend read/checksum 成功证据。此管理读只用于正确性 smoke，不掺入 IO500 性能测量。
- [x] 使用既有 process-lifetime transport counters，核对 storage 的 PID、session、manifest digest、endpoint；每个 storage 的 CXL RPC 与 `bulk_read + bulk_write` 活动必须为正，并与上述实际文件放置相互印证。不能强求每个节点两个方向计数都非零：bulk read/write 表示该进程发起的 pull/push，不直接等于应用文件 read/write。
- [x] smoke 结果明确区分内容校验、布局覆盖、backend read、transport、cleanup。throughput 不作为该 smoke 的通过条件，也不生成论文性能结论。

### Task 5：giga 上顺序验收和现有 IO500 兼容

**Files:** 仅部署上述 native 文件；产物写入独立的 `target/results/giga-native-3fs/<run-id>/`。主仓库 wrapper 和 IO500 profile 不改。

**Consumes:** 前四项的本地测试；已有受验证二进制和 build manifest。

**Produces:** 有证据的 1/2/4S 功能支持；有界 IO500 结果按真实状态保留。

- [x] 部署前核对本地/远端 C++ 源与二进制已有构建证据、第三方源码身份、ELF hashes。仅 Python 变化时复用已有二进制；不能确认构建来源时做必要的增量构建，不能伪造对应关系。此任务不使用 GPU。
- [x] 检查选定 CPU/SMT/LLC、正在进行的工作、全部固定端口；保留既有 `/dev/shm >= 8 GiB`、`/tmp >= 12 GiB` 空间检查和精确所有权规则。已有 GPU/bench 进程不停止。
- [x] 按 **2C1S smoke → 2C2S smoke → 2C4S smoke** 顺序执行，上一项通过才继续。1S 已有多客户端 liveness 不在这里被假定通过；若基线失败，记录首个失败阶段，先区分已有问题与扩展问题。
- [x] smoke 全部通过后，用完全相同的现有有界 IO500 配置运行 **3C1S → 3C2S → 3C4S**。这三次是功能/兼容验收，不因 1 秒 stonewall 就成为正式 IO500 成绩。

已实现的入口示例：

```bash
python3 components/3FS/deploy/giga-native/storage_smoke.py \
  --repo /root/cxlmemsim-riscv-io500 \
  --build-manifest /root/cxlmemsim-riscv-io500/target/results/giga-native-3fs/build-manifest.json \
  --topology 2c2s --run-id storage-scale-smoke-2c2s-r1

scripts/run_giga_native_3fs_io500.sh \
  --topology 3c2s --run-id storage-scale-bounded-3c2s-r1
```

- [x] 22 个 phase 的启用状态、原配置哈希、原 stonewall、验证器和 `official=false / expected_invalid=true` 保持原值；只允许既有 datadir/resultdir/find-rank 替换。IO500 中某个相位没有触及所有 server 时记录偏斜，不用聚合分数伪称均匀扩展。
- [x] 每次只启动一个集群。成功按已有逻辑回收本次 tmpfs/CXL backing；保存小型 JSON、配置、必要日志和摘要，不启用大型 perf/trace，也不新建压缩备份。失败保留首错和必要诊断，避免重试覆盖。

### Task 6：文档、回归与交付

**Files:** `deploy/giga-native/README.md` 和本计划进度；其他允许改动限前述文件清单。

**Consumes:** 实际测试与运行结果，不能用预期值填充通过状态。

**Produces:** 可重复调用的 native 多 server 入口及明确的结果边界。

- [x] 记录九种拓扑、RF1/四 target/stripe1 合同、6/8/12 核预算、LLC 共享、tmpfs 后端、逐节点证据及失败定位方法。
- [x] 运行以下回归，检查新测试覆盖身份碰撞、路由放置、基线配置不变和部分启动失败；检查 `git diff --check`。

```bash
python3 -m unittest discover -s components/3FS/tests/giga_native -p 'test_*.py'
python3 -m unittest discover -s components/3FS/tests/cxl_riscv -p 'test_make_phase1_manifest.py'
python3 -m unittest discover -s components/3FS/tests/cxl_riscv -p 'test_full_stack.py'
git -C components/3FS diff --check
```

- [x] 审查 diff：`src/**`、公共 RISC-V 生成器/模板、第三方目录、IO500 profile、论文和主仓库 IO500 脚本均无本任务改动。之前已有未提交修改仍保留。
- [x] 交付报告分别列出源码/配置测试、1/2/4S smoke、3C1/2/4S IO500 的通过/失败/未执行状态和证据路径。本轮计划及后续实现不自动提交或推送。

## 验收合同与后续边界

功能完成要求：相同二进制和机制配置；独立节点身份；四个 RF1 target 按计划唯一放置；多文件内容正确；每个 storage 的实际存储与 CXL 活动被证明；原 1S 接口继续工作；启动失败及正常结束均精确清理本次资源。

增加 server 后是否更快是后续测量问题，不是功能验收条件。首轮既有 IO500 的客户端最多三核，可能先受客户端、meta/FDB、共享内存带宽或 LLC 影响；不把“没有加速”直接解释成 storage 不可扩展。

本次不做在线集群扩容、RF2/3、自动重平衡、single-file stripe 调整、客户端上限扩展、元数据服务扩展或跨机实验。若后续研究单大文件，另开一组在所有 S 上固定同一 stripe size 的实验，不混入本轮默认 stripe1 结果。

## 当前状态

- [x] 检查当前 native 启动、manifest、配置加载、target/chain、文件布局和清理路径。
- [x] 核对第四 storage endpoint 冲突与本地配置隔离方式。
- [x] 只读核对 giga CPU/SMT/LLC 映射。
- [x] 改动前 65 项相关 Python 测试通过。
- [x] 完成并自查此计划。
- [x] 用户批准并要求按计划实施；Task 1–6 已完成；最终 1/2/4S smoke 与 3C1/2/4S IO500 全部通过。没有提交或推送。

## 2026-09-11 实施记录（已完成）

- 仅修改 native Python 适配、相应测试与文档；原 C++、公共生成器、第三方与 IO500 profile 不变。
- `storage-scaling-adaptation/source-attestation-before.json` 证明：远端原有完整源码指纹匹配原 build manifest，本地与远端 src（990 文件）、cmake（12 文件）、third_party（10715 文件）及 CMakeLists.txt 指纹一致；16 个构建产物的哈希通过原验证器。复用原二进制，不重写构建来源。
- giga `2c4s --preflight` 通过，端口与物理核合同成立；未操作已有 GPU 工作。
- 两个验收适配错误已修正并保留失败记录：`2c1s-r1` 对 Core socket 数量的假设过窄，原生 Core 会按主机 NIC 创建多个临时端口；`2c1s-r2` 的原生 stat 表使用 `ChainId(...)`，且 last chunk 是序号，需按 `meta::ChunkId` 的大端布局从 inode/序号构造真正 chunk ID。后者的 64 文件写入与独立读回已全部通过。
- 地址证据细化：原生 list-nodes 不打印 serviceGroups，所以使用 PID/heartbeat + 每节点 Core get-config + `/proc/<pid>/fd` 所属监听 socket。保留真实多网卡 Core 地址，不更改原生 listener 配置。
- `2c1s-r3` 暴露原二进制在频繁 admin 重建时的 `queryLastChunk / RPC::SendFailed`（第 9 个 stat、180 秒命令界限），文件内容读回正常，管理 heartbeat 正常。保存复现，未改 C++、协议、重试或 deadline。smoke 的 64 个 stat 改用原生 Dispatcher 分号批处理，每条命令与结果仍严格校验，保留 rc=0 但批处理提前停止的负例。该故障未被宣称修复。
- `storage-scale-smoke-20260911-2c1s-r4` 完整通过：64×4 MiB 跨客户端全量读回、四 chain 覆盖、实际 backend checksum read、逐 storage CXL 活动、全部 teardown；四个 target 的 `only_chunk_engine=true` 已归档验证。2S/4S 与 IO500 继续验收。

- `2c2s-r1` 完整通过，两个 storage 各 32 个文件、128 MiB，逐节点 checksum read 和 CXL 活动均通过。
- `2c4s-r1` 在首次文件创建时暴露启动门槛不足：mgmtd 路由版本 15，但 meta 的 chain allocator 仍看到版本 6，因此返回 `ChainTable ... Version(0) not found`。没有传输 bulk 请求，也没有存储写入错误。增加启动 barrier：观察 admin 已完成的最新路由刷新，等待 meta 完成同等或更新版本的刷新，并排除被丢弃的不完整视图。保持原生 10 秒刷新周期与所有协议/重试参数。按最终版本重走 1/2/4S smoke 和有界 IO500。
- 当前本地回归 59 + 7 + 21 = **87 项通过**，`git diff --check` 通过。

- 最终适配版本 smoke 梯度已经完成：`2c1s-r5`、`2c2s-r2`、`2c4s-r2` 均通过；对应每 server 的文件数是 64、32/32、16/16/16/16，完整 SHA-256 与 backend checksum 均通过。所有 target 均使用新引擎，逐 storage PID/session/endpoint/manifest 和 CXL 活动校验均通过，全部 owned 进程停止、挂载释放、固定端口释放。
- 有界 IO500 `3c1s-r1`、`3c2s-r1`、`3c4s-r1` 全部通过 22 个 phase 与原验证器。保留原 profile 和 INVALID 标记，不将这些功能结果用作正式性能结论。

## 最终交付与证据

- `target/results/giga-native-3fs/storage-scaling-adaptation/report.md`：自包含说明和各次结果链接；`verified-summary.json` 独立重验六个结果包的配置来源、引擎、布局、transport 与清理条件。
- `phase-layout-bounds.json`：从 27 份实际 IOR 阶段日志中的 access 模式和固定 stripe1 推导前台文件数据的 server 数上界。shared-file 阶段最多一个 server；3 rank 的 file-per-process 阶段在 4S 下最多三个。没有修改测量、没有把进程全生命周期计数当作相位计数；具体相位的 server 计数/CPU 归因仍是后续独立实验。
- 最终本地回归 **87 项通过**；本地与远端 9 个 Python 文件哈希相同。原 binary build manifest 保留，所有生产 C++、公共生成器、第三方与 IO500 profile 未被本任务改动。
- 所有成功运行自动清理本次 backing；确认失败尝试均已停止后，精确回收其 8 个 owned 临时根，释放 5.459 GiB。保留全部结果、日志与配置，无压缩备份。最终所有固定端口为空，无本次残留 backing。
- 修复的是部署/验收适配（地址、表格式和 meta 路由就绪条件），没有引入协议或数据路径优化。短生命周期 admin 的一次 CXL SendFailed 单独保留，根因未确认，未宣称被修复。
- 没有提交、推送或改动论文；已有其他未提交改动保持原状。
