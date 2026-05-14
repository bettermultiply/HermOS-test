# Motivation 实验：统一执行方案

## 实验总目标

在远端 snapshot fetching 场景下，对比全量拉取（Eager）和按需拉取（Lazy）两类方案在不同并发度下的冷启动表现，揭示各自的瓶颈，论证需要一种新的快照拉取方案。

### 成功条件

实验完成后，你应该能用数据支撑以下论述：

1. **Eager 的瓶颈**：远端全量下载 + 解压是串行的前置开销，所有 sandbox 都要等它完成。解压阶段存在大量零页无效开销。即使在高并发下 restore 阶段可以并行分摊，fetch + decompress 仍然是冷启动的主要瓶颈。
2. **Lazy 的瓶颈**：单实例下每次 page fault 走远端 HTTP 请求，RTT 累积导致端到端延迟高。高并发下 uffd 处理开销累积，且多实例独立发起大量重复的远端 page fault 请求（外部 cache 无法解决这个粒度的去重）。
3. **跨 sandbox 冗余**：并发 200 个 sandbox 时，Lazy 方案的实际远端请求数远大于去重后的理论最小值。

### 如果数据不符合预期

- 如果 Eager-Remote 的零页解压开销不显著（dense 解压 vs 原始解压耗时接近）→ C1 的 motivation 调整为强调传输体积浪费而非解压时间
- 如果 Lazy-Remote 在并发度=1 时端到端时间低于 Eager-Remote → 不是坏事，说明 eager 的串行问题更严重，但高并发数据会展示 lazy 的扩展性问题

---

## 控制变量

| 变量 | 固定值 | 说明 |
|------|--------|------|
| Rootfs | 本地，不经过远端 | 隔离 memory 策略的影响 |
| Workload | `python-json-tool`、`cli-pipeline`、`random-rg-scan-fixed`、`random-rg-scan-random`、`read-list`、`agent-tool-trace`，分别跑 | 围绕 Agent 常用工具链，覆盖通用脚本、CLI pipeline、不可预测代码搜索、内存读密集和 replayed tool use |
| 重复次数 | 每组 10 次，取 max | 预期方差小，报告标准差以佐证 |
| Firecracker 版本 | （填入你的版本） | 所有组统一 |
| Snapshot 压缩格式 | zstd 压缩 memory snapshot | Eager 组使用 |
| Snapshot 来源 | 远端 registry（Eager-Local 除外） | 统一远端场景 |

---

## 实验分组

| 组号 | 名称 | Memory 策略 | 数据位置 | 论证角色 |
|------|------|-------------|----------|----------|
| A | Eager-Local | 本地解压 → load_snapshot | 本地 | 参考上界（最快） |
| B | Eager-Remote | 远端下载 → 本地解压 → load_snapshot | 远端 | 暴露 fetch 串行开销 + 零页解压开销 |
| C | Lazy-Remote | uffd 按需，每个 sandbox 独立向远端发 HTTP 请求 | 远端 | 暴露远端 RTT 累积 + 高并发下重复请求 |
| E | Lazy-Local（可选） | uffd 按需，fault 从本地读 | 本地 | 留给设计章节（uffd 即使本地也有运行时开销） |

## 并发度

{1, 50, 100, 200}

并发度=1 的数据同时用于图 1（延迟拆解）和图 2（折线图的起点）。

---

## 每组在不同并发度下的执行流程

### 组 A：Eager-Local

**仅跑并发度=1**（作为参考上界，高并发下不测此组，因为它不涉及远端拉取）。

**前置准备**：压缩态 memory snapshot 已在本地磁盘。

**执行步骤与计时点**：

```
t0 ← 记录时间戳
执行: zstd -d memory_snapshot.zst -o memory_snapshot
t1 ← 记录时间戳
执行: Firecracker load_snapshot API
t2 ← 记录时间戳
执行: Firecracker resume_vcpu API
t3 ← 记录时间戳
等待: workload 首次输出
t4 ← 记录时间戳
```

**记录的时间段**：

| 指标 | 计算 | 含义 |
|------|------|------|
| T_decompress | t1 - t0 | 本地解压耗时（含零页） |
| T_restore | t3 - t1 | load_snapshot + resume_vcpu |
| T_first_response | t4 - t3 | vCPU 启动到 workload 输出 |
| **T_total** | **t4 - t0** | **端到端** |

> 此组无 T_fetch，因为数据已在本地。

---

### 组 B：Eager-Remote

**并发度={1, 50, 100, 200} 均跑。**

**关键特征**：fetch + decompress 是所有 sandbox 共享的串行前置步骤，只做一次。之后 N 个 Firecracker 实例并发启动。

#### 并发度=1 的流程

```
t0 ← 记录时间戳
执行: fetch 程序从 registry 下载压缩 snapshot
t1 ← 记录时间戳
执行: zstd -d 解压
t2 ← 记录时间戳
执行: Firecracker load_snapshot
t3 ← 记录时间戳
执行: Firecracker resume_vcpu
t4 ← 记录时间戳
等待: workload 首次输出
t5 ← 记录时间戳
```

**记录的时间段**：

| 指标 | 计算 | 含义 |
|------|------|------|
| T_fetch | t1 - t0 | 远端下载耗时 |
| T_decompress | t2 - t1 | 解压耗时（含零页） |
| T_restore | t4 - t2 | load_snapshot + resume_vcpu |
| T_first_response | t5 - t4 | vCPU 启动到 workload 输出 |
| **T_total** | **t5 - t0** | **端到端** |

#### 并发度=N（50/100/200）的流程

```
t0 ← 记录时间戳
执行: fetch 程序从 registry 下载压缩 snapshot（一次）
t1 ← 记录时间戳
执行: zstd -d 解压（一次）
t2 ← 记录时间戳
并发启动 N 个 Firecracker 实例:
    对每个实例 i:
        t3_i ← 记录时间戳（开始 load_snapshot）
        执行: Firecracker load_snapshot
        t4_i ← 记录时间戳（开始 resume_vcpu）
        执行: Firecracker resume_vcpu
        t5_i ← 记录时间戳（resume 完成）
        等待: workload 首次输出
        t6_i ← 记录时间戳
```

**记录的指标**：

| 指标 | 计算 | 含义 |
|------|------|------|
| T_fetch | t1 - t0 | 远端下载耗时（共享，一次） |
| T_decompress | t2 - t1 | 解压耗时（共享，一次） |
| T_restore_i | t5_i - t3_i | 第 i 个 sandbox 的 restore 耗时 |
| T_first_response_i | t6_i - t5_i | 第 i 个 sandbox 的首次响应耗时 |
| T_total_i | t6_i - t0 | 第 i 个 sandbox 的端到端时间 |
| **T_total_max** | **max(t6_i) - t0** | **最慢 sandbox 的端到端时间（整体完成时间）** |

> 图 2 中 Eager 的纵轴取 T_total_max（所有 sandbox 都就绪的时间）。

**注意**：每次跑之前清除本地缓存，确保 fetch 从远端实际拉取。

---

### 组 C：Lazy(uffd)-Remote

**并发度={1, 50, 100, 200} 均跑。**

**关键特征**：每个 sandbox 完全独立，各自的 uffd handler 独立向远端 registry 发 HTTP 请求。无共享前置步骤。

#### 并发度=1 的流程

```
t0 ← 记录时间戳
执行: Firecracker load_snapshot（空/minimal memory backend）
执行: 注册 uffd handler
t1 ← 记录时间戳
执行: Firecracker resume_vcpu
--- vCPU 运行，触发 page fault → uffd handler 逐个从远端拉取 ---
（uffd handler 内部：每次 fault 记录一条日志）
t2 ← 记录时间戳（workload 首次输出）
```

**记录的时间段**：

| 指标 | 计算 | 含义 |
|------|------|------|
| T_setup | t1 - t0 | load_snapshot + uffd 注册 |
| T_execution | t2 - t1 | resume 到 workload 输出 |
| **T_total** | **t2 - t0** | **端到端** |

#### 并发度=N（50/100/200）的流程

```
t0 ← 记录时间戳
并发启动 N 个 Firecracker 实例:
    对每个实例 i:
        t1_i ← 记录时间戳
        执行: Firecracker load_snapshot（空/minimal memory backend）
        执行: 注册 uffd handler
        t2_i ← 记录时间戳
        执行: Firecracker resume_vcpu
        --- uffd handler 独立处理 fault ---
        t3_i ← 记录时间戳（workload 首次输出）
```

**记录的指标**：

| 指标 | 计算 | 含义 |
|------|------|------|
| T_total_i | t3_i - t0 | 第 i 个 sandbox 的端到端时间（从统一起点算） |
| **T_total_max** | **max(t3_i) - t0** | **最慢 sandbox 的端到端时间** |

---

### 组 E（可选）：Lazy(uffd)-Local

**仅跑并发度=1。** 留给设计章节论证"uffd 即使本地也有运行时开销"。

流程与组 C 并发度=1 相同，区别：uffd handler 从本地磁盘读取 page 而非远端 HTTP。

---

## 零页开销补充测量

独立于主实验，用于量化 Eager 方案中解压阶段的零页无效开销。

### Step 1：零页占比扫描

对所有可用 snapshot 运行：

```bash
python3 fetch-motivation/simple-test/zero-pages.py /path/to/memory_snapshot
```

输出包含 `total_pages`、`zero_pages`、`zero_ratio`、文件大小和 dense 大小。

### Step 2：生成 dense snapshot

```bash
python3 fetch-motivation/simple-test/zero-pages.py \
  /path/to/memory_snapshot \
  --dense /path/to/memory_snapshot.dense
```

### Step 3：分别压缩后解压计时

```bash
# 压缩
zstd memory_snapshot -o memory_snapshot.zst
zstd memory_snapshot.dense -o memory_snapshot.dense.zst

# 解压计时（各跑 10 次取 max）
time zstd -d memory_snapshot.zst -o /tmp/out
time zstd -d memory_snapshot.dense.zst -o /tmp/out
```

### Step 4：计算倍数

```
零页开销倍数 = T_decompress_original / T_decompress_dense
```

论文表述："解压包含零页的原始 snapshot 耗时是解压剥离零页后的 dense snapshot 的 N 倍。"

### 数据记录表

| Workload | 文件大小 (MB) | 零页占比 (%) | 原始解压耗时 (ms) | Dense 解压耗时 (ms) | 解压倍数 |
|----------|--------------|-------------|-------------------|--------------------|---------| 
| python-json-tool | 待测 | 待测 | 待测 | 待测 | 待测 |
| cli-pipeline | 待测 | 待测 | 待测 | 待测 | 待测 |
| random-rg-scan-fixed | 待测 | 待测 | 待测 | 待测 | 待测 |
| random-rg-scan-random | 待测 | 待测 | 待测 | 待测 | 待测 |
| read-list | 待测 | 待测 | 待测 | 待测 | 待测 |
| agent-tool-trace | 待测 | 待测 | 待测 | 待测 | 待测 |

> 建议跑 5-10 个 workload。如果零页占比 >70% 且解压倍数 >1.5x，C1 的 motivation 很强。

---

## 图表设计

### 图 1：单实例（并发度=1）端到端延迟拆解——堆叠柱状图

**数据来源**：组 A/B/C 在并发度=1 下的数据。

**横轴**：3 个方案（Eager-Local / Eager-Remote / Lazy-Remote），按 workload 分组。

**纵轴**：时间 (ms)

**Eager 组的色块（从底到顶）**：
- `fetch`（仅 Eager-Remote）
- `decompress (dense)`：有效数据解压耗时（= 零页补充测量中的 T_decompress_dense）
- `decompress (zero-page overhead)`：零页开销（= T_decompress_original - T_decompress_dense）← **C1 核心 motivation**
- `restore`
- `first response`

**Lazy 组的色块（从底到顶）**：
- `setup`（load_snapshot + uffd 注册）
- `fault handling (total)`：所有 fault 的处理总时间（= uffd 日志中 Σ(t_fault_end - t_fault_start)）
- `compute (non-fault)`：T_execution - fault handling total（纯 CPU 运行时间）

**关键读图方式**：
- Eager-Remote 柱子中 `decompress (zero-page overhead)` 色块的大小 → C1 motivation 强度
- Lazy-Remote 整体远大于 Eager-Local → 远端按需拉取的代价

### 图 2：不同并发度下的端到端时间——折线图（Money Figure）

**数据来源**：组 B/C 在并发度 {1, 50, 100, 200} 下的 T_total_max。

**横轴**：并发度（1, 50, 100, 200）

**纵轴**：端到端时间 (ms)——最慢 sandbox 完成的时刻（从统一起点 t0 算）

**线条**：Eager-Remote / Lazy-Remote（2 条线）

**可选**：用阴影区域表示 max 与 min 的范围（10 次重复中的）

**期望观察**：
- Eager-Remote：fetch + decompress 是常数项，随并发增加 restore 阶段的并发争用使总时间增长，但 fetch+decompress 始终占大头
- Lazy-Remote：随并发增加，200 个 sandbox 各自独立发远端请求，请求总量线性增长，端到端时间上升

**跨 sandbox 冗余数字（文字段落，不出图）**：

在图 2 的讨论中写：

> "在并发度=200 时，Lazy-Remote 方案共产生 X 次远端 page 请求，而去重后理论最小值仅为 Y 次（冗余率 Z%）。这表明朴素按需拉取在高并发下存在严重的跨实例请求冗余，外部 cache 无法在 page 粒度消除这些重复。"

---

## 执行检查清单

### 跑实验前确认

- [ ] 远端 registry 可达，`ping` RTT：______ ms
- [ ] 本地无 snapshot 缓存机制（每次跑前手动清除 or 确认无 cache）
- [ ] Firecracker 版本：______
- [ ] zstd 版本：______
- [ ] uffd handler 程序已测试通过（单次 fault 能正确拉取并写入）
- [ ] Workload 已部署到 snapshot 中（Agent-centered workload suite）
- [ ] 计时脚本已验证（手动跑一次确认每个时间戳都有输出）
- [ ] Rootfs 统一为本地
- [ ] 并发启动脚本已验证（能同时 fork N 个 Firecracker 进程）
- [ ] 机器资源确认：CPU 核数 ____，内存 ____ GB（确保能承受 200 并发）
- [ ] 网络监控就绪（`sar -n DEV 1` 或 `bwm-ng`，持续记录）

### 每次跑之前确认

- [ ] 清除本地 snapshot 缓存（Eager-Remote / Lazy-Remote）
- [ ] 所有 Firecracker 进程已完全退出
- [ ] uffd handler 日志目录已清空（Lazy 组）
- [ ] 网络监控已启动

### 跑完后确认

- [ ] 原始时间戳完整（不只是差值）
- [ ] uffd handler 日志完整（行数合理、无截断）
- [ ] 网络监控日志已保存（用于排查是否带宽打满）

---

## 数据记录模板

### 组 A：Eager-Local（仅并发度=1）

每个 workload 一张表：

| Run # | T_decompress | T_restore | T_first_response | T_total |
|-------|-------------|-----------|-----------------|---------|
| 1 | | | | |
| ... | | | | |
| 10 | | | | |
| **max** | | | | |
| **std** | | | | |

### 组 B：Eager-Remote

**并发度=1**（每个 workload 一张表）：

| Run # | T_fetch | T_decompress | T_restore | T_first_response | T_total |
|-------|---------|-------------|-----------|-----------------|---------|
| 1 | | | | | |
| ... | | | | | |
| 10 | | | | | |
| **max** | | | | | |
| **std** | | | | | |

**并发度=50/100/200**（每个并发度 × 每个 workload 一张表）：

| Run # | T_fetch | T_decompress | T_total_max | T_total_min | T_restore_avg | T_first_response_avg |
|-------|---------|-------------|-------------|-------------|--------------|---------------------|
| 1 | | | | | | |
| ... | | | | | | |
| 10 | | | | | | |
| **max** | | | | | | |
| **std** | | | | | | |

> T_fetch 和 T_decompress 是共享的，每次只有一个值。T_total_max/min 是 N 个 sandbox 中最慢/最快的。

### 组 C：Lazy-Remote

**并发度=1**（每个 workload 一张表）：

| Run # | T_setup | T_execution | T_total | fault_count | T_fault_total | T_fault_avg | T_fetch_per_fault_avg |
|-------|---------|------------|---------|-------------|--------------|-------------|----------------------|
| 1 | | | | | | | |
| ... | | | | | | | |
| 10 | | | | | | | |
| **max** | | | | | | | |
| **std** | | | | | | | |

**并发度=50/100/200**（每个并发度 × 每个 workload 一张表）：

| Run # | T_total_max | T_total_min | total_requests | unique_pages | redundancy_ratio |
|-------|-------------|-------------|---------------|--------------|-----------------|
| 1 | | | | | |
| ... | | | | | |
| 10 | | | | | |
| **max** | | | | | |
| **std** | | | | | |

---

## 统一结论模板

> 我们系统评估了远端 snapshot fetching 场景下全量拉取（Eager）和按需拉取（Lazy）两类方案的冷启动表现。
>
> 在单实例场景下（图 1），Eager 方案的解压阶段存在大量零页无效开销——memory snapshot 中零页占比高达 P%，解压原始 snapshot 的耗时是剥离零页后的 N 倍。Lazy 方案虽然避免了全量下载，但每次 page fault 需要一次远端 HTTP 往返，RTT 累积导致端到端延迟显著高于本地方案。
>
> 在高并发场景下（图 2），问题进一步恶化。Eager 的 fetch + decompress 是所有 sandbox 的串行前置开销，在并发度=200 时仍占端到端时间的 X%。Lazy 方案中，200 个 sandbox 独立发起远端 page 请求，总请求量达 A 次，其中去重后仅需 B 次（冗余率 Z%）。
>
> 这些发现指向了三个设计需求：(1) 对 memory snapshot 的 sparse 结构进行感知处理以消除零页开销；(2) 将快照拉取与 restore 执行进行流水线化以压缩串行路径；(3) 在 host 层对跨 sandbox 的重复请求进行合并以减少远端访问量。

---

## 执行优先级

| 优先级 | 事项 | 产出 | 依赖 |
|--------|------|------|------|
| **P0** | 测 RTT（`ping` registry） | 环境参数 | 无 |
| **P0** | 跑零页占比扫描（5-10 个 snapshot） | C1 motivation 数据 | 有 snapshot 文件 |
| **P0** | 跑零页解压倍数测量 | 图 1 的零页开销拆解依据 | P0 零页扫描 |
| **P1** | 跑并发度=1 全部组 | 图 1 数据 | P0 |
| **P2** | 跑并发度=50/100/200 全部组 | 图 2 数据 | P1 |
| **P3** | 可选：`tc netem` 模拟 RTT=10ms 跑一轮 | 附录 RTT 敏感性分析 | P2 |
